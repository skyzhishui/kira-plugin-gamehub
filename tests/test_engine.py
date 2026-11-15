"""引擎层测试：RNG golden 向量、gomoku 规则、对局层动作与视图。"""

from __future__ import annotations

import json

import pytest

from engine import (
    CreateContext,
    MoveResultErr,
    MoveResultOk,
    NewMatchOptions,
    NewSeat,
    apply_match_action,
    create_match,
    describe_match,
    games,
    join_seat,
    next_random,
    seat_by_token,
    shuffled,
    status_of,
    view_match,
)
from engine.match import Match

# 由 rain-go 的 rng.ts 实跑导出（node，state=123456789 / 0）
GOLDEN_123456789 = [
    (0.2577907438389957, 1955022602),
    (0.9707721115555614, -508378881),
    (0.7853280142880976, 1323186932),
    (0.20616457983851433, -1140214551),
    (0.30307188746519387, 691351262),
]
GOLDEN_ZERO = [
    (0.26642920868471265, 1831565813),
    (0.0003297457005828619, -631835670),
    (0.2232720274478197, 1199730143),
]


@pytest.mark.parametrize("seed,expected", [(123456789, GOLDEN_123456789), (0, GOLDEN_ZERO)])
def test_next_random_golden(seed, expected):
    state = seed
    for value, next_state in expected:
        got, state = next_random(state)
        assert got == value
        assert state == next_state


def test_shuffled_is_deterministic_and_non_mutating():
    items = list(range(10))
    out1, s1 = shuffled(items, 42)
    out2, s2 = shuffled(items, 42)
    assert out1 == out2 and s1 == s2
    assert items == list(range(10))
    assert sorted(out1) == items


def _gomoku():
    return games.get("gomoku")


def _empty_state():
    return _gomoku().create(CreateContext(seed=1, players=2, options={}))


def test_gomoku_coordinates_skip_i():
    """列字母跳过 I（与 rain-go 一致）：I 非法，J 是第 9 列，P 是最后一列。"""
    from engine.coords import COLUMNS, from_gtp, to_gtp

    assert COLUMNS[:15] == "ABCDEFGHJKLMNOP"
    assert from_gtp("I8", 15) is None
    assert from_gtp("J8", 15) == 7 * 15 + 8
    assert to_gtp(7 * 15 + 8, 15) == "J8"
    assert from_gtp("P15", 15) == 14
    assert to_gtp(14, 15) == "P15"
    # 首手不限制天元：四角都可以
    mod = _gomoku()
    for corner in ("A1", "A15", "O1", "O15"):
        state = _empty_state()
        assert isinstance(mod.apply(state, 0, corner), MoveResultOk), corner


def test_gomoku_first_move_and_turn_order():
    mod = _gomoku()
    state = _empty_state()
    assert state["toPlay"] == 1 and state["moves"] == 0
    result = mod.apply(state, 1, "H8")
    assert isinstance(result, MoveResultErr) and "还没轮到你" in result.error
    result = mod.apply(state, 0, "H8")
    assert isinstance(result, MoveResultOk)
    assert result.log == "H8"
    assert mod.waiting_on(result.state) == [1]
    assert state["cells"][112] == 0  # 输入不被变异
    # 重复落子
    again = mod.apply(result.state, 1, "H8")
    assert isinstance(again, MoveResultErr) and "已经有子" in again.error


def test_gomoku_win_and_after_game():
    mod = _gomoku()
    state = _empty_state()
    moves = [(0, "A15"), (1, "A1"), (0, "B15"), (1, "B1"), (0, "C15"), (1, "C1"), (0, "D15"), (1, "D1"), (0, "E15")]
    for seat, move in moves:
        result = mod.apply(state, seat, move)
        assert isinstance(result, MoveResultOk), move
        state = result.state
    outcome = mod.outcome(state)
    assert outcome is not None
    assert outcome.winners == [0]
    assert outcome.text == "黑连五"
    assert mod.waiting_on(state) == []
    blocked = mod.apply(state, 1, "J8")
    assert isinstance(blocked, MoveResultErr) and "结束" in blocked.error


def test_gomoku_coordinates_and_bot():
    mod = _gomoku()
    state = _empty_state()
    assert mod.bot(state, 0) == "H8"  # 天元
    first = mod.apply(state, 0, "H8")
    assert isinstance(first, MoveResultOk)
    bot_move = mod.bot(first.state, 1)
    assert bot_move and isinstance(mod.apply(first.state, 1, bot_move), MoveResultOk)


def test_gomoku_candidates_hook_removed():
    """候选白名单已删除（未落地的分层决策）；模块不再暴露 candidates。"""
    mod = _gomoku()
    assert not hasattr(mod, "candidates")


def test_gomoku_describe_mentions_own_colour_and_board():
    mod = _gomoku()
    state = mod.apply(_empty_state(), 0, "H8").state
    text = mod.describe(state, 1, ["甲", "乙"])
    assert "You are O (white)" in text
    assert "Last move: H8." in text
    assert "X . ." in text or "X" in text


def _new_match(seat_kinds=("human", "ai")):
    seats = [
        NewSeat(kind=kind, token=f"tok{i}", name=f"P{i}")
        for i, kind in enumerate(seat_kinds)
    ]
    return create_match(
        NewMatchOptions(
            id="m1", kind="gomoku", seats=seats, seed=7, now=1_000
        )
    )


def test_create_match_autoplays_bots_without_version_bump():
    match = _new_match(("human", "bot"))
    # bot 执白，首手轮到人类，所以还没有自动走子
    assert match.version == 1
    assert match.seats[1].joined is True
    assert match.seats[1].token == ""
    human = apply_match_action(match, 0, {"type": "move", "move": "H8"}, 2_000)
    assert human.ok
    # 人类落子后 autoplay 让 bot 立刻回一手；version 只 +1
    assert human.match.version == 2
    assert len(human.match.log) == 2
    assert human.match.log[1].seat == 1
    assert human.match.state["toPlay"] == 1


def test_apply_match_action_can_defer_bot_autoplay():
    """``autoplay_bots=False``：引擎不再内联补手，交给房间层按节奏调度。"""
    match = _new_match(("human", "bot"))
    human = apply_match_action(
        match, 0, {"type": "move", "move": "H8"}, 2_000, autoplay_bots=False
    )
    assert human.ok
    assert human.match.version == 2
    assert len(human.match.log) == 1  # 只有人类这一手
    assert human.match.log[0].seat == 0
    assert human.match.state["toPlay"] == 2  # 仍轮到 bot，等房间层补


def test_create_match_can_skip_initial_bot_autoplay():
    """开局也可关闭内联 autoplay（房间层自己排程首手）。"""
    seats = [
        NewSeat(kind="human", token="t0", joined=True),
        NewSeat(kind="bot", token="", joined=True),
    ]
    match = create_match(
        NewMatchOptions(
            id="m2", kind="gomoku", seats=seats, seed=7, now=1_000, autoplay_bots=False
        )
    )
    assert match.version == 1
    assert match.log == []


def test_apply_match_action_rejects_and_strips_private_fields():
    match = _new_match()
    bad = apply_match_action(match, 5, {"type": "move", "move": "H8"}, 2_000)
    assert not bad.ok and bad.error == "bad_seat"
    ok = apply_match_action(match, 0, {"type": "move", "move": "H8"}, 2_001)
    assert ok.ok and ok.match.version == 2
    view = view_match(ok.match, 0)
    assert view["me"] == 0
    assert view["labels"] == ["黑", "白"]
    assert all("token" not in s for s in view["seats"])
    assert "state" not in view
    spectator = view_match(ok.match, None)
    assert spectator["view"]["you"] is None
    json.dumps(view, ensure_ascii=False)  # 必须可序列化


def test_chat_and_rename_and_resign():
    match = _new_match()
    said = apply_match_action(match, 0, {"type": "say", "text": "  你好  "}, 2_000)
    assert said.ok
    assert said.match.chat[0].text == "你好"
    empty = apply_match_action(said.match, 0, {"type": "say", "text": "   "}, 2_001)
    assert not empty.ok and empty.error == "empty"
    renamed = apply_match_action(said.match, 0, {"type": "rename", "name": "甲"}, 2_002)
    assert renamed.ok and renamed.match.seats[0].name == "甲"
    bad_name = apply_match_action(renamed.match, 0, {"type": "rename", "name": ""}, 2_003)
    assert not bad_name.ok and bad_name.error == "bad_name"
    resigned = apply_match_action(renamed.match, 1, {"type": "resign"}, 2_004)
    assert resigned.ok
    status = status_of(resigned.match)
    assert status.waiting_on == []
    assert status.result_text == "P1 认输"
    again = apply_match_action(resigned.match, 0, {"type": "move", "move": "A1"}, 2_005)
    assert not again.ok and again.error == "game_over"


def test_move_say_by_marker_is_keyword_only():
    """``MatchChat.by`` marks the real speaker of an attached say.

    The marker rides a keyword-only parameter of ``apply_match_action``: the
    REST path passes the client's action dict verbatim, so a dict-level
    ``by`` key must stay inert — only the room layer (delegated plays) may
    mark a say as spoken by the host agent. The marker survives to_dict /
    from_dict and the page view.
    """
    match = _new_match(("human", "human"))
    forged = apply_match_action(
        match, 0, {"type": "move", "move": "H8", "say": "伪造", "by": "host"}, 2_000
    )
    assert forged.ok
    assert forged.match.chat[-1].by == ""

    delegated = apply_match_action(
        forged.match, 1, {"type": "move", "move": "J8", "say": "我代下"}, 2_001,
        say_by="host",
    )
    assert delegated.ok
    assert delegated.match.chat[-1].by == "host"
    assert view_match(delegated.match, None)["chat"][-1]["by"] == "host"

    restored = Match.from_dict(delegated.match.to_dict())
    assert restored.chat[-1].by == "host"

    plain = apply_match_action(
        delegated.match, 0, {"type": "move", "move": "K9", "say": "本人发言"}, 2_002
    )
    assert plain.ok and plain.match.chat[-1].by == ""


def test_seat_token_join_and_lock_truncation():
    match = _new_match()
    assert seat_by_token(match, "tok0") == 0
    assert seat_by_token(match, "nope") is None
    assert seat_by_token(match, "") is None
    joined = join_seat(match, 0, 2_000)
    assert joined.seats[0].joined and joined.version == match.version + 1
    assert join_seat(joined, 0, 2_001) is joined  # 幂等
    many = match
    for i in range(520):
        res = apply_match_action(many, 0, {"type": "say", "text": f"m{i}"}, 3_000 + i)
        assert res.ok
        many = res.match
    assert len(many.chat) == 100
    assert len(json.dumps(many.to_dict(), ensure_ascii=False)) > 0


def test_describe_uses_move_protocol_not_mcp_tools():
    match = _new_match()
    text = describe_match(match, 0, page_url="http://x/match")
    assert "Page: http://x/match" in text
    assert "YOUR TURN" in text
    assert "MOVE:" not in text
    assert "Call play." not in text
    assert "wait_for_opponent" not in text
    # 非轮到方
    waiting = describe_match(match, 1)
    assert "Status: waiting for P0." in waiting


def test_match_roundtrip_dict():
    match = _new_match()
    data = json.loads(json.dumps(match.to_dict(), ensure_ascii=False))
    from engine.match import Match

    restored = Match.from_dict(data)
    assert restored.to_dict() == match.to_dict()
    assert view_match(restored, 0)["view"] == view_match(match, 0)["view"]


def test_catalog_lists_only_playable_games():
    """The lobby must not offer an entry it cannot start (e.g. chess)."""
    kinds = [g["kind"] for g in games.catalog()]
    assert "gomoku" in kinds and "reversi" in kinds and "doudizhu" in kinds
    assert "chess" not in kinds
    assert all(g["ready"] for g in games.catalog())


def test_lineup_labels_follow_the_configured_identities():
    """Lineup copy is rendered from names, never from a hard-coded persona."""
    labels = {x["value"]: x["label"] for x in games.lineup_labels("汐祈（Seki）", "nori")}
    assert labels["nori_agent"] == "汐祈（Seki） + nori"
    assert labels["nori_bot"] == "汐祈（Seki） + 陪练"
    assert labels["agent_bot"] == "nori + 陪练（汐祈（Seki）队友）"
    assert labels["bot_bot"] == "两位陪练（汐祈（Seki）队友）"

    # A missing host name degrades to a neutral label, not to a persona name.
    fallback = {x["value"]: x["label"] for x in games.lineup_labels("", "")}
    assert fallback["nori_agent"] == f"{games.NEUTRAL_HOST_NAME} + {games.NEUTRAL_AGENT_NAME}"
