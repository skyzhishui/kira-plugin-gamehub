"""Role-aware tools and notice-turn safety (KiraAI port of the nori suite).

Covers: teammate-perspective ``game_state``, notice-turn
(``message.is_notice`` / bound ``gamehub_turn``) refusing
``game_new`` / ``game_resign``, bound state without actor lookup, and the
turn-scoped ``state_<match>`` tool (the move rides the ``play_<match>``
tag channel — see ``test_turn_tag.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tools import game_new, game_resign, game_state
from tools_turn import build_turn_context, build_turn_tools

from test_tools import _Event, _Rooms, _plugin

NOTICE_SID = "gamehub:dm:admin"


def _notice_event(extra: dict | None = None) -> _Event:
    return _Event(NOTICE_SID, is_notice=True, extra=extra)


async def _open(rooms: _Rooms, *, opponent: str = "ai", **kwargs: Any):
    plugin = _plugin(rooms)
    await game_new(plugin, _Event(), kind="gomoku", opponent=opponent, **kwargs)
    return plugin, rooms.match


def _attach_plan(match, seat: int, text: str) -> None:
    match.seats[seat].plan = {
        "text": text,
        "source": "agent",
        "created_move": 0,
        "ttl_moves": 8,
        "created_at": 1000,
        "ttl_s": 60.0,
        "seq": 1,
        "review": False,
    }


# ----------------------------------------------------------------------
#  Teammate perspective
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "opponent, expect_opponent_plan",
    [
        # Practice (teammate) match: the human side's view, never the
        # opponent's private plan ("our plan" would be a lie).
        ("bot", False),
        # Legacy opponent match: the bot's plan is the AI seat's own plan.
        ("ai", True),
    ],
)
async def test_game_state_view_follows_the_match_role(opponent, expect_opponent_plan):
    rooms = _Rooms()
    plugin, match = await _open(rooms, opponent=opponent)
    _attach_plan(match, 1, "先占天元")

    text = await game_state(plugin, _Event())

    assert "Game" in text  # full view from the human-side seat
    if expect_opponent_plan:
        assert "先占天元" in text and "的打法" in text
    else:
        assert "诺里的打法" not in text
        assert "先占天元" not in text


@pytest.mark.parametrize(
    "agent_name, expected_name",
    [
        ("蕾依", "蕾依"),
        # No configured agent name keeps the neutral default, never a hook.
        (None, "nori"),
    ],
)
async def test_advanced_opponent_seat_uses_the_configured_agent_name(
    agent_name, expected_name
) -> None:
    """The advanced practice seat is stored with ``settings.agent_name``.

    That is the same source ``host/runtime.py`` feeds ``RoomManager``; the old
    fake injection hook (``agent_player_name``) must never be consulted.
    """
    rooms = _Rooms()
    plugin = SimpleNamespace(
        rooms=rooms,
        settings=SimpleNamespace(
            site_base_url="", max_say_len=280, agent_name=agent_name
        ),
        agent_player_name=lambda: "注入名",
    )
    text = await game_new(
        plugin, _Event(), kind="gomoku", opponent="bot", difficulty="advanced"
    )
    assert "已开局" in text
    assert rooms.match.seats[1].name == expected_name


# ----------------------------------------------------------------------
#  Notice-turn safety
# ----------------------------------------------------------------------
async def test_notice_turn_mutations_are_refused():
    """A notice turn never opens or resigns a real match, and the gate is a
    safety check — not an actor lookup — so it also blocks a resign aimed at
    the *other* match."""
    rooms = _Rooms()
    plugin, match = await _open(rooms, opponent="ai")
    version_before = match.version
    event = _notice_event()
    assert "不可用" in await game_new(plugin, event, kind="gomoku")
    assert "不可用" in await game_resign(plugin, event)
    assert "不可用" in await game_resign(plugin, _notice_event(), match_id="m1")
    assert match.version == version_before and match.resigned is None


async def test_notice_turn_game_state_explains_itself():
    rooms = _Rooms()
    plugin, _match = await _open(rooms, opponent="ai")
    text = await game_state(plugin, _notice_event())
    assert "通知回合" in text


async def test_bound_state_reads_the_granted_seat_without_actor_lookup():
    """The bound event binding decides the seat; the caller's actor is never
    consulted (the plain ``game_state`` path keeps the caller-seat rule)."""
    rooms = _Rooms()
    match = await _make_foreign_match(rooms)
    bound = {"match_id": match.id, "seat": 1, "kind": "nori_turn"}

    plain = await game_state(
        plugin=_plugin(rooms), event=_Event(extra={"gamehub_turn": bound})
    )
    assert "seat 1" in plain

    grant = _StubGrant(match.id, 1, "k1", "nori_turn")
    state = build_turn_tools(_plugin(rooms), grant)[0]
    tool = await state.execute(
        _Event(extra={"gamehub_turn": build_turn_context(grant)})
    )
    assert "seat 1" in tool


async def _make_foreign_match(rooms: _Rooms):
    """Open a match owned by somebody else and return it."""
    plugin = _plugin(rooms)
    await game_new(plugin, _Event("webchat:dm:someone-else"), kind="gomoku")
    return rooms.match


@pytest.mark.parametrize(
    "grant_state, expect_private",
    [
        # A live grant minted for this seat keeps its authorized perspective,
        # even behind a foreign SID (the bound view is the only path allowed
        # to show a seat other than the caller's).
        ("active", True),
        # A stale/foreign grant shows no private view at all (fail closed).
        ("done", False),
    ],
)
async def test_bound_doudizhu_perspective_requires_a_live_grant(
    grant_state, expect_private
):
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _Event("qq:gm:g1"), kind="doudizhu")
    match = rooms.match
    grant = _StubGrant(match.id, 1, "k1", "nori_turn")

    director = SimpleNamespace(
        grant_of=lambda match_id: SimpleNamespace(
            state=grant_state, key="k1", seat=1
        )
    )
    plugin = SimpleNamespace(
        rooms=rooms,
        settings=SimpleNamespace(site_base_url="", max_say_len=280),
        _director=director,
    )
    state = build_turn_tools(plugin, grant)[0]
    event = _Event("qq:gm:outsider", extra={"gamehub_turn": build_turn_context(grant)})

    text = await state.execute(event)

    if expect_private:
        assert "Your PRIVATE hand" in text
        assert _hand_text(match, 1) in text  # the granted (host AI) seat
        assert _hand_text(match, 0) not in text  # never the human seat's hand
    else:
        assert "Your PRIVATE hand" not in text


def _hand_text(match, seat: int) -> str:
    from engine.games.doudizhu.cards import move_text

    return move_text(list(match.state["cards"][seat]))


# ----------------------------------------------------------------------
#  Turn-scoped surfaces: state tool + play tag
# ----------------------------------------------------------------------
class _StubGrant:
    def __init__(self, match_id: str, seat: int, key: str, kind: str) -> None:
        self.match_id = match_id
        self.seat = seat
        self.key = key
        self.kind = kind
        self.state = "active"
        self.move_count = 0


class _StubDirector:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def submit_play(self, match_id: str, grant_key: str, move: str, say=None):
        self.calls.append((match_id, grant_key, move, say))
        return (f"已落子 {move}。本回合完成。", True)


def _turn_plugin(rooms: _Rooms, director: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        rooms=rooms,
        settings=SimpleNamespace(site_base_url="", max_say_len=280),
        _director=director,
    )


def test_build_turn_context_shape():
    grant = _StubGrant("m1", 1, "k1", "delegated_human")
    assert build_turn_context(grant) == {
        "match_id": "m1",
        "seat": 1,
        "kind": "delegated_human",
        "grant": "k1",
    }