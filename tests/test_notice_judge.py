"""Notice judge tests: LOCAL key-event fast channel only (System1 removed).

Coverage:

- fixed-board win / block / open-four / four / double-three signals; ordinary
  nearby move counter-example;
- historical threats do not re-trigger the fast channel (before/after);
- judge tier: key event => speak + priority, otherwise quiet; mode off;
- human-only (AI reply silent), independent-match cooldown, priority 8s /
  normal 15s;
- terminal dedup, event snapshot does not leak the advanced board, plan
  review not overwritten by a new plan;
- one judge log line per human move (no token / chat text).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine import NewMatchOptions, NewSeat, create_match, status_of
from engine.games.gomoku import key_events
from host.settings import Settings
from notice_judge import Judgment, NoticeJudge
from room import RoomManager
from talk import TableTalk

SIZE = 15


# ----------------------------------------------------------------------
#  Fixed-board helpers
# ----------------------------------------------------------------------
def _board(moves: list[str], *, to_play: int) -> dict[str, Any]:
    from engine.coords import from_gtp

    cells = [0] * (SIZE * SIZE)
    for index, move in enumerate(moves):
        cells[from_gtp(move, SIZE)] = 1 if index % 2 == 0 else 2
    return {"size": SIZE, "cells": cells, "toPlay": to_play, "moves": len(moves)}


def _play(before: dict[str, Any], move: str, color: int) -> dict[str, Any]:
    from engine.coords import from_gtp

    cells = list(before["cells"])
    point = from_gtp(move, SIZE)
    assert point is not None
    cells[point] = color
    return {
        "size": SIZE,
        "cells": cells,
        "toPlay": 3 - color,
        "moves": int(before["moves"]) + 1,
        "last": point,
    }


def _seq_match(moves: list[str]) -> Any:
    """Play a legal sequence from an empty board; seats alternate."""
    from engine import apply_match_action

    match = create_match(
        NewMatchOptions(
            id="m1",
            kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t", name="yoru", joined=True, actor="u"),
                NewSeat(kind="ai", token=""),
            ],
            seed=1,
            now=1000,
            # Explicit legacy opponent semantics (agent driver): a bare ai seat
            # would migrate to turn, where talk yields to the director.
            mode_options={"mode": "nori", "driver": "agent"},
        )
    )
    for index, move in enumerate(moves):
        seat = status_of(match).waiting_on[0]
        result = apply_match_action(
            match, seat, {"type": "move", "move": move}, 1000 + index
        )
        assert result.ok, move
        match = result.match
    return match


def _event(match: Any, before: Any, seat: int, move: str) -> dict[str, Any]:
    return {
        "type": "move",
        "seat": seat,
        "move": move,
        "over": status_of(match).outcome is not None,
        "match_id": match.id,
        "version": match.version,
        "match": match,
        "before_state": before.state,
    }


class _FakeRoom:
    def __init__(self, match: Any) -> None:
        self.match = match
        self.status = "active"
        self.last_talk_ts = 0.0
        self.session: str | None = None
        self.page = False

    def status_info(self):
        return status_of(self.match)


class _FakeRepo:
    async def save(self, match: Any, status: str) -> None:
        return None


# ----------------------------------------------------------------------
#  Pure rules: fixed signals / ordinary move / new vs. old threats
# ----------------------------------------------------------------------
def test_key_events_win_now() -> None:
    before = _board(
        ["H8", "A1", "J8", "B1", "K8", "C1", "L8", "D1"], to_play=1
    )
    from engine.games.gomoku import gomoku

    result = gomoku.apply(
        {**before, "toPlay": 1, "cells": before["cells"]}, 0, "M8"
    )
    state = result.state
    assert state.get("winLine")
    flags = key_events(state, before=before, seat=0)
    assert flags["win_now"] is True and flags["key"] is True


def test_key_events_block_win_only_when_this_move_blocks() -> None:
    before = _board(
        ["A1", "H8", "B1", "J8", "C1", "K8", "D1", "L8"], to_play=1
    )
    blocked = _play(before, "M8", 1)
    assert key_events(blocked, before=before, seat=0)["block_win"] is True

    elsewhere = _play(before, "A5", 1)
    assert key_events(elsewhere, before=before, seat=0)["block_win"] is False


@pytest.mark.parametrize(
    "moves, move, expected_flag, absent_flag",
    [
        # Straight open four vs. a single (closed) four.
        (["H8", "A1", "J8", "B1", "K8", "C1"], "L8", "open_four", "four"),
        (["H8", "A1", "J8", "B1", "K8", "C1"], "M8", "four", "open_four"),
        # Diagonal open four.
        (["F6", "A1", "G7", "B1", "H8", "C1"], "J9", "open_four", "four"),
        # Double three is its own key signal, never a four.
        (
            ["J8", "A1", "K8", "B1", "H10", "C1", "H11", "D1"],
            "H8",
            "double_three",
            "four",
        ),
    ],
)
def test_key_events_threat_shapes(moves, move, expected_flag, absent_flag) -> None:
    before = _board(moves, to_play=1)
    flags = key_events(_play(before, move, 1), before=before, seat=0)
    assert flags[expected_flag] is True
    assert flags[absent_flag] is False
    assert flags["key"] is True


def test_key_events_ordinary_move_and_old_threat_are_not_key() -> None:
    # A nearby-but-harmless move is no key event.
    before = _board(["H8", "H10", "J8", "H11"], to_play=1)
    flags = key_events(_play(before, "H9", 1), before=before, seat=0)
    assert flags["key"] is False
    assert not any(
        flags[name] for name in ("win_now", "block_win", "open_four", "four")
    )

    # An existing open four does not re-trigger on every following move.
    before = _board(
        ["H8", "A1", "J8", "B1", "K8", "C1", "L8", "A2"], to_play=1
    )
    assert key_events(_play(before, "N8", 1), before=before, seat=0)["key"] is False


# ----------------------------------------------------------------------
#  Judge / TableTalk wiring (fake host: publish_notice records calls)
# ----------------------------------------------------------------------
class _SessionMgr:
    async def resolve_session(self, sid: str):
        return SimpleNamespace(session_id=sid)


class _RecordingCtx:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.session_mgr = _SessionMgr()

    async def publish_notice(self, session, chain, is_mentioned=True):
        text = "".join(getattr(item, "text", "") for item in chain)
        sid = getattr(session, "session_id", None) or str(session)
        self.sent.append((sid, text))


class _Plugin:
    def __init__(self, cfg: Settings, ctx: _RecordingCtx, data_dir: Any) -> None:
        self.settings = cfg
        self.ctx = ctx
        self.data_dir = str(data_dir)
        self.stopping = False
        self._talk: TableTalk | None = None

    async def notice_target(self, room: Any):
        session = getattr(room, "session", None)
        if session is None:
            human = next(s for s in room.match.seats if s.kind == "human")
            session = human.session_id
        if not session:
            return None
        return session, bool(getattr(room, "page", False))


def _cfg(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "page_chat_enabled": True,
        "page_chat_talk_default": "judge",
        "table_talk_default": "judge",
        "table_talk_prompt_template": "[System: {event} {board} {lane_hint}]",
        "table_talk_prompt_extra": "",
        "talk_min_interval_s": 15.0,
        "talk_cooldown_s": 30.0,
        "talk_key_every_n": 5,
        "talk_on_start": True,
        "talk_on_over": True,
        "notice_judge_mode": "key",
        "notice_key_min_interval_s": 8.0,
        "notice_judge_log": True,
    }
    base.update(overrides)
    return Settings(base)


def _wire(tmp_path: Any, cfg: Settings):
    ctx = _RecordingCtx()
    plugin = _Plugin(cfg, ctx, tmp_path)
    talk = TableTalk(plugin)
    plugin._talk = talk
    judge = NoticeJudge(plugin)
    talk.attach_judge(judge)
    return plugin, talk, judge, ctx


async def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def _evaluate(judge: NoticeJudge, match: Any, before: Any, move: str):
    room = _FakeRoom(match)
    return await judge._evaluate(room, _event(match, before, 0, move))


@pytest.mark.asyncio
async def test_fast_channel_speaks_on_key_event(tmp_path) -> None:
    _, _, judge, _ = _wire(tmp_path, _cfg())
    before = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1"])
    after = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1", "L8"])
    judgment = await _evaluate(judge, after, before, "L8")
    assert judgment.speak is True and judgment.fast is True
    assert judgment.priority is True
    assert "open_four" in judgment.key_flags


@pytest.mark.asyncio
async def test_ordinary_move_stays_quiet_without_network(tmp_path) -> None:
    _, _, judge, _ = _wire(tmp_path, _cfg())
    before = _seq_match(["H8", "A1", "J4"])
    after = _seq_match(["H8", "A1", "J4", "B7"])
    judgment = await _evaluate(judge, after, before, "B7")
    assert judgment.speak is False and judgment.fast is False
    assert judgment.reason == "no_key_event"


@pytest.mark.asyncio
async def test_judge_mode_off_skips(tmp_path) -> None:
    _, _, judge, _ = _wire(tmp_path, _cfg(notice_judge_mode="off"))
    before = _seq_match([])
    after = _seq_match(["H8"])
    judgment = await _evaluate(judge, after, before, "H8")
    assert judgment.speak is False and judgment.reason == "mode_off"
    assert judgment.fallback == "off"  # page judge tier -> TableTalk key fallback


@pytest.mark.asyncio
async def test_ai_move_is_never_judged_or_spoken(tmp_path) -> None:
    plugin, talk, judge, ctx = _wire(tmp_path, _cfg())
    before = _seq_match(["H8"])
    after = _seq_match(["H8", "A4"])  # white (AI) move
    event = _event(after, before, 1, "A4")
    talk.on_event(_FakeRoom(after), event)
    assert judge._pending == {}
    await asyncio.sleep(0)
    assert ctx.sent == []


@pytest.mark.asyncio
async def test_human_move_enters_judge_queue(tmp_path) -> None:
    _, talk, judge, _ = _wire(tmp_path, _cfg())
    before = _seq_match([])
    after = _seq_match(["H8"])
    talk.on_event(_FakeRoom(after), _event(after, before, 0, "H8"))
    assert judge._pending


@pytest.mark.asyncio
async def test_independent_matches_same_session_do_not_share_cooldown(tmp_path) -> None:
    """Two matches sharing a page session: judge cooldown must be per match."""
    _, talk, _, ctx = _wire(tmp_path, _cfg())
    before = _seq_match([])
    after = _seq_match(["H8"])
    from dataclasses import replace

    after_b = replace(after, id="m2")
    room_a = _FakeRoom(after)
    room_a.session = "web-shared"
    room_b = _FakeRoom(after_b)
    room_b.session = "web-shared"
    judgment = Judgment(True, priority=False, reason="key")
    talk.start()
    try:
        assert await talk.on_judged_event(room_a, _event(after, before, 0, "H8"), judgment) == "queued"
        assert await talk.on_judged_event(room_b, _event(after_b, before, 0, "H8"), judgment) == "queued"
        assert await _wait_until(lambda: len(ctx.sent) == 2)
    finally:
        await talk.stop()
    assert len(ctx.sent) == 2


@pytest.mark.asyncio
async def test_priority_uses_8s_normal_uses_15s(tmp_path) -> None:
    _, talk, _, ctx = _wire(tmp_path, _cfg())
    before = _seq_match([])
    after = _seq_match(["H8"])
    room = _FakeRoom(after)
    room.session = "private-a"
    event = _event(after, before, 0, "H8")
    normal = Judgment(True, priority=False, reason="key")
    high = Judgment(True, priority=True, reason="key")

    assert await talk.on_judged_event(room, event, normal) == "queued"
    talk.start()
    try:
        assert await _wait_until(lambda: len(ctx.sent) == 1)
        # 10s later: normal still inside the 15s match cooldown; high has
        # passed its 8s.
        room.last_talk_ts = time.time() - 10.0
        after2 = _seq_match(["H8", "A1", "J4"])
        event2 = _event(after2, _seq_match(["H8", "A1"]), 0, "J4")
        assert await talk.on_judged_event(room, event2, normal) == "cooldown"
        assert await talk.on_judged_event(room, event2, high) == "queued"
        assert await _wait_until(lambda: len(ctx.sent) == 2)
    finally:
        await talk.stop()
    assert len(ctx.sent) == 2


@pytest.mark.asyncio
async def test_terminal_move_and_over_event_dedup(tmp_path) -> None:
    _, talk, _, ctx = _wire(tmp_path, _cfg())
    match = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1", "L8", "D1", "M8"])
    assert status_of(match).outcome is not None
    room = _FakeRoom(match)
    room.session = "private-a"
    move_event = {
        "type": "move",
        "seat": 0,
        "move": "M8",
        "over": True,
        "match_id": match.id,
        "version": match.version,
        "match": match,
        "before_state": match.state,
    }
    over_event = {
        "type": "over",
        "seat": 0,
        "move": "M8",
        "over": True,
        "match_id": match.id,
        "version": match.version,
        "match": match,
    }
    assert await talk._dispatch(room, move_event) == "sent"
    assert await talk._dispatch(room, over_event) == "dedup"
    assert len(ctx.sent) == 1


@pytest.mark.asyncio
async def test_event_snapshot_does_not_read_advanced_board(tmp_path) -> None:
    ctx = _RecordingCtx()
    plugin = _Plugin(_cfg(), ctx, tmp_path)
    talk = TableTalk(plugin)
    plugin._talk = talk
    manager = RoomManager(repository=_FakeRepo(), config=_cfg(), now_fn=lambda: 1000)
    room, _seat, _token = await manager.create(
        kind="gomoku", actor="u", session_id="private-u", name="u", now=1000
    )
    captured: list[dict[str, Any]] = []
    room.event_hook = lambda _room, event: captured.append(dict(event))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=room.match.version)
    await asyncio.sleep(0)
    assert captured
    human_event = captured[-1]
    assert human_event["match"].log[-1].move == "H8"

    # Event executes late: the board has already advanced with the AI reply.
    await room.apply_ai_move(1, "A4")
    assert room.match.log[-1].move == "A4"

    match = human_event["match"]
    fields = talk._fields(match, human_event, status_of(match))
    assert "H8" in fields["event"]
    assert "第 1 手" in fields["board"]
    assert "A4" not in fields["event"]


@pytest.mark.asyncio
async def test_judge_log_one_entry_per_human_move_without_secrets(tmp_path) -> None:
    plugin, talk, judge, ctx = _wire(tmp_path, _cfg())
    before = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1"])
    after = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1", "L8"])
    room = _FakeRoom(after)
    room.session = "private-a"
    talk.start()
    try:
        await judge._process(room, _event(after, before, 0, "L8"))
        assert await _wait_until(lambda: len(ctx.sent) == 1)
    finally:
        await talk.stop()

    path = tmp_path / "notice_judge_log.jsonl"
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "queued"
    assert entry["speak"] is True
    assert entry["move"] == "L8"
    for key in ("token", "chat", "state_text"):
        assert key not in entry
    assert len(ctx.sent) == 1


@pytest.mark.asyncio
async def test_stop_cancels_worker_and_clears_pending(tmp_path) -> None:
    _, _, judge, _ = _wire(tmp_path, _cfg())
    judge.start()
    before = _seq_match([])
    after = _seq_match(["H8"])
    judge.on_event(_FakeRoom(after), _event(after, before, 0, "H8"))
    await asyncio.sleep(0)
    await judge.stop()
    assert judge.running is False
    assert judge._pending == {}


@pytest.mark.asyncio
async def test_page_game_human_event_routes_to_judge_end_to_end(tmp_path) -> None:
    cfg = _cfg(talk_min_interval_s=0.0)
    ctx = _RecordingCtx()
    plugin = _Plugin(cfg, ctx, tmp_path)
    plugin.page_session_id = "web-u-bot-token"
    talk = TableTalk(plugin)
    plugin._talk = talk
    judge = NoticeJudge(plugin)
    talk.attach_judge(judge)
    manager = RoomManager(repository=_FakeRepo(), config=cfg, now_fn=lambda: 1000)
    manager.event_hook = talk.on_event
    room, _seat, _token = await manager.create(
        kind="gomoku",
        actor="u",
        session_id=plugin.page_session_id,
        name="u",
        now=1000,
        mode_options={"mode": "nori", "driver": "agent"},
    )
    judge.start()
    talk.start()
    try:
        # Play up to an open-four key move (L8) for the human seat.
        for move in ["H8", "A1", "J8", "B1", "K8", "C1"]:
            seat = status_of(room.match).waiting_on[0]
            await room.apply(
                seat, {"type": "move", "move": move}, client_version=room.match.version
            )
        await room.apply(
            0, {"type": "move", "move": "L8"}, client_version=room.match.version
        )
        # The AI reply must not trigger a separate notice.
        await room.apply_ai_move(1, "A4")
        log_path = tmp_path / "notice_judge_log.jsonl"
        for _ in range(100):
            if log_path.is_file() and log_path.read_text(encoding="utf-8").strip():
                break
            await asyncio.sleep(0.01)
        assert await _wait_until(lambda: any("L8" in text for _sid, text in ctx.sent))
        entries = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    finally:
        await judge.stop()
        await talk.stop()

    moves = [entry["move"] for entry in entries]
    # Per-match coalescing keeps only the latest human move; the AI reply is
    # never judged at all.
    assert moves[-1] == "L8"
    assert "A4" not in moves
    assert set(moves) <= {"H8", "J8", "K8", "L8"}
    assert entries[-1]["status"] == "queued"
    texts = [text for _sid, text in ctx.sent]
    assert any("L8" in text for text in texts)
    assert not any("A4" in text for text in texts)


@pytest.mark.asyncio
async def test_key_priority_marks_current_plan_review(tmp_path) -> None:
    _, talk, judge, ctx = _wire(tmp_path, _cfg())
    manager = RoomManager(repository=_FakeRepo(), config=_cfg(), now_fn=lambda: 1000)
    room, _seat, _token = await manager.create(
        kind="gomoku", actor="u", session_id="private-u", name="u", now=1000
    )
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=room.match.version)
    await room.apply(1, {"type": "move", "move": "A1"}, client_version=room.match.version)
    await room.apply(0, {"type": "move", "move": "J8"}, client_version=room.match.version)
    await room.apply(1, {"type": "move", "move": "B1"}, client_version=room.match.version)
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await room.apply(1, {"type": "move", "move": "C1"}, client_version=room.match.version)
    before_move = room.match
    await room.apply(0, {"type": "move", "move": "L8"}, client_version=room.match.version)
    snapshot = room.match  # judge's move-time snapshot (no plan yet)
    await room.set_plan(1, text="新策略", ttl_moves=10, ttl_s=120.0)
    version = room.match.version
    seq = room.match.seats[1].plan["seq"]

    await judge._process(
        room,
        {
            "type": "move",
            "seat": 0,
            "move": "L8",
            "match": snapshot,
            "before_state": before_move.state,
        },
    )

    plan = room.match.seats[1].plan
    assert plan["text"] == "新策略"
    assert plan["seq"] == seq
    assert plan["review"] is True
    assert room.match.version == version
    assert talk._queue.qsize() == 1  # text queued; the talk worker sends it


@pytest.mark.asyncio
async def test_queued_text_dropped_if_room_ends_before_send(tmp_path) -> None:
    _, talk, _, ctx = _wire(tmp_path, _cfg())
    before = _seq_match([])
    after = _seq_match(["H8"])
    room = _FakeRoom(after)
    room.session = "private-a"
    judgment = Judgment(True, priority=False, reason="key")
    assert await talk.on_judged_event(
        room, _event(after, before, 0, "H8"), judgment
    ) == "queued"

    terminal = _seq_match(
        ["H8", "A1", "J8", "B1", "K8", "C1", "L8", "D1", "M8"]
    )
    room.match = terminal
    room.status = "over"
    # The terminal event itself must not be dropped (bypasses the judge queue).
    talk.on_event(
        room,
        {
            "type": "move",
            "seat": 0,
            "move": "M8",
            "over": True,
            "match_id": terminal.id,
            "version": terminal.version,
            "match": terminal,
        },
    )

    talk.start()
    try:
        assert await _wait_until(lambda: len(ctx.sent) == 1)
    finally:
        await talk.stop()
    texts = [text for _sid, text in ctx.sent]
    assert len(texts) == 1
    assert "对局结束" in texts[0]  # terminal, not the stale pre-terminal H8 move


# ----------------------------------------------------------------------
#  2026-11-16 remediation regressions.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_judge_mode_behaves_like_the_off_default(tmp_path) -> None:
    """P2: an explicitly cleared notice_judge_mode must NOT fail-open to key."""
    _, _, judge, _ = _wire(tmp_path, _cfg(notice_judge_mode=""))
    before = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1"])
    after = _seq_match(["H8", "A1", "J8", "B1", "K8", "C1", "L8"])
    judgment = await _evaluate(judge, after, before, "L8")
    assert judgment.speak is False
    assert judgment.fallback == "off"
    assert judgment.reason == "mode_off"


@pytest.mark.asyncio
async def test_judgement_log_rotates_past_the_size_cap(tmp_path) -> None:
    """P2: the jsonl judgement log rotates once instead of growing forever."""
    import notice_judge as module
    from notice_judge import _append_line

    path = Path(str(tmp_path)) / "notice_judge_log.jsonl"
    old_cap = module._JSONL_MAX_BYTES
    try:
        module._JSONL_MAX_BYTES = 200
        for index in range(20):
            _append_line(path, json.dumps({"n": index}) * 3 + "\n")
    finally:
        module._JSONL_MAX_BYTES = old_cap
    rotated = path.with_name(path.name + ".1")
    assert rotated.exists(), "oversized log must have been rotated"
    assert path.stat().st_size <= 200 + 200  # at most one entry past the cap
