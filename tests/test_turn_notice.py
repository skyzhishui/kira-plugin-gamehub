"""Action-turn notice wording (``director.build_turn_notice``) — confirmed shape.

The notice is a short public event in one fixed shape:

- chess: ``对局通知 · N 手：<mover> 刚落子 <move>，现在轮到你（<agent>，
  执行<label>）。盘面与规则见本轮 gamehub_context 注入块；…``
- doudizhu: ``对局通知 · 斗地主第<N>局：<situation>，现在轮到你（…）。
  局势与规则见本轮 gamehub_context 注入块；…`` — the situation is the most
  recent public move/pass with its actor and concrete card faces (no action
  prompts); the landlord-lead/re-lead facts stay.
- a delegated grant folds a short ``<agent> 代 <human>`` clause into the
  turn phrase; no seat definition and no second identity sentence.

Everything else — the action contract (tag syntax, format, example,
feedback), rules, board, speech guidance — rides the ephemeral
``gamehub_context`` block (``context.build_game_state_context``).

Threat wording (body-only coordinates do not count / the system plays for
you / only one tag per reply) is deliberately gone, not rephrased:
corrections flow through the bounded retry notices plus the block's
requirement section.
"""
from __future__ import annotations

from types import SimpleNamespace

from director import build_nudge_notice, build_stage_notice, build_turn_notice
from engine import games
from engine.match import LogEntry, Match, SeatInfo


def _plugin(host_name: str = "汐祈（Seki）", agent_name: str = "nori"):
    return SimpleNamespace(
        host_agent_name=host_name,
        settings=SimpleNamespace(agent_name=agent_name),
    )


def _doudizhu_match(state_overrides: dict | None = None):
    """A three-seat doudizhu match (human + two AI seats)."""
    module = games.get("doudizhu")
    ctx = SimpleNamespace(seed=1, options={"hands": "1"})
    state = module.create(ctx)
    for key, value in (state_overrides or {}).items():
        state[key] = value
    seats = [
        SeatInfo(kind="human", name="Human", token=""),
        SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        SeatInfo(kind="ai", name="nori", token=""),
    ]
    return Match(
        id="m1", kind="doudizhu", created_at=0, updated_at=0, version=1,
        seats=seats, state=state,
    )


def _gomoku_match(match_id: str, *, version: int = 1, moves=()):
    module = games.get("gomoku")
    state = module.create(SimpleNamespace(seed=1, options={}))
    log = []
    for index, (seat, move) in enumerate(moves):
        result = module.apply(state, seat, move)
        assert getattr(result, "state", None) is not None
        state = result.state
        log.append(LogEntry(seat=seat, move=move, t=index + 1))
    return Match(
        id=match_id, kind="gomoku", created_at=0, updated_at=0, version=version,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
        log=log,
    )


def _assert_short_shape(
    text: str,
    *,
    head_prefix: str,
    pointer_prefix: str,
    state_tool: str,
    turn_clause: str,
) -> None:
    """The confirmed header shape: one event line + one folded turn clause +
    the fallback pointer; nothing else."""
    assert text.startswith("[System: " + head_prefix)
    assert f"{pointer_prefix}见本轮 gamehub_context 注入块" in text
    assert "若缺失，先调用" in text and state_tool in text
    assert text.count(turn_clause) == 1
    assert text.count("轮到你") == 1  # the turn phrase is folded, never repeated
    # No second identity sentence and no seat definition in the notice.
    assert "你是座位" not in text
    assert "已把这一手委托" not in text
    assert "座位0" not in text and "座位1" not in text and "座位2" not in text
    # The threat paragraph is gone (not rephrased) and no action prompts.
    for token in (
        "系统会替你随手",
        "一次回复只输出一个落子标签",
        "只在正文里写",
        "不算数",
        "压过",
        "轮到你叫分",
        "或不要",
    ):
        assert token not in text
    # Rules / board / format / example / speech guidance live in the block.
    assert "规则（完整）" not in text
    assert "完整局面" not in text
    assert "bid 1" not in text
    assert "符合你人设的发言" not in text
    assert "表情包" not in text


# ----------------------------------------------------------------------
#  Doudizhu
# ----------------------------------------------------------------------
def test_doudizhu_notice_uses_the_confirmed_header_shape():
    text = build_turn_notice(
        _doudizhu_match({"bids": [{"seat": 0, "bid": 2}], "turn": 1}),
        seat=1,
        kind="nori_turn",
        move_count=0,
        tool_name="play_m1",
        plugin=_plugin(),
    )
    assert text.startswith("[System: 对局通知 · 斗地主第1局：")
    assert "斗地主行动回合" not in text  # the old head prefix is gone
    # The folded actor clause carries the host agent name and the seat label
    # (the label comes from the engine: the waiting bidder is 叫分中).
    assert "现在轮到你（汐祈（Seki），执叫分中）。" in text
    _assert_short_shape(
        text,
        head_prefix="对局通知 · 斗地主第1局：",
        pointer_prefix="局势与规则",
        state_tool="state_m1",
        turn_clause="现在轮到你（汐祈（Seki），执叫分中）",
    )


def test_doudizhu_situation_facts_are_public_and_concrete():
    first = build_turn_notice(
        _doudizhu_match(), seat=1, kind="nori_turn", move_count=0,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "斗地主第1局：首次叫分，现在轮到你" in first

    prev = build_turn_notice(
        _doudizhu_match({"bids": [{"seat": 0, "bid": 2}]}),
        seat=1, kind="nori_turn", move_count=0,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "上一家 Human 叫 2 分，现在轮到你" in prev

    prev_pass = build_turn_notice(
        _doudizhu_match({"bids": [{"seat": 0, "bid": 0}]}),
        seat=1, kind="nori_turn", move_count=0,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "上一家 Human 不叫" in prev_pass

    redeal = build_turn_notice(
        _doudizhu_match({"bids": [], "redeals": 1}),
        seat=1, kind="nori_turn", move_count=0,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "三家都不叫，已重新发牌" in redeal


def test_doudizhu_play_facts_show_the_recent_public_move():
    """Follow: the most recent public action with actor + concrete faces —
    no combo-name hiding, no "压过或不要" action prompt."""
    played = build_turn_notice(
        _doudizhu_match({
            "phase": "play", "landlord": 0, "passes": 0, "handNo": 2,
            "plays": [
                {"seat": 0, "cards": ["H9", "S9"], "name": "对子"},
            ],
            "trick": {"seat": 0, "name": "对子", "cards": ["H9", "S9"]},
        }),
        seat=1, kind="nori_turn", move_count=3,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "斗地主第2局：" in played  # the hand number rides the head
    assert "Human 出了 9 9，现在轮到你" in played
    assert "「对子」" not in played  # combo name never hides the faces

    after_pass = build_turn_notice(
        _doudizhu_match({
            "phase": "play", "landlord": 0, "passes": 1,
            "plays": [
                {"seat": 0, "cards": ["H3"], "name": "单张"},
                {"seat": 1, "cards": None, "name": "不要"},
            ],
            "trick": {"seat": 0, "name": "单张", "cards": ["H3"]},
        }),
        seat=2, kind="nori_turn", move_count=4,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "汐祈（Seki） 不要，现在轮到你" in after_pass

    lead = build_turn_notice(
        _doudizhu_match({"phase": "play", "landlord": 1, "trick": None,
                         "passes": 0, "plays": []}),
        seat=1, kind="nori_turn", move_count=2,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "地主先出，现在轮到你" in lead  # the lead fact stays

    relead = build_turn_notice(
        _doudizhu_match({"phase": "play", "landlord": 0, "trick": None,
                         "passes": 0, "plays": [{"seat": 0, "cards": ["H3"]}]}),
        seat=1, kind="nori_turn", move_count=5,
        tool_name="play_m1", plugin=_plugin(),
    )
    assert "两家都不要，重新领出，现在轮到你" in relead


def test_doudizhu_delegated_identity_is_one_short_clause():
    text = build_turn_notice(
        _doudizhu_match(), seat=0, kind="delegated_human", move_count=0,
        tool_name="play_m1", plugin=_plugin(),
    )
    # The delegated identity is one clause inside the turn phrase; the seat
    # label comes from the engine (the first bidder is 叫分中).
    assert "现在轮到你（汐祈（Seki） 代 Human，执叫分中）。" in text
    assert "座位0" not in text and "已把这一手委托" not in text
    _assert_short_shape(
        text,
        head_prefix="对局通知 · 斗地主第1局：",
        pointer_prefix="局势与规则",
        state_tool="state_m1",
        turn_clause="现在轮到你（汐祈（Seki） 代 Human，执叫分中）",
    )


# ----------------------------------------------------------------------
#  Chess (gomoku / reversi)
# ----------------------------------------------------------------------
def test_gomoku_notice_uses_the_confirmed_header_shape():
    match = _gomoku_match("m2", moves=[(0, "H8")])
    text = build_turn_notice(
        match, seat=1, kind="nori_turn", move_count=1, tool_name="play_m2",
        plugin=_plugin(),
    )
    assert text.startswith("[System: 对局通知 · 1 手：")
    # One clause: mover fact + folded actor (host agent name + seat label).
    assert "Human 刚落子 H8，现在轮到你（汐祈（Seki），执白）。" in text
    _assert_short_shape(
        text,
        head_prefix="对局通知 · 1 手：",
        pointer_prefix="盘面与规则",
        state_tool="state_m2",
        turn_clause="现在轮到你（汐祈（Seki），执白）",
    )


def test_chess_first_move_folds_the_actor_into_one_clause():
    match = _gomoku_match("m-first")
    text = build_turn_notice(
        match, seat=0, kind="nori_turn", move_count=0, tool_name="play_mf",
        plugin=_plugin(),
    )
    assert "对局通知 · 0 手：对局开始，现在轮到你（汐祈（Seki），执黑）先行。" in text
    _assert_short_shape(
        text,
        head_prefix="对局通知 · 0 手：",
        pointer_prefix="盘面与规则",
        state_tool="state_mf",
        turn_clause="现在轮到你（汐祈（Seki），执黑）先行",
    )


def test_every_turn_notice_has_the_same_short_shape():
    """First and later turns share one identical short shape: event + folded
    actor clause + pointer; no rules/board differences."""
    first = build_turn_notice(
        _gomoku_match("m3", version=1, moves=[(0, "H8")]),
        seat=1, kind="nori_turn", move_count=1, tool_name="play_m3",
        plugin=_plugin(),
    )
    later = build_turn_notice(
        _gomoku_match("m3", version=3, moves=[(0, "H8"), (1, "J9"), (0, "K8")]),
        seat=1, kind="nori_turn", move_count=3, tool_name="play_m3",
        plugin=_plugin(),
    )
    for text, clause in (
        (first, "现在轮到你（汐祈（Seki），执白）"),
        (later, "现在轮到你（汐祈（Seki），执白）"),
    ):
        _assert_short_shape(
            text,
            head_prefix="对局通知 · ",
            pointer_prefix="盘面与规则",
            state_tool="state_m3",
            turn_clause=clause,
        )
    assert "刚落子" in first and "刚落子" in later
    assert "K8" in later  # the latest trigger fact


def test_reversi_auto_pass_continuation_keeps_one_event_line():
    """Auto-pass continuation: one event line explains the consecutive turn
    without blaming the opponent."""
    module = games.get("reversi")
    state = module.create(SimpleNamespace(seed=1, options={}))
    state = dict(state)
    state["passed"] = 2
    match = Match(
        id="m-rev", kind="reversi", created_at=0, updated_at=0, version=2,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
        log=[LogEntry(seat=0, move="D3", t=1)],
    )
    text = build_turn_notice(
        match, seat=0, kind="nori_turn", move_count=1, tool_name="play_mrev",
        plugin=_plugin(),
    )
    assert "你刚落子 D3；对手无合法落点已自动过手，仍轮到你（汐祈（Seki），执" in text
    _assert_short_shape(
        text,
        head_prefix="对局通知 · 1 手：",
        pointer_prefix="盘面与规则",
        state_tool="state_mrev",
        turn_clause="仍轮到你（汐祈（Seki）",
    )


def test_delegated_chess_notice_keeps_a_short_identity_clause():
    text = build_turn_notice(
        _gomoku_match("m5", moves=[(0, "H8"), (1, "C3")]),
        seat=0, kind="delegated_human", move_count=2, tool_name="play_m5",
        plugin=_plugin(),
    )
    assert "C3，现在轮到你（汐祈（Seki） 代 Human，执黑）。" in text
    assert "座位0" not in text and "已把这一手委托" not in text
    _assert_short_shape(
        text,
        head_prefix="对局通知 · 2 手：",
        pointer_prefix="盘面与规则",
        state_tool="state_m5",
        turn_clause="现在轮到你（汐祈（Seki） 代 Human，执黑）",
    )


# ----------------------------------------------------------------------
#  Retry / stage notices
# ----------------------------------------------------------------------
def test_nudge_notice_is_state_plus_pointer_only():
    """F1/F2 slimming: the retry notice states the status and points at the
    block; the move format/example lives in gamehub_context."""
    text = build_nudge_notice(
        _gomoku_match("m4"), seat=1, tool_name="play_m4", plugin=_plugin()
    )
    assert "还没有提交动作" in text
    assert "盘面与规则见本轮 gamehub_context 注入块" in text
    assert "state_m4" in text
    # The example/threat moved to the block — none of it stays here.
    assert "<play_m4>" not in text
    assert "bid 1" not in text and "H9" not in text
    assert "系统会替你随手" not in text
    assert "只在正文里写" not in text

    # Text-only variant (F1): names the exact mistake, same short shape.
    corrective = build_nudge_notice(
        _gomoku_match("m4"), seat=1, tool_name="play_m4", plugin=_plugin(),
        text_only=True,
    )
    assert "刚才只发了文字" in corrective
    assert "没有提交动作" in corrective
    assert "<play_m4>" not in corrective


def test_doudizhu_nudge_carries_no_example():
    text = build_nudge_notice(
        _doudizhu_match(), seat=1, tool_name="play_m1", plugin=_plugin()
    )
    # Bidding-phase examples (bid 1 / pass) live in the context block only.
    assert "bid 1" not in text and "pass" not in text
    assert "state_m1" in text


def test_stage_notices_are_short_events_without_authorization():
    """Start/end: event + pointer only; the end never asks for a move."""
    start_match = _gomoku_match("m-stage", moves=[(0, "H8")])
    start = build_stage_notice(start_match, "start", plugin=_plugin())
    assert start.startswith("[System: 对局开始 · 五子棋")
    assert "执黑" in start and "执白" in start
    assert "play" not in start
    assert "gamehub_context" in start

    # Finish a real match so status_of carries an outcome.
    module = games.get("gomoku")
    state = module.create(SimpleNamespace(seed=1, options={}))
    moves = [(0, "H8"), (1, "C3"), (0, "J8"), (1, "C4"), (0, "K8"),
             (1, "C5"), (0, "L8"), (1, "C6"), (0, "M8")]
    log = []
    for index, (seat, move) in enumerate(moves):
        result = module.apply(state, seat, move)
        assert getattr(result, "state", None) is not None
        state = result.state
        log.append(LogEntry(seat=seat, move=move, t=index + 1))
    done = Match(
        id="m-over", kind="gomoku", created_at=0, updated_at=0, version=12,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
        log=log,
    )
    end = build_stage_notice(done, "over", plugin=_plugin())
    assert end.startswith("[System: 对局结束 · 五子棋")
    assert "轮到你" not in end
    assert "落子" not in end
    assert "gamehub_context" in end
