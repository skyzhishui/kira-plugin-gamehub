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


class _Rooms:
    """Minimal RoomManager stand-in exposing only what tools.py touches."""

    def __init__(self, match=None, *, match_id: str = "m1") -> None:
        self.match = match
        self.match_id = match_id
        self.created: list[dict] = []
        self.busy = False
        self.cancelled: list[str] = []

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
        return _Room(
            self.match, on_apply=self._set_match, on_cancel=self.cancelled
        )

    def _set_match(self, match) -> None:
        self.match = match

    async def create(self, **kwargs):
        if self.busy:
            raise _err("actor_busy", "你已有一局未结束，先下完这局吧")
        kind = kwargs["kind"]
        mode_options = dict(kwargs.get("mode_options") or {})
        opponent = str(kwargs.get("opponent") or "ai")
        if kind == "doudizhu":
            # Mirror ``RoomManager.create``: the lineup decides every seat's
            # driver, ``opponent`` is only used when no lineup is given.
            lineup = str(mode_options.get("lineup") or "nori_agent")
            drivers = modes.LINEUPS.get(lineup)
            if drivers is None:
                raise _err("bad_request", "不支持的斗地主阵容")
            kinds = ["human"] + [
                "bot" if driver == "bot" else "ai" for driver in drivers[1:]
            ]
        elif mode_options.get("mode") == modes.MODE_BOT and mode_options.get(
            "difficulty"
        ) == modes.DIFF_EASY:
            kinds = ["human", "bot"]
        elif mode_options:
            kinds = ["human", "ai"]
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
        return _Room(
            self.match, on_apply=self._set_match, on_cancel=self.cancelled
        ), 0, "a" * 32

    async def resign_ai(self, actor):
        result = apply_match_action(self.match, 1, {"type": "resign"}, 2)
        assert isinstance(result, MatchActionOk)
        self.match = result.match
        return self.match


class _Room:
    def __init__(self, match, on_apply=None, on_cancel=None) -> None:
        self.match = match
        self._on_apply = on_apply
        self._on_cancel = on_cancel

    async def apply(self, seat, action, **kwargs):
        result = apply_match_action(self.match, seat, action, 2)
        assert isinstance(result, MatchActionOk), getattr(result, "error", None)
        self.match = result.match
        if self._on_apply is not None:
            self._on_apply(self.match)
        return self.match

    def cancel_drive_tasks(self) -> None:
        if self._on_cancel is not None:
            self._on_cancel.append(self.match.id)


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
@pytest.mark.parametrize(
    "sid, expect_link",
    [
        ("webchat:dm:u1", True),   # private: the entry link is shown
        ("qq:gm:123", False),      # group: never leak the seat token
    ],
)
async def test_game_new_shows_the_entry_link_only_in_private(sid, expect_link):
    rooms = _Rooms()
    plugin = _plugin(rooms, base="https://nori.example")
    text = await game_new(plugin, _event(sid), kind="gomoku")

    assert "已开局" in text
    assert rooms.created[0]["actor"] == sid
    if expect_link:
        assert (
            "https://nori.example/page/plugin/kira-plugin-gamehub/hub/match.html"
            in text
        )
        assert "&t=" + "a" * 32 in text
    else:
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


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"kind": "nope"}, "没有这个游戏"),
        ({"kind": "gomoku", "opponent": "human"}, "陪练"),
        ({"kind": "gomoku", "difficulty": "easy"}, "difficulty"),
        # Doudizhu strength comes from ``lineup``; ``difficulty`` is not a knob.
        ({"kind": "doudizhu", "difficulty": "easy"}, "lineup"),
    ],
)
async def test_game_new_rejects_incompatible_arguments(kwargs, expected):
    rooms = _Rooms()
    text = await game_new(_plugin(rooms), _event(), **kwargs)
    assert expected in text
    assert rooms.created == []  # nothing was opened


async def test_game_new_defaults_and_mode_derived_options():
    rooms = _Rooms()
    text = await game_new(_plugin(rooms), _event())
    assert "已开局" in text
    assert rooms.created[0]["kind"] == "gomoku"  # default game

    # ``mode=bot`` alone opens an easy practice (teammate) match.
    rooms = _Rooms()
    await game_new(_plugin(rooms), _event(), kind="gomoku", mode="bot")
    assert rooms.created[0]["mode_options"] == {
        "mode": "bot",
        "difficulty": "easy",
        "level": "intermediate",
    }
    assert rooms.match.seats[1].kind == "bot"


async def test_game_new_doudizhu_honours_the_requested_lineup():
    rooms = _Rooms()
    await game_new(
        _plugin(rooms), _event(), kind="doudizhu", opponent="bot", lineup="bot_bot"
    )
    assert rooms.created[0]["mode_options"] == {"lineup": "bot_bot"}
    assert [seat.kind for seat in rooms.match.seats] == ["human", "bot", "bot"]


# ----------------------------------------------------------------------
#  Doudizhu viewpoint (P1-1): own hand only, spectators get no hands
# ----------------------------------------------------------------------
def _doudizhu_hand(match, seat: int) -> str:
    """The private hand text the module renders for ``seat`` (fresh copy)."""
    from engine.games.doudizhu.cards import move_text

    return move_text(list(match.state["cards"][seat]))


async def test_doudizhu_own_seat_view_across_every_entry_point():
    """Private-hand isolation holds on ``game_state`` and ``game_new``.

    ``game_new``'s first answer and its "already running" re-answer render
    the caller's own seat too — and group vs private changes only the link,
    never the view.
    """
    rooms = _Rooms()
    plugin = _plugin(rooms, base="https://nori.example")
    opened = await game_new(plugin, _event("qq:gm:g1"), kind="doudizhu")
    match = rooms.match
    assert modes.mode_of(match) == modes.MODE_NORI  # nori lineup by default
    own, nori = _doudizhu_hand(match, 0), _doudizhu_hand(match, 1)
    assert own != nori

    # game_new (first open) view.
    assert own in opened and nori not in opened
    assert "match.html" not in opened  # the entry link stays private-only

    # game_new (existing match) re-answer.
    again = await game_new(plugin, _event("qq:gm:g1"), kind="doudizhu")
    assert "已经有一局" in again
    assert own in again and nori not in again

    # game_state view for the seated caller.
    state = await game_state(plugin, _event("qq:gm:g1"))
    assert "Your PRIVATE hand" in state
    assert own in state and nori not in state


async def test_doudizhu_outsider_gets_spectator_view_without_hands_or_tactics():
    """An unseated caller (other session/group or a teammate lineup) only
    gets the public summary: no hands, no plan/guidance panel."""
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event("qq:gm:g1"), kind="doudizhu")
    # Internal tactics exist on the AI seat, but never reach an outsider.
    rooms.match.seats[1].plan = {
        "text": "先出小牌",
        "source": "agent",
        "created_move": 0,
        "ttl_moves": 8,
        "created_at": 1000,
        "ttl_s": 60.0,
        "seq": 1,
    }

    # A different session in another group knows the match id (printed in
    # group chats) but has no seat.
    text = await game_state(plugin, _event("qq:gm:elsewhere"), match_id="m1")

    assert "Your PRIVATE hand" not in text
    assert "Seats:" in text  # public board summary
    assert "先出小牌" not in text and "当前游戏指导" not in text
    for seat in range(3):
        assert _doudizhu_hand(rooms.match, seat) not in text

    # Same rule for a teammate lineup: no hidden-information fallback.
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event("qq:gm:g1"), kind="doudizhu", lineup="bot_bot")
    assert modes.role_of(rooms.match) == modes.ROLE_TEAMMATE
    text = await game_state(plugin, _event("qq:gm:elsewhere"), match_id="m1")
    assert "Your PRIVATE hand" not in text
    assert _doudizhu_hand(rooms.match, 0) not in text


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
    # The tool wrapper answers like the plain function.
    assert "Gomoku" in await GameStateTool(plugin).execute(_event())

    result = await game_resign(plugin, _event())
    assert "你认输了" in result
    assert "玩家 获胜" in result
    assert rooms.match.resigned == 1


async def test_tools_require_session_identity():
    with pytest.raises(ValueError):
        await game_new(_plugin(_Rooms()), _event(""), kind="gomoku")
    with pytest.raises(ValueError):
        await game_state(_plugin(_Rooms()), _event(""))


# ----------------------------------------------------------------------
#  By-id resign (P1-2): the match id alone is never an authorization
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "case, actor_seat, caller_sid, notice_route, expect_owner",
    [
        # The match's own session/actor seat may resign, and the room cleanup
        # (cancel drive tasks) runs exactly like ``rooms.resign_ai``.
        ("owner", ("qq:gm:g1", "qq:gm:g1"), "qq:gm:g1", None, True),
        # A different session in another group: fail closed, match untouched.
        ("outsider", ("qq:gm:g1", "qq:gm:g1"), "qq:gm:elsewhere", None, False),
        # The configured notice route is a delivery channel, not ownership.
        (
            "route_not_ownership",
            ("qq:gm:g1", "qq:gm:g1"),
            "qq:gm:elsewhere",
            "qq:gm:elsewhere",
            False,
        ),
        # No seat attribution at all on a legacy row: deny, don't act.
        ("unattributed", ("", ""), "qq:gm:g1", None, False),
        # Page matches: the shared page session is seated, so it may resign.
        ("page_session", ("admin", "gamehub:gm:room"), "gamehub:gm:room", None, True),
    ],
)
async def test_resign_by_id_authorizes_only_a_seated_caller(
    case, actor_seat, caller_sid, notice_route, expect_owner
):
    rooms = _Rooms()
    plugin = _plugin(rooms)
    if notice_route is not None:
        plugin.notice_target_session_sync = lambda room: notice_route
    await game_new(plugin, _event("gamehub:gm:room"), kind="gomoku")
    rooms.match.seats[0].actor, rooms.match.seats[0].session_id = actor_seat

    result = await game_resign(plugin, _event(caller_sid), match_id="m1")

    if expect_owner:
        assert "你认输了" in result, case
        assert rooms.match.resigned == 1
        assert rooms.cancelled == ["m1"]  # same cleanup discipline as resign_ai
    else:
        assert "认输失败" in result, case
        assert rooms.match.resigned is None  # the match is untouched
        assert rooms.cancelled == []


async def test_resign_by_id_refuses_a_teammate_match_before_resigning():
    """The seated caller is never allowed to resign a match the host only
    coaches: the host has no seat to resign."""
    rooms = _Rooms()
    plugin = _plugin(rooms)
    await game_new(plugin, _event("qq:gm:g1"), kind="gomoku", opponent="bot")

    result = await game_resign(plugin, _event("qq:gm:g1"), match_id="m1")

    assert "队友" in result
    assert rooms.match.resigned is None


# ----------------------------------------------------------------------
#  Tool surface
# ----------------------------------------------------------------------
def test_module_exposes_exactly_four_chat_tools():
    """The global surface stays four read/lifecycle functions.

    Moves ride the ``play_<match>`` tag and plans are data-only (deferred),
    so no ``game_move`` / ``play`` / ``game_plan`` tool may reappear.
    """
    assert callable(tools_module.game_list)
    names = [name for name in tools_module.__all__ if name.startswith("game_")]
    assert names == ["game_list", "game_new", "game_resign", "game_state"]
    for absent in ("game_move", "play", "game_plan"):
        assert not hasattr(tools_module, absent), absent
    # The tool class mirrors the function's contract.
    assert GameStateTool.name == "game_state"
    schema = GameStateTool.get_schema()
    assert schema["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }