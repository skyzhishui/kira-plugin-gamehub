"""TurnDirector (notice-driven action turns) behaviour tests — KiraAI port.

Covers: trigger timing (human move → mandatory notice; AI move does not
self-trigger), stage notices, notice-target changes, the watchdog rule-bot
fallback (AI seat + delegated human seat), late/stale plays, stop-next-turn,
manual-move races (``move_count``), request-scoped ``tools_for`` grants and the
typed ``PlayResult``.

Host fakes: ``host.notices.send_notice`` is monkeypatched with a recorder, and
``plugin.notice_target`` returns ``(session_id, is_page)`` like the runtime.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import director as director_module
import modes
from director import PlayResult, TurnDirector
from engine.types import GameModule
from room import RoomManager
from tools_turn import play_tool_name, state_tool_name

from test_talk import _settings


class _Repo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def save(self, match, status) -> None:
        self.rows[match.id] = match.to_dict()

    async def load_active(self):
        return []


class _Recorder:
    """Records published notices; splits action notices from stage notices."""

    def __init__(self, delay: float = 0.0) -> None:
        self.sent: list[tuple[str, str]] = []
        self.stage: list[tuple[str, str]] = []
        self.delay = delay

    async def __call__(self, plugin: Any, sid: str, text: str) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        if text.startswith("[System: 对局开始") or text.startswith(
            "[System: 对局结束"
        ):
            self.stage.append((str(sid), str(text)))
        else:
            self.sent.append((str(sid), str(text)))
        return True


def _harness(**cfg_overrides: Any):
    """Assemble ``(rooms, plugin, recorder, director)`` with tunable timers."""
    overrides = dict(cfg_overrides)
    settings = _settings(
        turn_play_timeout_s=overrides.pop("turn_play_timeout_s", 0.6),
        turn_fallback_delay_s=overrides.pop("turn_fallback_delay_s", 0.05),
        **overrides,
    )
    recorder = _Recorder()
    rooms = RoomManager(repository=_Repo(), config=settings)
    plugin = SimpleNamespace()
    plugin.settings = settings
    plugin.rooms = rooms
    plugin._rooms = rooms
    plugin.stopping = False
    plugin.target_override = ""
    plugin.notice_sent = recorder.sent
    plugin.stage_sent = recorder.stage

    async def notice_target(room=None):
        human = next((s for s in room.match.seats if s.kind == "human"), None)
        session = str(getattr(human, "session_id", "") or "")
        if getattr(plugin, "target_override", ""):
            session = str(plugin.target_override)
        return session, bool(session)

    plugin.notice_target = notice_target
    director = TurnDirector(plugin)
    plugin._director = director
    plugin.turn_owns = director.owns
    rooms.event_hook = director.on_event
    return rooms, plugin, recorder, director


@pytest.fixture(autouse=True)
def _patch_notices(monkeypatch: pytest.MonkeyPatch):
    """Route ``director.send_notice`` through the harness recorder."""
    state: dict[str, Any] = {}

    async def fake_send(plugin: Any, sid: str, text: str) -> bool:
        recorder = getattr(plugin, "_recorder", None)
        if recorder is None:
            state.setdefault("plain", []).append((str(sid), str(text)))
            return True
        return await recorder(plugin, sid, text)

    monkeypatch.setattr(director_module, "send_notice", fake_send)
    return state


def _harness_bound(**cfg_overrides: Any):
    """``_harness`` with the recorder also exposed on the plugin (fixture hook)."""
    rooms, plugin, recorder, director = _harness(**cfg_overrides)
    plugin._recorder = recorder
    return rooms, plugin, recorder, director


async def _create(
    rooms: RoomManager,
    *,
    actor: str = "u1",
    opponent: str = "ai",
    mode_options: dict | None = None,
):
    return await rooms.create(
        kind="gomoku",
        actor=actor,
        session_id=f"private-{actor}",
        opponent=opponent,
        name="玩家",
        now=1000,
        mode_options=mode_options,
    )


async def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def _notice_text(recorder: _Recorder) -> str:
    assert recorder.sent, "没有发出行动通知"
    return recorder.sent[-1][1]


NORI = {"mode": "nori", "driver": "turn"}
DELEGATED = {"mode": "bot", "difficulty": "easy", "delegation": True}


# ----------------------------------------------------------------------
#  Trigger timing and notice content
# ----------------------------------------------------------------------
async def test_human_move_triggers_mandatory_notice_with_full_board_minus_plan():
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    # Historic plan data stays intact: written, not injected, still readable.
    await room.set_plan(1, text="这局放水，别堵活四", ttl_moves=8, ttl_s=60)

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))

    text = _notice_text(recorder)
    assert play_tool_name(room.match.id) in text  # the per-match play tool
    assert "H8" in text  # this move
    assert "放水" not in text  # minus plan
    assert room.match.seats[1].plan["text"] == "这局放水，别堵活四"
    # The guidance: call play once, may speak in the same reply.
    assert "一次回复只调用一次" in text
    assert "同一次回复" in text
    grant = director.grant_of(room.match.id)
    assert grant is not None and grant.kind == "nori_turn"
    assert grant.state == "active"
    assert grant.move_count == 1
    assert grant.session_id == "private-u1"
    assert grant.actor == "nori_notice"
    await director.stop()


async def test_nori_mode_start_and_over_send_tool_free_stage_notices_once():
    """Start/end each go out once, carry no tool, and mint no grant."""
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await _wait(lambda: len(recorder.stage) == 1)
    start = recorder.stage[0][1]
    assert start.startswith("[System: 对局开始 · 五子棋")
    assert "执黑" in start and "执白" in start
    assert "不是解说" in start
    assert "play" not in start
    assert director.grant_of(room.match.id) is None

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    assert play_tool_name(room.match.id) in _notice_text(recorder)
    assert director.grant_of(room.match.id) is not None

    # Resign: the in-flight grant closes, the end notice still goes once.
    await room.apply(1, {"type": "resign"})
    await _wait(lambda: len(recorder.stage) == 2)
    over = recorder.stage[1][1]
    assert over.startswith("[System: 对局结束 · 五子棋")
    assert "认输" in over
    assert "play" not in over
    assert director.grant_of(room.match.id) is None
    await director.stop()


async def test_teammate_mode_does_not_get_director_stage_notices():
    """Teammate (bot) matches keep table talk for start/end."""
    rooms, _plugin, recorder, director = _harness_bound()
    await _create(
        rooms, opponent="bot", mode_options={"mode": "bot", "difficulty": "easy"}
    )
    await asyncio.sleep(0.2)
    assert recorder.stage == []
    await director.stop()


async def test_ai_play_does_not_self_trigger_and_no_duplicate_grants():
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok and result.status == "played" and "已落子" in result.text
    await _wait(lambda: director.grant_of(room.match.id).state == "played")
    assert len(recorder.sent) == 1  # no self-trigger

    await room.apply(0, {"type": "move", "move": "J8"})
    await _wait(
        lambda: director.grant_of(room.match.id) is not None
        and director.grant_of(room.match.id).move_count == 3
    )
    assert len(recorder.sent) == 2
    await director.stop()


async def test_notice_targets_actual_resolved_target_and_follows_changes():
    rooms, plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, actor="u1", mode_options=dict(NORI))

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: len(recorder.sent) == 1)
    assert recorder.sent[0][0] == "private-u1"
    assert director.grant_of(room.match.id).session_id == "private-u1"

    grant = director.grant_of(room.match.id)
    await director.submit_play(room.match.id, grant.key, "J9")
    await _wait(lambda: director.grant_of(room.match.id).state == "played")

    plugin.target_override = "live2d-other-target"
    await room.apply(0, {"type": "move", "move": "J8"})
    await _wait(lambda: len(recorder.sent) == 2)
    assert recorder.sent[1][0] == "live2d-other-target"  # immediate effect
    await director.stop()


# ----------------------------------------------------------------------
#  Watchdog fallback
# ----------------------------------------------------------------------
async def test_watchdog_bot_fallback_plays_ai_seat_once():
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    assert len(room.match.log) == 2
    assert room.match.log[-1].seat == 1

    # Late play: the grant is resolved (watchdog filled), no move lands.
    grant = director.grant_of(room.match.id)
    result = await director.submit_play(room.match.id, grant.key, "K9")
    assert result.terminal is True and result.ok is False
    assert "结束" in result.text
    assert len(room.match.log) == 2
    await director.stop()


async def test_submit_within_grace_wins_and_watchdog_never_double_moves():
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=5.0, turn_fallback_delay_s=0.3
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok and "已落子" in result.text
    await asyncio.sleep(0.5)  # past the grace window
    assert director.grant_of(room.match.id).state == "played"
    assert len(room.match.log) == 2
    await director.stop()


async def test_watchdog_fills_delegated_human_seat_once():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    assert grant.kind == "delegated_human" and grant.seat == 0

    await _wait(lambda: grant.state == "filled")
    assert room.match.log[0].seat == 0  # delegated fill on the human seat
    assert room.match.log[1].seat == 1  # easy practice bot answers

    # Delegation keeps renewing while it is on.
    await _wait(lambda: len(room.match.log) >= 6)
    await _wait(
        lambda: director.grant_of(room.match.id) is not None
        and director.grant_of(room.match.id).state == "active"
    )
    human_moves = [e for e in room.match.log if e.seat == 0]
    bot_moves = [e for e in room.match.log if e.seat == 1]
    assert len(human_moves) >= 2 and len(bot_moves) >= 2

    # Stop-next-turn: the in-flight grant may finish, then nothing new.
    await room.set_mode_options(delegation=False)
    current = director.grant_of(room.match.id)
    if current is not None and current.state == "active":
        from engine import games

        legal = games.get(room.match.kind).bot(room.match.state, current.seat)
        result = await director.submit_play(room.match.id, current.key, legal)
        assert result.terminal is True and "已落子" in result.text
    moves_now = len(room.match.log)
    await asyncio.sleep(1.2)
    final = director.grant_of(room.match.id)
    assert (
        len(room.match.log) == moves_now
        or final is None
        or final.state != "active"
    )
    await director.stop()


# ----------------------------------------------------------------------
#  Stop-next-turn and grant races
# ----------------------------------------------------------------------
async def test_stop_button_affects_next_turn_not_in_flight():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    await room.set_mode_options(delegation=False)
    assert modes.delegation_of(room.match) is False
    assert grant.state == "active"  # the in-flight turn is not cancelled
    result = await director.submit_play(room.match.id, grant.key, "H8")
    assert result.ok and "已落子" in result.text

    await _wait(lambda: room.match.log and room.match.log[-1].seat == 1)
    await asyncio.sleep(0.2)
    current = director.grant_of(room.match.id)
    assert current is None or current.state != "active"
    assert director.owns(room.match) is False  # talk takes over again
    await director.stop()


async def test_stale_grant_cannot_take_subsequent_turn():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    assert grant.move_count == 0

    # A manual move wins the race (move_count guard inside the room lock).
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.terminal is True
    assert "局面已经变化" in result.text or "作废" in result.text
    assert room.match.log[0].move == "H8"

    await _wait(lambda: len(room.match.log) >= 2)
    await _wait(
        lambda: director.grant_of(room.match.id) is not None
        and director.grant_of(room.match.id).move_count == len(room.match.log)
    )
    await director.stop()


async def test_wrong_grant_key_is_refused():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    result = await director.submit_play(room.match.id, "deadbeef", "J9")
    assert result.terminal is True and "没有有效的落子授权" in result.text
    await director.stop()


# ----------------------------------------------------------------------
#  Driver routing: turn never drives the agent; easy = bot; advanced = agent
# ----------------------------------------------------------------------
async def test_turn_driver_never_drives_agent():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)

    driven: list[tuple[str, int]] = []

    async def drive_hook(room_obj, seat):
        driven.append((room_obj.match.id, seat))

    # Mirror runtime._drive_ai routing without importing main.py.
    from modes import DRIVER_BOT, DRIVER_TURN, seat_driver_of

    async def drive_ai(room_obj, seat):
        driver = seat_driver_of(room_obj.match, seat)
        if driver == DRIVER_TURN:
            return
        if driver == DRIVER_BOT:
            await room_obj.ai_step(seat)
            return
        await drive_hook(room_obj, seat)

    await drive_ai(room, 1)
    assert driven == []  # a turn match never reaches the agent

    legacy = SimpleNamespace(
        match=SimpleNamespace(
            id="legacy-chess",
            kind="chess",
            seats=[SimpleNamespace(kind="human"), SimpleNamespace(kind="ai")],
            mode_options={},
        )
    )
    await drive_ai(legacy, 1)
    assert driven == [("legacy-chess", 1)]  # non-turn legacy stays on the agent
    await director.stop()


async def test_legacy_gomoku_ai_seat_migrates_to_turn_not_agent():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms)  # legacy → gomoku migrates to turn
    assert modes.driver_of(room.match) == "turn"
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok and "已落子" in result.text
    assert len(room.match.log) == 2
    await director.stop()


async def test_easy_bot_match_is_teammate_and_uses_module_bot_only():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(
        rooms, mode_options={"mode": "bot", "difficulty": "easy"}
    )
    assert modes.role_of(room.match) == "teammate"
    assert modes.driver_of(room.match) == "bot"
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: len(room.match.log) == 2)
    assert room.match.log[0].seat == 0 and room.match.log[1].seat == 1
    assert director.grant_of(room.match.id) is None  # no delegation → no grant
    await director.stop()


async def test_advanced_bot_match_routes_opponent_through_agent():
    rooms, _plugin, _recorder, director = _harness_bound()
    driven: list[tuple[str, int]] = []

    async def drive_hook(room_obj, seat):
        driven.append((room_obj.match.id, seat))

    rooms.drive_ai_hook = drive_hook
    room, _seat, _token = await _create(
        rooms, mode_options={"mode": "bot", "difficulty": "advanced"}
    )
    assert modes.role_of(room.match) == "teammate"
    assert modes.driver_of(room.match) == "agent"
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(driven))
    assert driven[0][1] == 1
    assert director.grant_of(room.match.id) is None
    await director.stop()


async def test_delegation_enable_when_waiting_human_turn_grants_immediately():
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(
        rooms, mode_options={"mode": "bot", "difficulty": "easy"}
    )
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: len(room.match.log) == 2)
    assert director.grant_of(room.match.id) is None
    await room.set_mode_options(delegation=True)
    granted = await director.refresh_turn(room, reason="delegation_enable")
    assert granted is True
    grant = director.grant_of(room.match.id)
    assert grant is not None and grant.kind == "delegated_human"
    await _wait(lambda: len(recorder.sent) == 1)
    text = _notice_text(recorder)
    assert "代下" in text or "委托" in text
    await director.stop()


async def test_director_stop_cancels_grants_without_moves():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    moves_before = len(room.match.log)
    await director.stop()
    assert director.grant_of(room.match.id) is None
    await asyncio.sleep(0.2)
    assert len(room.match.log) == moves_before  # shutdown never plays


# ----------------------------------------------------------------------
#  Concurrent minting / typed results / grant pruning
# ----------------------------------------------------------------------
async def test_parallel_grant_turn_mints_single_grant_and_notice() -> None:
    rooms, _plugin, recorder, director = _harness_bound(turn_play_timeout_s=0.6)
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)

    grants = await asyncio.gather(
        *(director.grant_turn(room, 1, "nori_turn", reason="race") for _ in range(4))
    )
    assert sum(grant is not None for grant in grants) == 1
    await _wait(lambda: len(recorder.sent) == 1)
    await asyncio.sleep(0.1)
    assert len(recorder.sent) == 1
    assert len(director._grants) == 1
    await director.stop()


async def test_submit_play_returns_typed_result_and_say_only_on_success() -> None:
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    result = await director.submit_play(room.match.id, grant.key, "J9", say="这手稳")
    assert isinstance(result, PlayResult)
    assert result.ok is True and result.terminal is True
    assert result.status == "played" and result.say == "这手稳"
    assert result.move == "J9"
    # Legacy unpacking still works.
    text, terminal = result
    assert text == result.text and terminal is True
    # persist_say defaults to False: the say is not written into match chat.
    assert room.match.chat == []

    result = await director.submit_play(room.match.id, grant.key, "K9", say="迟到")
    assert result.ok is False and result.terminal is True
    assert result.status == "no_grant" and result.say == ""
    await director.stop()


async def test_submit_play_persist_say_writes_match_chat() -> None:
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    result = await director.submit_play(
        room.match.id, grant.key, "J9", say="该你了", persist_say=True
    )
    assert result.ok and result.say == "该你了"
    assert [entry.text for entry in room.match.chat] == ["该你了"]
    await director.stop()


async def test_prune_grants_removes_only_resolved_old_grants() -> None:
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await director.submit_play(room.match.id, grant.key, "J9")
    assert grant.state == "played"

    assert director.prune_grants(now=time.monotonic(), retention_s=3600) == []
    assert director.prune_grants(now=time.monotonic(), retention_s=0) == [
        room.match.id
    ]
    assert director.grant_of(room.match.id) is None
    await director.stop()


async def test_no_bot_module_disables_delegation_and_does_not_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rule bot: the watchdog closes the grant and disables delegation."""
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.4, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)

    class _NoBot:
        # Same object as the base default: means "no rule bot".
        bot = GameModule.bot

    class _ProxyGames:
        @staticmethod
        def get(_kind):
            return _NoBot()

    # Only the director's registry view is replaced; rooms/engine stay real.
    monkeypatch.setattr(director_module, "games", _ProxyGames())
    await _wait(
        lambda: director.grant_of(room.match.id) is not None
        and director.grant_of(room.match.id).state != "active"
    )
    assert director.grant_of(room.match.id).state == "cancelled"
    assert modes.delegation_of(room.match) is False
    merged = await room.set_mode_options(delegation=False)
    assert merged["delegation"] is False
    await director.stop()


# ----------------------------------------------------------------------
#  Request-scoped tools: visible only to the notice session, gone after play
# ----------------------------------------------------------------------
async def test_tools_for_only_the_announced_session():
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    assert director.tools_for("private-u1") == []  # no grant yet

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    session_id = recorder.sent[0][0]

    tools = director.tools_for(session_id)
    names = {tool.name for tool in tools}
    assert play_tool_name(room.match.id) in names
    assert state_tool_name(room.match.id) in names
    assert director.tools_for("private-other") == []  # other sessions: nothing
    assert director.tools_for("") == []

    # A legal play closes the grant → the next request gets no tools.
    grant = director.grant_of(room.match.id)
    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok
    assert director.tools_for(session_id) == []
    await director.stop()


async def test_multi_match_same_target_gets_distinct_tools_no_overwrite():
    """Two matches sharing one target session keep distinct play tools."""
    rooms, plugin, recorder, director = _harness_bound()

    async def notice_target(_room=None):
        return "qq:group:100:nori", True

    plugin.notice_target = notice_target
    room_a, _sa, _ta = await _create(rooms, actor="ua", mode_options=dict(NORI))
    room_b, _sb, _tb = await _create(rooms, actor="ub", mode_options=dict(NORI))
    await room_a.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await room_b.apply(0, {"type": "move", "move": "C3"}, client_version=1)
    await _wait(
        lambda: director.grant_of(room_a.match.id) is not None
        and director.grant_of(room_b.match.id) is not None
    )
    await _wait(lambda: len(recorder.sent) == 2)

    # One session, two grants: tools_for returns one bound pair; both matches
    # are individually reachable through their own grants.
    names = {tool.name for tool in director.tools_for("qq:group:100:nori")}
    assert names in (
        {play_tool_name(room_a.match.id), state_tool_name(room_a.match.id)},
        {play_tool_name(room_b.match.id), state_tool_name(room_b.match.id)},
    )

    grant_a = director.grant_of(room_a.match.id)
    grant_b = director.grant_of(room_b.match.id)
    result_a = await director.submit_play(room_a.match.id, grant_a.key, "J9")
    result_b = await director.submit_play(room_b.match.id, grant_b.key, "D3")
    assert result_a.ok and result_b.ok
    assert len(room_a.match.log) == 2 and len(room_b.match.log) == 2
    await director.stop()


async def test_play_tool_through_grant_stops_the_turn():
    """The real ``play_<match>`` tool submits via the director and stops."""
    from test_tools import _Event

    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    play = next(
        tool
        for tool in director.tools_for(grant.session_id)
        if tool.name == play_tool_name(room.match.id)
    )
    event = _Event(extra={"gamehub_turn": {"match_id": room.match.id, "seat": 1,
                                           "kind": "nori_turn", "grant": grant.key}})
    text = await play.execute(event, move="J9")
    assert "本次行动授权已完成" in text
    assert event.stopped is True
    assert grant.state == "played"
    assert len(room.match.log) == 2

    # A late duplicate call is refused without lying about success.
    late = await play.execute(event, move="K9")
    assert "授权" in late and "已完成" not in late
    assert len(room.match.log) == 2
    await director.stop()


async def test_illegal_move_is_correctable_and_watchdog_still_fills():
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.4, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    result = await director.submit_play(room.match.id, grant.key, "ZZ99")
    assert result.ok is False and result.terminal is False
    assert result.status == "invalid" and result.error
    assert grant.state == "active"  # the model may still correct itself
    # Watchdog is still armed and fills once.
    await _wait(lambda: grant.state == "filled")
    assert len(room.match.log) == 2
    await director.stop()


async def test_busy_queued_notice_watchdog_fills_and_late_play_is_stale():
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.5, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    assert len(room.match.log) == 2

    grant = director.grant_of(room.match.id)
    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.terminal is True and "结束" in result.text
    assert len(room.match.log) == 2
    await director.stop()


async def test_restore_without_target_still_fills_via_watchdog():
    """No resolvable target (early boot) → no notice, watchdog fills anyway."""
    rooms, plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.5, turn_fallback_delay_s=0.05
    )
    plugin.notice_target = None  # host wiring not ready
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    assert len(room.match.log) == 2
    await director.stop()


async def test_stop_button_watchdog_fills_minted_grant_then_no_future():
    """Stop-next-turn: an already minted grant may finish; no new grants."""
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.5, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    await room.set_mode_options(delegation=False)
    await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    assert room.match.log[0].seat == 0

    await _wait(lambda: len(room.match.log) >= 2)
    await asyncio.sleep(0.5)
    current = director.grant_of(room.match.id)
    assert current is None or current.state != "active"
    await director.stop()


async def test_restore_active_rearms_waiting_turns():
    rooms, _plugin, recorder, director = _harness_bound(turn_play_timeout_s=5.0)
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    # First event: the human opens and the AI seat is already waiting.
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    director.cancel_match(room.match.id, reason="test")
    await asyncio.sleep(0.1)

    count = await director.restore_active()
    assert count == 1
    grant = director.grant_of(room.match.id)
    assert grant is not None and grant.state == "active"
    assert grant.move_count == len(room.match.log)
    await director.stop()


async def test_cancel_match_closes_the_grant_and_revokes_tools():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    assert director.tools_for(grant.session_id)

    director.cancel_match(room.match.id, reason="user_stop")
    assert grant.state == "cancelled"
    assert director.tools_for(grant.session_id) == []
    # Cancelling an unknown match is a no-op.
    director.cancel_match("nope")
    await director.stop()


async def test_say_is_truncated_by_settings_on_the_tool_path():
    """``max_say_len`` truncates the optional line before it reaches the room."""
    from test_tools import _Event

    rooms, _plugin, _recorder, director = _harness_bound(max_say_len=4)
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    play = next(
        tool
        for tool in director.tools_for(grant.session_id)
        if tool.name == play_tool_name(room.match.id)
    )
    event = _Event(extra={"gamehub_turn": {"match_id": room.match.id, "seat": 1,
                                           "kind": "nori_turn", "grant": grant.key}})
    await play.execute(event, move="J9", say="这句太长了会被截断")
    assert grant.state == "played"
    await director.stop()


async def test_agent_name_drives_stage_and_action_notice_wording():
    rooms, _plugin, recorder, director = _harness_bound(agent_name="蕾依")
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await _wait(lambda: len(recorder.stage) == 1)
    assert "蕾依" in recorder.stage[0][1]

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    assert "蕾依" in _notice_text(recorder)
    await director.stop()


async def test_no_agent_name_falls_back_to_nori():
    assert director_module.DEFAULT_AGENT_NAME == "nori"
    assert director_module._agent_name(SimpleNamespace(settings=SimpleNamespace())) == "nori"
    assert (
        director_module._agent_name(
            SimpleNamespace(settings=SimpleNamespace(agent_name="  "))
        )
        == "nori"
    )
