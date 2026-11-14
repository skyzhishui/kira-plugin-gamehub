"""Lobby/match REST + WS: envelopes, status mapping and the WS handshake.

These tests call :class:`host.web.GamehubApi` directly (no aiohttp request or
response objects) against a real :class:`room.RoomManager` and a fake plugin.
The WS test drives :func:`host.web.serve_ws` with a queue-backed fake socket.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from engine.match import Match
from host.settings import Settings
from host.web import (
    ApiError,
    GamehubApi,
    _fail,
    _raise_room_error,
    envelope,
    error_envelope,
    serve_ws,
    ws_hub,
)
from room import RoomError, RoomManager


class FakeRepo:
    """Minimal in-memory repository (same shape as ``host.store``)."""

    def __init__(self) -> None:
        self.snapshots: dict[str, Match] = {}
        self.statuses: dict[str, str] = {}

    async def save(self, match: Match, status: str) -> None:
        self.snapshots[match.id] = Match.from_dict(match.to_dict())
        self.statuses[match.id] = status

    async def load(self, match_id: str) -> Match | None:
        snapshot = self.snapshots.get(match_id)
        return Match.from_dict(snapshot.to_dict()) if snapshot is not None else None

    async def load_active(self) -> list[Match]:
        return [
            Match.from_dict(snapshot.to_dict())
            for match_id, snapshot in self.snapshots.items()
            if self.statuses.get(match_id) == "active"
        ]


class FakeDirector:
    def __init__(self) -> None:
        self.refreshes: list[str] = []
        self.grants: dict[str, Any] = {}

    def grant_of(self, match_id: str) -> Any:
        return self.grants.get(str(match_id))

    async def refresh_turn(self, room: Any, *, reason: str = "refresh") -> bool:
        self.refreshes.append(str(reason))
        return True


class FakeCtx:
    def __init__(self) -> None:
        self.published: list[tuple[Any, Any, bool]] = []
        self.session_ok = True
        self.session_mgr = SimpleNamespace(resolve_session=self._resolve)
        self.bot_config = SimpleNamespace(bot=SimpleNamespace(nickname="诺里"))

    def _resolve(self, sid: str) -> Any:
        return SimpleNamespace(sid=sid) if self.session_ok else None

    async def publish_notice(self, session: Any, chain: Any, *, is_mentioned: bool = False) -> None:
        self.published.append((session, chain, is_mentioned))


class FakeTalk:
    def _mode_for(self, match: Any) -> str:
        return "key"


class FakePlugin:
    stopping = False
    _ready = True
    _adapter_ready = True

    def __init__(self, rooms: RoomManager, data_dir: Path) -> None:
        self.rooms = rooms
        self.data_dir = data_dir
        self.settings = Settings(
            {
                "max_active_matches": 5,
                "agent_name": "诺里",
                "practice_name": "小白",
                "page_chat_enabled": True,
                "page_chat_talk_default": "judge",
                "page_bubble_hold_s": 3.0,
                "site_base_url": "",
            }
        )
        self.ctx = FakeCtx()
        self._director = FakeDirector()
        self._talk = FakeTalk()
        self.persona = SimpleNamespace(reference_image_path="/tmp/avatar.png")

    def match_origin(self, match: Any) -> dict[str, str]:
        for seat in match.seats:
            if seat.source == "chat" and seat.session_id:
                return {"kind": "chat", "session_id": seat.session_id}
        return {"kind": "webui", "session_id": ""}


# 1x1 transparent PNG, used as an avatar upload payload.
_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def make_plugin(tmp_path: Path, *, now: int = 1000) -> FakePlugin:
    rooms = RoomManager(
        repository=FakeRepo(),
        config=Settings({"max_active_matches": 5}),
        now_fn=lambda: now,
        bot_name="诺里",
        practice_name="小白",
    )
    return FakePlugin(rooms, tmp_path)


async def data_of(coro):
    result = await coro
    assert result["ok"] is True
    assert result["error"] == ""
    return result["data"]


async def error_of(coro, status: int) -> ApiError:
    with pytest.raises(ApiError) as excinfo:
        await coro
    assert excinfo.value.status == status, excinfo.value.message
    return excinfo.value


# ----------------------------------------------------------------------
#  Seams
# ----------------------------------------------------------------------


async def test_ping_and_page_session(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))

    ping = await data_of(api.ping())
    assert ping["pong"] is True
    assert ping["page_session"] == "gamehub:dm:admin"
    assert ping["actor"] == "admin"
    assert ping["ready"] is True and ping["adapter_ready"] is True
    assert {"gomoku", "reversi", "doudizhu"} <= set(ping["games"])

    session = await data_of(api.page_session())
    assert session == {"session_id": "gamehub:dm:admin", "actor": "admin"}


def test_envelopes_are_symmetric():
    assert envelope({"a": 1}) == {"ok": True, "data": {"a": 1}, "error": ""}
    failure = error_envelope(ApiError(403, "bad token"))
    assert failure == {"ok": False, "data": None, "error": "bad token", "code": ""}


def test_error_envelope_carries_code():
    """The envelope exposes the stable code so the WebUI can localise it."""
    failure = error_envelope(_fail("座位口令无效", 403, code="bad_token"))
    assert failure["ok"] is False
    assert failure["error"] == "座位口令无效", "error stays the Chinese message"
    assert failure["code"] == "bad_token"

    # Room/engine codes survive the mapping (with their status).
    with pytest.raises(ApiError) as excinfo:
        _raise_room_error(RoomError("stale_version", "版本已更新"))
    assert excinfo.value.code == "stale_version"
    assert excinfo.value.status == 409
    assert error_envelope(excinfo.value)["code"] == "stale_version"

    # Every _fail site carries a code; the unmapped-type fallback is bad_request.
    with pytest.raises(ApiError) as excinfo:
        _raise_room_error(ValueError("boom"))
    assert excinfo.value.code == "bad_request"


async def test_identity_read_and_update(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    initial = await data_of(api.get_identity())
    assert initial["name"] == "Human"
    assert initial["actor"] == "admin"
    assert initial["page_session"] == "gamehub:dm:admin"

    updated = await data_of(api.set_identity({"name": "阿空"}))
    assert updated["name"] == "阿空"
    assert updated["effective_name"] == "阿空"
    saved = json.loads((tmp_path / "identity.json").read_text(encoding="utf-8"))
    assert saved == {"name": "阿空"}
    assert (await data_of(api.get_identity()))["name"] == "阿空"

    # New matches pick up the display name.
    created = await data_of(api.create_match({"kind": "gomoku"}))
    assert created["match"]["seats"][0]["name"] == "阿空"

    await error_of(api.set_identity({"name": "  "}), 400)
    await error_of(api.set_identity({"name": "x" * 41}), 400)


async def test_identity_without_data_dir_is_memory_only(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.data_dir = None
    api = GamehubApi(plugin)
    assert (await data_of(api.get_identity()))["name"] == "Human"
    assert (await data_of(api.set_identity({"name": "玩家"})))["name"] == "玩家"


async def test_ui_config_lobby_and_match(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    lobby = await data_of(api.ui_config())
    assert lobby["page_session"] == "gamehub:dm:admin"
    assert lobby["agent_name"] == "诺里"
    assert lobby["agent_avatar"] == "./assets/agent-avatar.png"
    assert lobby["match_id"] == ""
    assert lobby["current"] is None
    assert lobby["mode_options"] is None
    assert any(entry["kind"] == "gomoku" for entry in lobby["games"])
    assert lobby["notice_target_session_id"] == "gamehub:dm:admin"
    assert lobby["notice_target_status"] == "default"
    assert lobby["context_injectable"] is True

    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    # The lobby view now reports the current page match.
    lobby = await data_of(api.ui_config())
    assert lobby["current"]["id"] == match_id
    assert lobby["mode_options"]["mode"] == "nori"

    scoped = await data_of(api.ui_config(match_id))
    assert scoped["match_id"] == match_id
    assert scoped["match"]["id"] == match_id
    assert scoped["match_over"] is False
    assert scoped["talk_mode"] == "key"
    assert scoped["source"] == {"kind": "webui", "session_id": ""}
    assert scoped["chat_session"] == "gamehub:dm:admin"
    assert scoped["mode_options"]["mode"] == "nori"

    await error_of(api.ui_config("nope"), 404)


async def test_ui_config_notice_target_override(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.settings.notice_target_session_id = "gamehub:dm:other"
    api = GamehubApi(plugin)

    config = await data_of(api.ui_config())
    assert config["notice_target_session_id"] == "gamehub:dm:other"
    assert config["notice_target_status"] == "override"
    assert config["notice_target_platform"] == "gamehub"

    plugin.ctx.session_ok = False
    config = await data_of(api.ui_config())
    assert config["notice_target_status"] == "invalid"
    assert config["notice_target_error"]


async def test_ui_config_avatar_is_plugin_owned(tmp_path):
    """The agent avatar is plugin-owned: bundled default + upload, never the host persona."""
    plugin = make_plugin(tmp_path)
    plugin.persona = None

    async def get_active_persona():
        return SimpleNamespace(reference_image_path=" /data/persona/a.png ")

    plugin.ctx.persona_mgr = SimpleNamespace(get_active_persona=get_active_persona)
    api = GamehubApi(plugin)
    config = await data_of(api.ui_config())
    # The host persona image must never leak into the gamehub UI.
    assert config["agent_avatar"] == "./assets/agent-avatar.png"

    uploaded = await data_of(api.set_avatar({"data": _PNG_DATA_URL}))
    assert uploaded["uploaded"] is True
    assert uploaded["agent_avatar"].startswith("/api/plugin/kira-plugin-gamehub/agent-avatar")

    config = await data_of(api.ui_config())
    assert config["agent_avatar"] == uploaded["agent_avatar"]

    reset = await data_of(api.set_avatar({"reset": True}))
    assert reset["uploaded"] is False
    assert reset["agent_avatar"] == "./assets/agent-avatar.png"

    await error_of(api.set_avatar({"data": "not-a-data-url"}), 400)
    await error_of(api.set_avatar({}), 400)


async def test_notice_test_publishes_into_game_session(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    result = await data_of(api.notice_test())
    assert result["dispatched"] is True
    assert result["session_id"] == "gamehub:dm:admin"
    assert len(plugin.ctx.published) == 1
    _session, chain, mentioned = plugin.ctx.published[0]
    assert mentioned is True
    assert "通知联调" in chain[0].text

    plugin.ctx.session_ok = False
    await error_of(api.notice_test(), 503)


# ----------------------------------------------------------------------
#  Lobby
# ----------------------------------------------------------------------


async def test_games_lists_catalog_and_active_matches(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    empty = await data_of(api.games())
    assert empty["current"] is None
    assert empty["active_matches"] == []
    gomoku = next(entry for entry in empty["games"] if entry["kind"] == "gomoku")
    assert gomoku["agent_name"] == "诺里"
    assert gomoku["practice_name"] == "小白"

    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]
    token = created["token"]

    listed = await data_of(api.games())
    assert listed["current"]["id"] == match_id
    assert len(listed["active_matches"]) == 1
    row = listed["active_matches"][0]
    assert row["id"] == match_id
    assert row["game_name"]
    assert row["players"][0] == {"seat": 0, "kind": "human", "actor": "admin", "name": "Human"}
    assert row["source"]["kind"] == "webui"
    assert row["can_control"] is True
    assert row["status"] == "active"
    assert row["notice_target"]["session_id"] == "gamehub:dm:admin"
    assert token not in json.dumps(row)

    # Control face returns the acting seat token only.
    control = await data_of(api.control(match_id))
    assert control["seat"] == 0
    assert control["token"] == token
    assert control["url"].endswith(f"&t={token}")
    await error_of(api.control(match_id, seat=1), 403)

    await data_of(api.actions(match_id, token, {"type": "resign"}))
    await error_of(api.control(match_id), 409)
    assert (await data_of(api.games()))["active_matches"] == []


# ----------------------------------------------------------------------
#  Create / read match
# ----------------------------------------------------------------------


async def test_create_match_default_is_nori_mode(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))

    created = await data_of(api.create_match({"kind": "gomoku"}))
    match = created["match"]
    token = created["token"]
    assert len(token) == 32
    assert match["me"] == 0
    assert match["seats"][0]["joined"] is True
    assert "token" not in match["seats"][0]
    assert match["mode_options"]["mode"] == "nori"
    assert created["mode_options"]["driver"] == "turn"
    assert created["url"].endswith(f"&t={token}")

    again = await data_of(api.create_match({"kind": "gomoku"}))
    assert again["existing"] is True
    assert again["match"]["id"] == match["id"]
    assert again["token"] == token


async def test_create_match_rejects_bad_input(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))

    await error_of(api.create_match({"kind": "nope"}), 400)
    await error_of(api.create_match({"kind": "gomoku", "mode": "weird"}), 400)
    await error_of(
        api.create_match({"kind": "gomoku", "mode": "nori", "difficulty": "easy"}), 400
    )
    await error_of(
        api.create_match({"kind": "gomoku", "mode": "bot", "difficulty": "hard"}), 400
    )
    await error_of(
        api.create_match({"kind": "gomoku", "opponent": "human"}), 400
    )
    await error_of(
        api.create_match({"kind": "gomoku", "options": "nope"}), 400
    )


async def test_create_match_bot_mode_and_chess_level(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))

    created = await data_of(
        api.create_match(
            {
                "kind": "gomoku",
                "mode_options": {"mode": "bot", "difficulty": "advanced", "level": "beginner"},
            }
        )
    )
    match = created["match"]
    assert match["seats"][1]["kind"] == "ai"
    assert created["mode_options"]["mode"] == "bot"
    assert created["mode_options"]["difficulty"] == "advanced"
    assert created["mode_options"]["level"] == "beginner"

    updated = await data_of(api.set_mode(match["id"], {"level": "advanced"}))
    assert updated["mode_options"]["level"] == "advanced"


async def test_create_doudizhu_lineup_and_spectator_view(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))

    created = await data_of(
        api.create_match(
            {
                "kind": "doudizhu",
                "mode_options": {"lineup": "nori_agent"},
                "options": {"hands": "6"},
            }
        )
    )
    match = created["match"]
    assert match["mode_options"]["seat_drivers"] == ["human", "turn", "agent"]
    assert match["options"] == {"hands": "6"}
    assert len(match["view"]["hand"]) == 17

    public = await data_of(api.get_match(match["id"]))
    assert public["match"]["view"]["hand"] == []
    assert public["match"]["me"] is None
    assert "rng" not in public["match"]["view"]


async def test_get_match_token_views_and_errors(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]
    token = created["token"]

    seated = await data_of(api.get_match(match_id, token))
    assert seated["match"]["me"] == 0

    await error_of(api.get_match(match_id, "deadbeef"), 403)
    await error_of(api.get_match("missing"), 404)


async def test_set_mode_rejects_incompatible_switch(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))
    created = await data_of(
        api.create_match(
            {"kind": "gomoku", "mode_options": {"mode": "bot", "difficulty": "easy"}}
        )
    )
    match_id = created["match"]["id"]

    # The opponent seat is a rule bot, so switching to nori mode is refused.
    await error_of(api.set_mode(match_id, {"mode": "nori"}), 409)
    await error_of(api.set_mode(match_id, {"mode": "weird"}), 409)
    await error_of(api.set_mode("missing", {"mode": "nori"}), 404)

    # A no-op switch keeps the current options.
    same = await data_of(api.set_mode(match_id, {"mode": "bot", "difficulty": "easy"}))
    assert same["mode_options"]["mode"] == "bot"
    assert same["mode_options"]["difficulty"] == "easy"


async def test_chat_match_is_listed_but_is_not_page_current(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    room, _seat, _token = await plugin.rooms.create(
        kind="gomoku",
        actor="webchat:dm:friend",
        session_id="webchat:dm:friend",
        name="朋友",
        source="chat",
    )

    data = await data_of(api.games())
    assert data["current"] is None
    assert [row["id"] for row in data["active_matches"]] == [room.match.id]
    assert data["active_matches"][0]["source"]["kind"] == "chat"

    # The page can still create its own match (different actor).
    created = await data_of(api.create_match({"kind": "gomoku"}))
    assert created["match"]["id"] != room.match.id
    assert plugin.rooms.count() == 2


# ----------------------------------------------------------------------
#  Actions
# ----------------------------------------------------------------------


async def test_actions_move_versions_and_status_mapping(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]
    token = created["token"]
    version = created["match"]["version"]

    moved = await data_of(
        api.actions(
            match_id,
            token,
            {"type": "move", "move": "H8", "client_version": version},
        )
    )
    assert moved["match"]["version"] == version + 1
    assert any(entry["move"] == "H8" for entry in moved["match"]["log"])

    # stale_version -> 409
    await error_of(
        api.actions(
            match_id, token, {"type": "move", "move": "A1", "client_version": version}
        ),
        409,
    )
    # illegal move -> 400
    await error_of(
        api.actions(
            match_id,
            token,
            {"type": "move", "move": "H8", "client_version": moved["match"]["version"]},
        ),
        400,
    )
    # missing / bad token -> 403
    await error_of(api.actions(match_id, "", {"type": "resign"}), 403)
    await error_of(api.actions(match_id, "deadbeef", {"type": "resign"}), 403)
    # bad body -> 400
    await error_of(api.actions(match_id, token, ["nope"]), 400)

    ended = await data_of(api.actions(match_id, token, {"type": "resign"}))
    assert ended["match"]["status"]["resultText"]
    # game_over -> 409
    await error_of(api.actions(match_id, token, {"type": "resign"}), 409)


async def test_delegation_endpoints_and_action_face(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))
    plugin = api._plugin

    # nori-mode matches cannot delegate.
    nori_match = await data_of(api.create_match({"kind": "gomoku"}))
    await error_of(
        api.set_delegation(nori_match["match"]["id"], {"enabled": True}), 409
    )
    await error_of(
        api.actions(
            nori_match["match"]["id"],
            nori_match["token"],
            {"type": "delegate", "delegated": True},
        ),
        409,
    )

    # Bot-mode match: REST toggle and the seat-token action face. The page
    # actor already owns a match, so resign it first.
    await data_of(api.actions(nori_match["match"]["id"], nori_match["token"], {"type": "resign"}))
    bot_match = await data_of(
        api.create_match(
            {"kind": "gomoku", "mode_options": {"mode": "bot", "difficulty": "easy"}}
        )
    )
    match_id = bot_match["match"]["id"]
    token = bot_match["token"]

    mode_info = await data_of(api.get_mode(match_id))
    assert mode_info["mode_options"]["mode"] == "bot"
    assert mode_info["delegation"] is False

    enabled = await data_of(api.set_delegation(match_id, {"enabled": True}))
    assert enabled["delegation"] is True
    assert enabled["turn_granted"] is True
    assert plugin._director.refreshes == ["delegation_enable"]

    disabled = await data_of(api.set_delegation(match_id, {"enabled": False}))
    assert disabled["delegation"] is False
    assert disabled["in_flight_grant"] is None

    via_action = await data_of(
        api.actions(match_id, token, {"type": "delegate", "delegated": True})
    )
    assert via_action["delegation"] is True
    assert via_action["turn_granted"] is True

    await error_of(api.set_delegation(match_id, {}), 400)
    await error_of(api.set_delegation("missing", {"enabled": True}), 404)


async def test_delegate_action_requires_human_seat(tmp_path):
    api = GamehubApi(make_plugin(tmp_path))
    created = await data_of(
        api.create_match(
            {"kind": "gomoku", "mode_options": {"mode": "bot", "difficulty": "easy"}}
        )
    )
    room = api._rooms.get_room(created["match"]["id"])
    assert room is not None
    ai_token = room.match.seats[1].token or "anything"

    # Bot/AI seats never carry a token, so a non-empty unknown token is simply
    # invalid for this match.
    await error_of(
        api.actions(
            created["match"]["id"],
            ai_token,
            {"type": "delegate", "delegated": True},
        ),
        403,
    )


# ----------------------------------------------------------------------
#  WS
# ----------------------------------------------------------------------


class FakeWS:
    """Queue-backed fake WebSocket (``send_json`` / ``receive_json``)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed: tuple[int, bytes] | None = None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> Any:
        return await self.queue.get()

    async def close(self, code: int = 1000, message: bytes = b"") -> None:
        self.closed = (code, message)


async def wait_for(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


async def test_ws_first_frame_ping_and_broadcast(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    ws = FakeWS()
    task = asyncio.create_task(serve_ws(plugin, ws, match_id, created["token"]))
    try:
        await wait_for(lambda: len(ws.sent) >= 1)
        assert ws.sent[0]["type"] == "match"
        assert ws.sent[0]["match"]["me"] == 0
        assert ws_hub.subscriber_count(match_id) == 1

        await ws.queue.put({"type": "ping", "t": 7})
        await wait_for(lambda: any(p.get("type") == "pong" for p in ws.sent))
        pong = next(p for p in ws.sent if p.get("type") == "pong")
        assert pong["t"] == 7

        # Agent replies are fanned out through the match hub.
        await ws_hub.broadcast(match_id, {"type": "agent_message", "text": "你好"})
        await wait_for(
            lambda: any(p.get("type") == "agent_message" for p in ws.sent)
        )
    finally:
        await ws.queue.put(None)
        await asyncio.wait_for(task, timeout=2.0)

    # Disconnect unsubscribes from the hub.
    assert ws_hub.subscriber_count(match_id) == 0


async def test_ws_spectator_invalid_token_and_missing_match(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    spectator = FakeWS()
    await spectator.queue.put(None)
    await asyncio.wait_for(serve_ws(plugin, spectator, match_id, ""), timeout=2.0)
    assert spectator.sent[0]["type"] == "match"
    assert spectator.sent[0]["match"]["me"] is None

    invalid = FakeWS()
    await invalid.queue.put(None)
    await asyncio.wait_for(serve_ws(plugin, invalid, match_id, "nope"), timeout=2.0)
    assert invalid.sent[0] == {"type": "error", "code": "bad_token", "message": "座位口令无效，已转为观战"}
    assert invalid.sent[1]["type"] == "match"
    assert invalid.sent[1]["match"]["me"] is None

    missing = FakeWS()
    await asyncio.wait_for(serve_ws(plugin, missing, "missing", ""), timeout=2.0)
    assert missing.sent == [{"type": "error", "code": "not_found", "message": "对局不存在"}]


async def test_ws_without_rooms_reports_error(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.rooms = None
    ws = FakeWS()
    await asyncio.wait_for(serve_ws(plugin, ws, "any", ""), timeout=2.0)
    assert ws.sent == [{"type": "error", "code": "not_ready", "message": "对局服务不可用"}]
