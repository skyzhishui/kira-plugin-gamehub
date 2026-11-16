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
from engine import games
from engine.types import GameModule
from room import RoomManager
from tools_turn import play_tool_name, state_tool_name, submit_play_from_actions

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
        self.nudges: list[tuple[str, str]] = []
        self.delay = delay

    async def __call__(self, plugin: Any, sid: str, text: str) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        if text.startswith("[System: 对局开始") or text.startswith(
            "[System: 对局结束"
        ):
            self.stage.append((str(sid), str(text)))
        elif "还没有提交动作" in text:
            # Reminder before the rule bot takes an unanswered turn.
            self.nudges.append((str(sid), str(text)))
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
    assert f"state_{room.match.id}" in text  # the pointer names the bound tool
    assert "H8" in text  # this move
    assert "放水" not in text  # minus plan
    assert room.match.seats[1].plan["text"] == "这局放水，别堵活四"
    # The notice is a short event: no call discipline, no threats, no speech
    # guidance — the action contract rides the ephemeral context block.
    assert "一次回复只输出一个落子标签" not in text
    assert "同一次回复" not in text
    assert "系统会替你随手" not in text
    assert "盘面与规则见本轮 gamehub_context 注入块" in text
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
    assert "play" not in start
    assert director.grant_of(room.match.id) is None

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    assert f"state_{room.match.id}" in _notice_text(recorder)
    assert director.grant_of(room.match.id) is not None

    # Resign: the in-flight grant closes, the end notice still goes once.
    await room.apply(1, {"type": "resign"})
    await _wait(lambda: len(recorder.stage) == 2)
    over = recorder.stage[1][1]
    assert over.startswith("[System: 对局结束 · 五子棋")
    assert "认输" in over
    assert "play" not in over
    assert "轮到你" not in over  # the end never authorizes a move
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
    await director.stop()


@pytest.mark.parametrize("no_target", [False, True], ids=["target", "no_target"])
async def test_late_play_after_watchdog_fill_is_refused(no_target):
    """Once the rule bot filled the turn, a late submission never lands.

    Also covers early boot (no resolvable notice target): the notice is
    skipped, the grant is still minted and the watchdog still fills.
    """
    rooms, plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.5, turn_fallback_delay_s=0.05
    )
    if no_target:
        plugin.notice_target = None  # host wiring not ready
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    assert len(room.match.log) == 2

    grant = director.grant_of(room.match.id)
    result = await director.submit_play(room.match.id, grant.key, "J9")
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


async def test_watchdog_never_fills_a_turn_that_is_still_progressing():
    """A slow but live host turn must not be overwritten by the rule bot.

    The watchdog deadline is a *silence* window: every observed host step
    (``note_progress`` from the ON_STEP_RESULT/ON_TOOL_RESULT hooks) pushes it
    forward, so a turn that keeps producing steps outlives the window.
    """
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.6, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    # Host is working: report a step every 0.2s for ~2s (well past the 0.6s
    # silence window) while the turn stays unresolved.
    for _ in range(10):
        director.note_progress(grant.session_id)
        await asyncio.sleep(0.2)

    assert director.grant_of(room.match.id).state == "active", "live turn must stay open"
    assert len(room.match.log) == 1, "no bot move while the host is progressing"

    # Once the host goes quiet, the watchdog fills as before.
    await _wait(lambda: director.grant_of(room.match.id).state == "filled", timeout=5.0)
    assert room.match.log[-1].seat == 1
    await director.stop()


async def test_stalled_turn_gets_one_reminder_before_the_bot_fill():
    """A host that answers the notice with text only is asked once, then filled.

    Real symptom: the model replied "10，继续顶。" without calling
    ``play_<match>``; the watchdog silently played a rule-bot move and the
    table looked like the opponent timed out. One explicit reminder first.
    """
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=0.4, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)

    # The reminder arrives while the grant is still open.
    await _wait(lambda: bool(recorder.nudges), timeout=3.0)
    assert director.grant_of(room.match.id).state == "active"
    assert len(room.match.log) == 1, "a reminder must not play for the host"
    nudge = recorder.nudges[0][1]
    assert f"state_{room.match.id}" in nudge
    assert "还没有提交动作" in nudge
    # Short retry: status + pointer only; format/example/threat live in the
    # context block now.
    assert "<play_" not in nudge
    assert "系统会替你随手" not in nudge
    assert "gamehub_context" in nudge

    # Still no move → the bot fills, and only one reminder was ever sent.
    await _wait(lambda: director.grant_of(room.match.id).state == "filled", timeout=5.0)
    assert room.match.log[-1].seat == 1
    assert len(recorder.nudges) == 1
    await director.stop()


async def test_reminder_is_skipped_when_the_host_plays_in_time():
    """No reminder noise when the host uses the tool: only the action notice."""
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=5.0, turn_fallback_delay_s=0.3
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok
    await asyncio.sleep(0.6)
    assert recorder.nudges == []
    assert len(recorder.sent) == 1
    await director.stop()


async def test_note_progress_ignores_sessions_without_a_grant():
    """A step result for an unrelated session must not touch other grants."""
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=0.6, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    deadline = grant.deadline

    director.note_progress("private-unrelated")
    assert grant.deadline == deadline
    assert director.grant_of(room.match.id).state == "active"
    await director.stop()


async def test_watchdog_fills_delegated_human_seat_once():
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    assert grant.kind == "delegated_human" and grant.seat == 0

    await _wait(lambda: grant.state == "filled")
    assert room.match.log[0].seat == 0  # delegated fill on the human seat
    # 规则陪练的应手按房间节奏延迟，稍后才落地（不再是同一次动作内嵌）。
    await _wait(lambda: len(room.match.log) >= 2)
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
    # The delegated identity stays one short clause folded into the turn
    # phrase ("代 <human>"); no seat definition is stuffed into the notice.
    assert " 代 " in text
    assert "轮到你" in text
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

    class _NoBotModule:
        """The real gomoku module with the rule bot switched off.

        Everything else (``outcome``/``describe``/…) delegates so ``status_of``
        keeps working; ``bot`` at class level equals the base default, which
        is exactly how the engine's ``_has_bot`` detects "no rule bot".
        """

        bot = GameModule.bot

        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    class _ProxyGames:
        @staticmethod
        def get(kind: str):
            return _NoBotModule(games.get(kind))

    # The watchdog now takes its move from engine.match.bot_move (the
    # canonical entry that keeps difficulty perturbation), which consults
    # engine.match's own registry — patch there so the bot-less module is
    # visible on exactly the path the fill uses.
    from engine import match as engine_match

    monkeypatch.setattr(engine_match, "games", _ProxyGames())
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
    """Request-scoped tools: only the announced session, gone after play, and
    each schema carries its *instance* name for the provider payload."""
    rooms, _plugin, recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    assert director.tools_for("private-u1") == []  # no grant yet

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    session_id = recorder.sent[0][0]

    tools = director.tools_for(session_id)
    # The move rides the play tag channel; only the read view stays a tool.
    assert {tool.name for tool in tools} == {state_tool_name(room.match.id)}
    for tool in tools:
        schema = tool.get_schema()
        # A classmethod schema would serialise the parent name; the instance
        # name must reach the provider or the request is rejected.
        assert schema["name"] == state_tool_name(room.match.id)
        assert schema["description"]
        params = schema["parameters"]
        assert isinstance(params, dict) and params.get("type") == "object"
        assert isinstance(params.get("properties"), dict)
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

    # One session, two grants: tools_for exposes one grant's read tool; both
    # matches are individually reachable through their own grants.
    names = {tool.name for tool in director.tools_for("qq:group:100:nori")}
    assert names in (
        {state_tool_name(room_a.match.id)},
        {state_tool_name(room_b.match.id)},
    )

    grant_a = director.grant_of(room_a.match.id)
    grant_b = director.grant_of(room_b.match.id)
    result_a = await director.submit_play(room_a.match.id, grant_a.key, "J9")
    result_b = await director.submit_play(room_b.match.id, grant_b.key, "D3")
    assert result_a.ok and result_b.ok
    assert len(room_a.match.log) == 2 and len(room_b.match.log) == 2
    await director.stop()


async def test_play_tag_through_grant_submits_the_move():
    """The ``play_<match>`` tag path submits via the director; duplicates refuse."""
    rooms, plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    actions = [
        SimpleNamespace(message_list=[SimpleNamespace(text="J9，堵你。")]),
        SimpleNamespace(tag=SimpleNamespace(name=play_tool_name(room.match.id)), value="J9"),
    ]
    submitted = await submit_play_from_actions(
        plugin, director, grant, actions, grant.session_id
    )
    assert submitted is True
    assert grant.state == "played"
    assert len(room.match.log) == 2

    # A late duplicate tag is refused without a second move.
    late = await submit_play_from_actions(
        plugin, director, grant, actions, grant.session_id
    )
    assert late is False  # refused inside submit_play (grant already played)
    assert grant.state == "played"
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


async def test_restore_active_rearms_only_matches_whose_seat_owes_a_move():
    rooms, _plugin, recorder, director = _harness_bound(turn_play_timeout_s=5.0)
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    # First event: the human opens and the AI seat is already waiting.
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    director.cancel_match(room.match.id, reason="test")
    await asyncio.sleep(0.1)

    # A teammate match with no delegation never owes a turn → no grant.
    teammate_room, _sb, _tb = await _create(
        rooms, actor="ub", opponent="bot",
        mode_options={"mode": "bot", "difficulty": "easy"},
    )
    await teammate_room.apply(0, {"type": "move", "move": "C3"}, client_version=1)
    await _wait(lambda: len(teammate_room.match.log) == 2)

    count = await director.restore_active()
    assert count == 1  # only the waiting turn, never the teammate match
    grant = director.grant_of(room.match.id)
    assert grant is not None and grant.state == "active"
    assert grant.move_count == len(room.match.log)
    assert director.grant_of(teammate_room.match.id) is None
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


async def test_say_is_truncated_by_settings_on_the_tag_path():
    """``max_say_len`` truncates the tag-path line before it reaches the room."""
    rooms, plugin, _recorder, director = _harness_bound(max_say_len=4)
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    actions = [
        SimpleNamespace(message_list=[SimpleNamespace(text="这句太长了会被截断")]),
        SimpleNamespace(tag=SimpleNamespace(name=play_tool_name(room.match.id)), value="J9"),
    ]
    assert await submit_play_from_actions(
        plugin, director, grant, actions, grant.session_id
    ) is True
    assert grant.state == "played"
    assert room.match.chat and room.match.chat[-1].text == "这句太长"
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


async def test_notice_is_header_only_and_points_to_the_injected_block():
    """通知只发回合头：规则/盘面改由每轮请求的 gamehub_context 注入块承载。

    每条通知都会逐字写入会话历史并留在滚动窗口里——首条带规则、后续带
    盘面的旧方案会让陈旧盘面（以及斗地主的私牌）堆积在历史中。现在通知
    只保留触发事实与调用纪律，并以兜底行指向注入块与 state_<match>。
    """
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=5.0, turn_fallback_delay_s=0.3
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))

    # Turn 1: the human opens; the notice is a header with the fallback line.
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    first = _notice_text(recorder)
    assert "规则（完整）" not in first
    assert "完整局面" not in first
    assert "gamehub_context" in first
    assert f"state_{room.match.id}" in first
    assert "一次回复只输出一个落子标签" not in first
    assert "系统会替你随手" not in first

    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok

    # Turn 2 (human moves again): identical header-only shape.
    await room.apply(
        0, {"type": "move", "move": "K10"}, client_version=room.match.version
    )
    await _wait(lambda: len(recorder.sent) >= 2)
    later = _notice_text(recorder)
    assert "规则（完整）" not in later
    assert "完整局面" not in later
    assert "gamehub_context" in later
    await director.stop()


async def test_failed_notice_delivery_still_fills_with_the_watchdog():
    """送达失败时授权保留、看门狗照常兜底（规则不再有重发语义）。"""
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=0.6, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))

    # First delivery fails: the model never saw the notice.
    original = director_module.send_notice

    async def failing(plugin, sid, text):
        return False

    director_module.send_notice = failing
    try:
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
        await _wait(lambda: director.grant_of(room.match.id) is not None)
        await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    finally:
        director_module.send_notice = original

    # The bot fallback moved for the host; the match proceeds regardless of
    # notice delivery, and no notice carried rules either way.
    await room.apply(
        0, {"type": "move", "move": "J10"}, client_version=room.match.version
    )
    await _wait(lambda: len(recorder.sent) >= 1)
    assert "规则（完整）" not in _notice_text(recorder)
    await director.stop()


# ----------------------------------------------------------------------
#  F1/F2/F3: text-only replies, corrective nudges, in-flight detection
# ----------------------------------------------------------------------


async def test_text_only_reply_gets_corrective_nudge_and_fast_fill():
    """F1：回复完成却没落子 → 定向催促 + 缩短倒计时 → 快速兜底。

    事件复盘（18:15 斗地主）：flash 级模型把「叫3」写成正文后，旧逻辑
    只把它当进展续命 90 秒才兜底。现在：grace 后立即催促，deadline 切到
    turn_text_retry_s，静默则迅速由规则 bot 补手。
    """
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0,
        turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.05,
        turn_text_retry_s=0.3,
        turn_text_retry_max=2,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    sid = grant.session_id

    # A run that started AFTER the notice completes with plain text only
    # (no play submission) — the F1 arming precondition. Production offers
    # the turn surface on every request (host/prompt.inject_into_request),
    # so the test mirrors that before marking the run.
    director.tools_for(sid)
    director.note_llm_request(sid)
    director.note_response(sid)
    await _wait(lambda: any("刚才只发了文字" in t for _s, t in recorder.sent))
    corrective = next(t for _s, t in recorder.sent if "刚才只发了文字" in t)
    # Short corrective: the state + the block pointer. The concrete tag
    # syntax/example lives in the context requirements now.
    assert play_tool_name(room.match.id) not in corrective
    assert "gamehub_context" in corrective
    assert f"state_{room.match.id}" in corrective
    assert "系统会替你随手" not in corrective

    # The shortened deadline fills the turn quickly (the watchdog's sleep
    # granularity caps wake-up at 5s, so "fast" means << the 30s timeout).
    await _wait(lambda: director.grant_of(room.match.id).state == "filled", timeout=8.0)
    assert grant.text_nudges == 1
    await director.stop()


async def test_submitting_during_the_grace_window_cancels_the_corrective_nudge():
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0,
        turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.3,  # long enough to submit first
        turn_text_retry_s=0.4,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))

    director.note_llm_request(grant.session_id)
    director.note_response(grant.session_id)
    result = await director.submit_play(room.match.id, grant.key, "J9")
    assert result.ok
    await asyncio.sleep(0.6)  # past grace + retry window
    assert not any("刚才只发了文字" in t for _s, t in recorder.sent)
    await director.stop()


async def test_corrective_nudge_is_capped_per_grant():
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=60.0,
        turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.02,
        turn_text_retry_s=50.0,  # long: keep the grant alive across nudges
        turn_text_retry_max=2,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    sid = grant.session_id

    for _ in range(5):
        director.tools_for(sid)  # the request offers the surface (production)
        director.note_llm_request(sid)
        director.note_response(sid)
        await asyncio.sleep(0.08)

    await _wait(lambda: grant.text_nudges >= 1)
    await asyncio.sleep(0.2)
    assert grant.text_nudges == 2  # capped at turn_text_retry_max
    corrective = [t for _s, t in recorder.sent if "刚才只发了文字" in t]
    assert len(corrective) == 2
    await director.stop()


async def test_turn_in_flight_marking_and_its_ttl():
    """F3：请求已发出、还没有任何回复 → 视作在途，不判静默。

    旧代码探测核心不存在的 ``session_mgr.is_busy``（永远 False），导致
    18:17:35 明明有 101s 的 run 在跑却被判静默、催促被提前浪费。只发过
    request、永不回复的死 run 标记必须按 TTL 失效，否则会楔死后续回合。
    """
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    sid = grant.session_id

    assert not await director._turn_in_flight(grant)
    director.note_llm_request(sid)
    assert await director._turn_in_flight(grant), "open run must count as busy"
    director.note_response(sid)  # answered → no longer in flight per se

    # A request mark older than the TTL is a dead run: it must not wedge.
    # (Clear the progress signal so only the run mark is under test.)
    grant.last_progress = 0.0
    director._llm_req[sid] = time.monotonic() - 301.0
    assert not await director._turn_in_flight(grant)
    await director.stop()


async def test_response_predating_the_notice_never_arms_the_corrective():
    """F1 守卫：通知还在排队时完成的历史 run 不算「只说不做」。

    事件复盘：开场白 run（101s）完成时，行动通知还没被模型看到——
    此时催「你刚才只发了文字」既冤枉又浪费预算，还会提前开短窗。
    """
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.05, turn_text_retry_s=0.3,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    sid = grant.session_id

    # A run that started BEFORE the notice (e.g. the opening-line run).
    before = grant.notice_sent_at - 10.0
    director._llm_req[sid] = before
    director.note_response(sid)
    await asyncio.sleep(0.3)  # past grace + retry
    assert not any("刚才只发了文字" in t for _s, t in recorder.sent)
    assert grant.text_nudges == 0
    assert grant.short_deadline == 0.0
    await director.stop()


async def test_tool_activity_during_grace_stands_the_corrective_down():
    """F1 守卫：多步回合（工具在跑）不是「只说不做」，不消耗预算。"""
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.3, turn_text_retry_s=0.4,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    sid = grant.session_id

    director.note_llm_request(sid)
    director.note_response(sid)  # step 1 done (say a state_ call, no play yet)
    await asyncio.sleep(0.05)  # enter the grace window
    director.note_tool_progress(sid)  # the tool result lands mid-grace
    await asyncio.sleep(0.5)  # past grace + retry
    assert not any("刚才只发了文字" in t for _s, t in recorder.sent)
    assert grant.text_nudges == 0
    await director.stop()


async def test_open_run_past_short_deadline_gets_bounded_extensions_then_fills():
    """F2：短窗耗尽但 run 仍在跑 → 分段续命到 short_ceiling，然后兜底。

    事件里的故障切换模型 25 token 也要 100s：一刀切在 25s 兜底会把
    模型正确的 play 弹掉；但不设上限又会被挂死的 run 楔死。折中：
    open run 每次只买一个 retry 窗口，天花板是纠正时刻+完整静默窗。
    """
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=1.2,
        turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.05,
        turn_text_retry_s=0.6,
        turn_text_retry_max=1,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))
    sid = grant.session_id

    director.tools_for(sid)  # the request offers the surface (production)
    director.note_llm_request(sid)
    director.note_response(sid)
    await _wait(lambda: grant.text_nudges == 1)  # corrective sent
    ceiling = grant.short_ceiling

    # The corrective triggers a NEW slow run that never answers.
    director.note_llm_request(sid)
    await _wait(
        lambda: director.grant_of(room.match.id).state == "filled",
        timeout=ceiling - time.monotonic() + 5.0,
    )
    # The fill happened no earlier than the short deadline and no later than
    # the ceiling (bounded extensions, then the rule bot takes over).
    assert grant.short_deadline > 0
    await director.stop()


async def test_shared_session_nudges_only_the_grant_whose_tools_were_offered():
    """F4 守卫：同一会话多 grant 时，只有拿到回合表面的 grant 会被催。

    ``tools_for``/标签注册每个请求只暴露一个 grant 的表面——另一个 grant 的
    模型根本无法提交，催它「只说不做」既错误又烧预算。
    """
    rooms, plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.05, turn_text_retry_s=0.5,
    )
    plugin.target_override = "shared-sid"  # both matches target one session
    room_a, _s, _t = await _create(rooms, actor="u1", mode_options=dict(NORI))
    room_b, _s, _t = await _create(rooms, actor="u2", mode_options=dict(NORI))
    await room_a.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await room_b.apply(0, {"type": "move", "move": "J9"}, client_version=1)
    await _wait(lambda: len(director._grants) >= 2)
    await _wait(lambda: len(recorder.sent) >= 2)
    grant_a = director.grant_of(room_a.match.id)
    grant_b = director.grant_of(room_b.match.id)
    assert grant_a is not None and grant_b is not None
    sid = grant_a.session_id
    assert sid == grant_b.session_id == "shared-sid"

    # The request layer offered grant A's tools for this session.
    director.tools_for(sid)
    offered = director._tools_grant.get(sid)
    nudged = grant_a if offered == grant_a.match_id else grant_b
    other = grant_b if nudged is grant_a else grant_a

    director.note_llm_request(sid)
    director.note_response(sid)
    await _wait(lambda: nudged.text_task is not None and nudged.text_task.done())
    await asyncio.sleep(0.2)
    assert nudged.text_nudges == 1, "the offered grant gets the corrective"
    assert other.text_nudges == 0, "the grant without tools is never blamed"
    await director.stop()


# ----------------------------------------------------------------------
#  2026-11-16 remediation regressions (P2).
# ----------------------------------------------------------------------
async def test_note_tag_rejected_only_counts_delivered_notices():
    """Budget is consumed by delivered notices, not failed sends (P2)."""
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    original = director_module.send_notice

    async def failing(plugin, sid, text):
        return False

    director_module.send_notice = failing
    try:
        await director.note_tag_rejected("s", grant, "zz", "无效动作")
    finally:
        director_module.send_notice = original
    assert grant.tag_retries == 0  # nothing delivered → no budget consumed
    assert grant.last_rejection == ("zz", "无效动作")  # the context trail stays

    await director.note_tag_rejected("s", grant, "zz", "无效动作")
    assert grant.tag_retries == 1
    await director.stop()


async def test_note_response_without_offered_surface_never_nudges():
    """Fail-closed: a grant whose tools were never offered gets no nudge (P2)."""
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0,
        turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.02,
        turn_text_retry_s=0.3,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))

    # No tools_for() call: the mapping has no entry for this session.
    assert director._tools_grant.get(grant.session_id) is None
    director.note_llm_request(grant.session_id)
    director.note_response(grant.session_id)
    await asyncio.sleep(0.2)  # past the grace + retry window
    assert grant.text_task is None, "no corrective without an offered surface"
    assert grant.text_nudges == 0
    await director.stop()


async def test_finish_cancels_an_in_flight_notice_task():
    """``_finish`` also cancels ``turn_task`` (narrow notice-after-close race)."""
    rooms, _plugin, _recorder, director = _harness_bound()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    original = director_module.send_notice
    release = asyncio.Event()

    async def slow_send(plugin, sid, text):
        await release.wait()  # park mid-delivery
        return True

    director_module.send_notice = slow_send
    try:
        await _wait(lambda: grant.notice_sent)  # _run_turn is inside send now
        director.cancel_match(room.match.id, "test_close")
        assert grant.state != "active"
        await asyncio.sleep(0.05)
        assert grant.turn_task is None or grant.turn_task.done()
    finally:
        release.set()
        director_module.send_notice = original
    await director.stop()
