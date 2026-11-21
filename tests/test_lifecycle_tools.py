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
from room import RoomError

from test_turn_director import DELEGATED, NORI, _create, _harness, _wait


@pytest.fixture(autouse=True)
def _patch_notices(monkeypatch: pytest.MonkeyPatch):
    """Publish notices into the per-plugin recorder instead of the host."""

    async def fake_send(plugin, sid: str, text: str, **kwargs) -> bool:
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
@pytest.mark.parametrize(
    "mode_options, end_action",
    [
        (dict(NORI), "resign_ai"),        # room-level resign helper
        (dict(NORI), "seat1_resign"),     # opponent resigns in-window
        (dict(DELEGATED), "seat0_resign"),  # delegated human resigns manually
    ],
    ids=["resign_ai", "in_window_resign", "delegated_human_resign"],
)
async def test_match_end_closes_the_grant_and_never_fills(mode_options, end_action):
    """A match that ends (any resign path) closes its grant, and the watchdog
    never plays a move into the finished position."""
    rooms, _plugin, _recorder, director = _bound_harness()
    room, _seat, _token = await _create(rooms, mode_options=mode_options)
    if mode_options.get("delegation"):
        await _wait(lambda: director.grant_of(room.match.id) is not None)
    else:
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
        await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    moves_before = len(room.match.log)

    if end_action == "resign_ai":
        await rooms.resign_ai("u1")
        await _wait(lambda: director.grant_of(room.match.id) is None)
        expected_log = moves_before + 1
    elif end_action == "seat1_resign":
        await room.apply(1, {"type": "resign"})
        expected_log = moves_before + 1
    else:
        await room.apply(0, {"type": "resign"})
        await _wait(lambda: director.grant_of(room.match.id) is None)
        expected_log = moves_before + 1

    # Past the watchdog deadline: a cancelled grant never plays.
    await asyncio.sleep(0.8)
    assert room.status == "over"
    assert room.match.log[-1].move == "resign"
    assert len(room.match.log) == expected_log
    assert director.grant_of(room.match.id) is None
    if grant is not None:
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
