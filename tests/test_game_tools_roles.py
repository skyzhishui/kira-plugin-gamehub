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

import modes
from tools import game_new, game_resign, game_state
from tools_turn import build_play_tag, build_turn_context, build_turn_tools

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
async def test_game_state_teammate_perspective_in_bot_only_match():
    rooms = _Rooms()
    plugin, match = await _open(rooms, opponent="bot")
    assert modes.role_of(match) == modes.ROLE_TEAMMATE
    text = await game_state(plugin, _Event())
    assert "Game" in text  # full view from the human-side seat
    assert "诺里的打法" not in text  # opponent plan never becomes "our plan"
    assert text.strip()


async def test_advanced_opponent_seat_uses_agent_configured_name() -> None:
    """The advanced practice seat is stored with the configured agent name."""
    rooms = _Rooms()
    plugin = SimpleNamespace(
        rooms=rooms,
        settings=SimpleNamespace(site_base_url="", max_say_len=280),
        agent_player_name=lambda: "蕾依",
    )
    text = await game_new(
        plugin,
        _Event(),
        kind="gomoku",
        opponent="bot",
        difficulty="advanced",
    )
    assert "已开局" in text
    assert rooms.match.seats[1].name == "蕾依"


async def test_game_state_opponent_perspective_unchanged_for_legacy():
    rooms = _Rooms()
    plugin, match = await _open(rooms, opponent="ai")
    _attach_plan(match, 1, "先占天元")
    text = await game_state(plugin, _Event())
    assert "先占天元" in text  # opponent match still shows the bot's plan
    assert "的打法" in text


async def test_tool_outputs_have_no_legacy_move_protocol_or_hardcoded_open():
    rooms = _Rooms()
    plugin, _match = await _open(rooms, opponent="ai")
    state_text = await game_state(plugin, _Event())
    assert "MOVE:" not in state_text
    assert "执黑先行" not in state_text
    new_text = await game_new(plugin, _Event("webchat:dm:u2"), kind="gomoku")
    assert "已开局" in new_text
    assert "MOVE:" not in new_text
    assert "执黑先行" not in new_text


# ----------------------------------------------------------------------
#  Notice-turn safety
# ----------------------------------------------------------------------
async def test_notice_turn_mutations_are_refused():
    rooms = _Rooms()
    plugin, match = await _open(rooms, opponent="ai")
    version_before = match.version
    event = _notice_event()
    assert "不可用" in await game_new(plugin, event, kind="gomoku")
    assert "不可用" in await game_resign(plugin, event)
    # A notice turn never opens / resigns a real match.
    assert match.version == version_before
    assert match.resigned is None


async def test_notice_actor_cannot_touch_other_match():
    rooms = _Rooms()
    plugin, match = await _open(rooms, opponent="ai")
    version_before = match.version
    text = await game_resign(plugin, _notice_event())
    assert "不可用" in text  # safety gate, not an actor lookup
    assert match.version == version_before and match.resigned is None


async def test_notice_turn_game_state_explains_itself():
    rooms = _Rooms()
    plugin, _match = await _open(rooms, opponent="ai")
    text = await game_state(plugin, _notice_event())
    assert "通知回合" in text


async def test_bound_game_state_uses_granted_seat_without_actor_lookup():
    rooms = _Rooms()
    rooms.match = None
    match = await _make_foreign_match(rooms)
    bound = {"match_id": match.id, "seat": 1, "kind": "nori_turn"}
    text = await game_state(plugin=_plugin(rooms), event=_Event(extra={"gamehub_turn": bound}))
    assert "seat 1" in text  # bound seat view, independent of the actor


async def _make_foreign_match(rooms: _Rooms):
    """Open a match owned by somebody else and return it."""
    plugin = _plugin(rooms)
    await game_new(plugin, _Event("webchat:dm:someone-else"), kind="gomoku")
    return rooms.match


async def test_bound_state_tool_reads_event_binding():
    rooms = _Rooms()
    match = await _make_foreign_match(rooms)
    grant = _StubGrant(match.id, 1, "k1", "nori_turn")
    plugin = _plugin(rooms)
    state = build_turn_tools(plugin, grant)[0]
    text = await state.execute(_Event(extra={"gamehub_turn": build_turn_context(grant)}))
    assert "seat 1" in text


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


def test_turn_surfaces_are_state_tool_only_and_match_scoped():
    """The move rides the tag channel; only the read view stays a tool."""
    a = _StubGrant("aaaa1111", 1, "k1", "nori_turn")
    b = _StubGrant("bbbb2222", 1, "k2", "nori_turn")
    tools_a = build_turn_tools(_turn_plugin(_Rooms()), a)
    tools_b = build_turn_tools(_turn_plugin(_Rooms()), b)
    assert [tool.name for tool in tools_a] == ["state_aaaa1111"]
    assert [tool.name for tool in tools_b] == ["state_bbbb2222"]
    assert not {tools_a[0].name} & {tools_b[0].name}
    # The tag channel keeps the match-scoped naming too.
    tag_a = build_play_tag(_turn_plugin(_Rooms()), a)
    tag_b = build_play_tag(_turn_plugin(_Rooms()), b)
    assert tag_a.name == "play_aaaa1111"
    assert tag_b.name == "play_bbbb2222"
    assert not {tag_a.name} & {tag_b.name}


def test_move_format_hint_lives_in_the_tag_description():
    grant = _StubGrant("m1", 1, "k1", "nori_turn")
    tag = build_play_tag(_turn_plugin(_Rooms()), grant, match_kind="reversi")
    assert "D6" in tag.description
    state = build_turn_tools(_turn_plugin(_Rooms()), grant)[0]
    assert state.name == "state_m1"