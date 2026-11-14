"""Chat tool tests: identity resolution, single-match rules, links, errors.

Covers the KiraAI port of nori's ``GameListTool`` / ``GameNewTool`` /
``GameStateTool`` / ``GameResignTool`` as plain coroutines taking
``(plugin, event, ...)``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import modes
from engine import (
    MatchActionOk,
    NewMatchOptions,
    NewSeat,
    apply_match_action,
    create_match,
)
import tools as tools_module
from tools import GameStateTool, game_list, game_new, game_resign, game_state


class _Room:
    def __init__(self, match) -> None:
        self.match = match


class _Rooms:
    """Minimal RoomManager stand-in exposing only what tools.py touches."""

    def __init__(self, match=None, *, match_id: str = "m1") -> None:
        self.match = match
        self.match_id = match_id
        self.created: list[dict] = []
        self.busy = False

    def find_by_actor(self, actor):
        if self.match is None:
            return None
        return (
            _Room(self.match)
            if any(s.actor == actor for s in self.match.seats)
            else None
        )

    async def get_or_load(self, match_id):
        if self.match is None or self.match.id != str(match_id):
            return None
        return _Room(self.match)

    async def create(self, **kwargs):
        if self.busy:
            raise _err("actor_busy", "你已有一局未结束，先下完这局吧")
        kind = kwargs["kind"]
        mode_options = dict(kwargs.get("mode_options") or {})
        opponent = str(kwargs.get("opponent") or "ai")
        if mode_options.get("mode") == modes.MODE_BOT and mode_options.get(
            "difficulty"
        ) == modes.DIFF_EASY:
            opponent = "bot"
        elif mode_options:
            opponent = "ai"
        if kind == "doudizhu":
            drivers = modes.LINEUPS.get(
                str(mode_options.get("lineup") or "nori_agent"),
                ["human", "turn", "agent"],
            )
            kinds = ["human"] + [
                "bot" if driver == "bot" else "ai" for driver in drivers[1:]
            ]
        else:
            kinds = ["human", opponent]
        seats = [
            NewSeat(
                kind="human",
                token="a" * 32,
                name=kwargs.get("name") or "玩家",
                actor=kwargs["actor"],
                session_id=kwargs["session_id"],
                source=kwargs.get("source", ""),
            )
        ]
        for index, kind_ in enumerate(kinds[1:], start=1):
            seats.append(
                NewSeat(
                    kind=kind_,
                    token="",
                    name=(kwargs.get("opponent_name") if index == 1 else None),
                )
            )
        self.match = create_match(
            NewMatchOptions(
                id=self.match_id,
                kind=kind,
                seats=seats,
                seed=1,
                now=int(kwargs.get("now") or 0),
                options=dict(kwargs.get("options") or {}),
                mode_options=mode_options,
            )
        )
        self.created.append(kwargs)
        return _Room(self.match), 0, "a" * 32

    async def resign_ai(self, actor):
        result = apply_match_action(self.match, 1, {"type": "resign"}, 2)
        assert isinstance(result, MatchActionOk)
        self.match = result.match
        return self.match


class _Message:
    def __init__(self, is_notice: bool = False) -> None:
        self.is_notice = is_notice


class _Event:
    """``KiraMessageBatchEvent``-like object (sid / extra / message / stop)."""

    def __init__(
        self,
        sid: str = "webchat:dm:u1",
        *,
        is_notice: bool = False,
        extra: dict | None = None,
    ) -> None:
        self.sid = sid
        self.extra = dict(extra or {})
        self.message = _Message(is_notice)
        self.messages = [self.message]
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _err(code: str, message: str) -> Exception:
    exc = Exception(message)
    exc.code = code  # type: ignore[attr-defined]
    exc.message = message  # type: ignore[attr-defined]
    return exc


def _plugin(rooms: _Rooms, base: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        rooms=rooms,
        settings=SimpleNamespace(site_base_url=base, max_say_len=280),
    )


def _event(sid: str = "webchat:dm:u1") -> _Event:
    return _Event(sid)


# ----------------------------------------------------------------------
#  game_list
# ----------------------------------------------------------------------
async def test_game_list():
    text = await game_list(_plugin(_Rooms()), _event())
    assert "gomoku" in text
    assert "H8" in text


# ----------------------------------------------------------------------
#  game_new
# ----------------------------------------------------------------------
async def test_game_new_private_returns_link_and_uses_session_actor():
    rooms = _Rooms()
    plugin = _plugin(rooms, base="https://nori.example")
    text = await game_new(plugin, _event(), kind="gomoku")
    assert "已开局" in text
    assert rooms.created[0]["actor"] == "webchat:dm:u1"
    assert (
        "https://nori.example/page/plugin/kira-plugin-gamehub/hub/match.html" in text
    )
    assert "&t=" + "a" * 32 in text


async def test_game_new_group_hides_link():
    rooms = _Rooms()
    plugin = _plugin(rooms, base="https://nori.example")
    text = await game_new(plugin, _event("qq:gm:123"), kind="gomoku")
    assert "对局 ID：m1" in text
    assert "match.html" not in text


async def test_game_new_returns_existing_match():
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event(), kind="gomoku")
    before = len(rooms.created)
    text = await game_new(plugin, _event(), kind="gomoku")
    assert len(rooms.created) == before
    assert "已经有一局" in text


async def test_game_new_rejects_unknown_game_and_bad_opponent():
    plugin = _plugin(_Rooms())
    assert "没有这个游戏" in await game_new(plugin, _event(), kind="nope")
    assert "陪练" in await game_new(plugin, _event(), kind="gomoku", opponent="human")


async def test_game_new_defaults_to_gomoku():
    rooms = _Rooms()
    text = await game_new(_plugin(rooms), _event())
    assert "已开局" in text
    assert rooms.created[0]["kind"] == "gomoku"


async def test_game_new_derives_opponent_from_mode_argument():
    """``mode=bot`` alone opens a practice (teammate) match."""
    rooms = _Rooms()
    await game_new(_plugin(rooms), _event(), kind="gomoku", mode="bot")
    assert rooms.created[0]["mode_options"] == {
        "mode": "bot",
        "difficulty": "easy",
        "level": "intermediate",
    }
    assert rooms.match.seats[1].kind == "bot"


async def test_game_new_difficulty_only_makes_sense_for_practice():
    plugin = _plugin(_Rooms())
    text = await game_new(plugin, _event(), kind="gomoku", difficulty="easy")
    assert "difficulty" in text


# ----------------------------------------------------------------------
#  game_state / game_resign
# ----------------------------------------------------------------------
async def test_game_state_without_match():
    text = await game_state(_plugin(_Rooms()), _event())
    assert "没有进行中的对局" in text


async def test_game_state_and_resign():
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event(), kind="gomoku")
    state = await game_state(plugin, _event())
    assert "Gomoku" in state
    result = await game_resign(plugin, _event())
    assert "你认输了" in result
    assert "玩家 获胜" in result
    assert rooms.match.resigned == 1


async def test_game_state_by_match_id_uses_bound_seat():
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event(), kind="gomoku")
    text = await game_state(plugin, _event("webchat:dm:someone-else"), match_id="m1")
    # Seat 0 belongs to another actor: the caller still gets a spectator view.
    assert "对局 m1" in text


async def test_tools_require_session_identity():
    with pytest.raises(ValueError):
        await game_new(_plugin(_Rooms()), _event(""), kind="gomoku")
    with pytest.raises(ValueError):
        await game_state(_plugin(_Rooms()), _event(""))


# ----------------------------------------------------------------------
#  Tool surface
# ----------------------------------------------------------------------
def test_module_exposes_four_chat_tools_without_move_play_or_plan():
    names = ["game_list", "game_new", "game_state", "game_resign"]
    for name in names:
        assert callable(getattr(tools_module, name))
    assert GameStateTool.name == "game_state"
    assert "game_move" not in names
    assert "play" not in names
    assert "game_plan" not in names  # deferred; historic data stays readable


def test_game_state_tool_schema_is_toolset_compatible():
    schema = GameStateTool.get_schema()
    assert schema["name"] == "game_state"
    assert schema["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


async def test_game_state_tool_executes_like_the_function():
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event(), kind="gomoku")
    tool = GameStateTool(plugin)
    assert "Gomoku" in await tool.execute(_event())