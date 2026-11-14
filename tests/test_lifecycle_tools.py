"""Turn-director lifecycle tests: resign, plan data, tool surface, restart.

Ported from nori's ``tests/test_lifecycle_tools.py`` — only the parts that
touch the notice-driven turn director fit the KiraAI port. The web/main
identity-migration tests and the ``bot_config.toml`` exposure test belong to
nori's host wiring and are intentionally absent here.

Host fakes are shared with ``test_turn_director``: a real ``RoomManager`` over
an in-memory repo, a fake plugin exposing ``rooms`` / ``settings`` /
``notice_target`` / ``stopping`` / ``_director``, and a recorded
``host.notices.send_notice``.
"""

from __future__ import annotations

import asyncio

import pytest

import director as director_module
import tools as game_tools
from room import RoomError

from test_turn_director import DELEGATED, NORI, _create, _harness, _wait


@pytest.fixture(autouse=True)
def _patch_notices(monkeypatch: pytest.MonkeyPatch):
    """Publish notices into the per-plugin recorder instead of the host."""

    async def fake_send(plugin, sid: str, text: str) -> bool:
        recorder = getattr(plugin, "_recorder", None)
        if recorder is None:
            return True
        return await recorder(plugin, sid, text)

    monkeypatch.setattr(director_module, "send_notice", fake_send)


def _bound_harness(**cfg_overrides):
    rooms, plugin, recorder, director = _harness(**cfg_overrides)
    plugin._recorder = recorder
    return rooms, plugin, recorder, director


# ----------------------------------------------------------------------
#  Resign / match over ends the turn cleanly
# ----------------------------------------------------------------------
async def test_resign_closes_inflight_grant_and_never_fills():
    rooms, _plugin, _recorder, director = _bound_harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    moves_before = len(room.match.log)

    await rooms.resign_ai("u1")
    await _wait(lambda: director.grant_of(room.match.id) is None)
    # Past the watchdog deadline: a cancelled grant never plays.
    await asyncio.sleep(0.8)
    assert room.match.log[-1].move == "resign"
    assert len(room.match.log) == moves_before + 1
    assert room.status == "over"
    await director.stop()


async def test_match_over_before_watchdog_fill_leaves_position_alone():
    rooms, _plugin, _recorder, director = _bound_harness(
        turn_play_timeout_s=0.3, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    # Resign right inside the watchdog window.
    await room.apply(1, {"type": "resign"})
    await asyncio.sleep(0.6)
    assert room.status == "over"
    assert room.match.log[-1].move == "resign"
    assert len(room.match.log) == 2  # the human move + the resign, no bot fill
    assert director.grant_of(room.match.id) is None
    await director.stop()


async def test_delegated_grant_is_closed_when_the_match_ends():
    rooms, _plugin, _recorder, director = _bound_harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(DELEGATED))
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    assert grant.kind == "delegated_human"

    # The human resigns manually: no more delegated fills afterwards.
    await room.apply(0, {"type": "resign"})
    await _wait(lambda: director.grant_of(room.match.id) is None)
    await asyncio.sleep(0.8)
    assert room.status == "over"
    assert director.tools_for(grant.session_id) == []
    await director.stop()


# ----------------------------------------------------------------------
#  Plan data stays non-destructive and out of the action notice
# ----------------------------------------------------------------------
async def test_plan_write_semantics_survive_and_stay_out_of_the_notice():
    rooms, _plugin, recorder, director = _bound_harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    before_moves = room.match.state["moves"]

    match = await room.set_plan(1, text="这局放水，别堵活四", ttl_moves=5, ttl_s=60)
    plan = match.seats[1].plan
    assert plan["text"] == "这局放水，别堵活四"
    assert plan["ttl_moves"] == 5
    assert match.version == 1  # set_plan never bumps the version
    assert match.state["moves"] == before_moves

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    notice = recorder.sent[-1][1]
    assert "放水" not in notice  # no plan injection
    assert "H8" in notice
    # Historic plan data is untouched.
    assert room.match.seats[1].plan["text"] == "这局放水，别堵活四"
    await director.stop()


async def test_plan_write_after_game_over_is_rejected_with_no_open_grant():
    rooms, _plugin, _recorder, director = _bound_harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await rooms.resign_ai("u1")
    await _wait(lambda: director.grant_of(room.match.id) is None)
    with pytest.raises(RoomError) as err:
        await room.set_plan(1, text="结束后的策略", ttl_moves=3, ttl_s=60)
    assert err.value.code == "game_over"
    assert room.match.seats[1].plan is None
    await director.stop()


# ----------------------------------------------------------------------
#  Tool surface: the director adds no global tool
# ----------------------------------------------------------------------
async def test_global_tool_surface_has_no_move_play_or_plan():
    rooms, plugin, _recorder, director = _bound_harness()
    # ``main.py`` registers exactly these four plain functions.
    surface = [
        name
        for name in game_tools.__all__
        if name.startswith("game_")
    ]
    assert surface == ["game_list", "game_new", "game_resign", "game_state"]
    for absent in ("game_move", "play", "game_plan"):
        assert absent not in game_tools.__all__
        assert not hasattr(game_tools, absent)
    # Turn tools are request-scoped: nothing before a grant exists.
    assert director.tools_for("private-u1") == []
    assert director.tools_for("") == []

    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    # Even with a live grant the global surface is unchanged.
    assert [
        name for name in game_tools.__all__ if name.startswith("game_")
    ] == surface
    assert not hasattr(game_tools, "play")
    await director.stop()


# ----------------------------------------------------------------------
#  Stop / restore lifecycle
# ----------------------------------------------------------------------
async def test_stop_then_restore_does_not_resurrect_work():
    rooms, _plugin, _recorder, director = _bound_harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    moves_before = len(room.match.log)

    await director.stop()
    # A stopped director never mints/runs anything again (plugin teardown).
    assert await director.restore_active() == 0
    assert director.tools_for("private-u1") == []
    assert director.on_event(room, {"type": "move", "match": room.match}) is None
    await asyncio.sleep(0.3)
    assert len(room.match.log) == moves_before


async def test_restore_active_only_rearms_matches_whose_seat_owes_a_move():
    rooms, plugin, _recorder, director = _bound_harness(turn_play_timeout_s=5.0)
    waiting_room, _sa, _ta = await _create(
        rooms, actor="ua", mode_options=dict(NORI)
    )
    await waiting_room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(waiting_room.match.id) is not None)
    director.cancel_match(waiting_room.match.id, reason="test")

    # A teammate match with no delegation never owes a turn.
    teammate_room, _sb, _tb = await _create(
        rooms,
        actor="ub",
        opponent="bot",
        mode_options={"mode": "bot", "difficulty": "easy"},
    )
    await teammate_room.apply(0, {"type": "move", "move": "C3"}, client_version=1)
    await _wait(lambda: len(teammate_room.match.log) == 2)

    assert director.grant_of(waiting_room.match.id) is None or (
        director.grant_of(waiting_room.match.id).state != "active"
    )
    restored = await director.restore_active()
    assert restored == 1
    grant = director.grant_of(waiting_room.match.id)
    assert grant is not None and grant.state == "active"
    assert director.grant_of(teammate_room.match.id) is None
    await director.stop()
