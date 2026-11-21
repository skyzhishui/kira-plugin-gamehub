"""Lobby/match REST + WS: envelopes, status mapping and the WS handshake.

These tests call :class:`host.web.GamehubApi` directly (no aiohttp request or
response objects) against a real :class:`room.RoomManager` and a fake plugin.
The WS test drives :func:`host.web.serve_ws` with a queue-backed fake socket.
"""

from __future__ import annotations

import asyncio
import contextlib
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

    def _resolve(self, sid: str) -> Any:
        return SimpleNamespace(sid=sid) if self.session_ok else None

    async def resolve_session(self, sid: str) -> Any:
        """Mirror ``SessionManager.resolve_session`` (async in the host)."""
        return self._resolve(sid)

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
                "agent_name": "nori",
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
        bot_name="汐祈（Seki）",
        practice_name="小白",
    )
    return FakePlugin(rooms, tmp_path)


async def data_of(coro):
    result = await coro
    assert result["ok"] is True
    assert result["error"] == ""
    return result["data"]


@pytest.fixture(autouse=True)
def native_chat_transport(monkeypatch):
    async def publish(plugin, uid, name, text, *, role, match_id=""):
        if not hasattr(plugin.ctx, "chat_published"):
            plugin.ctx.chat_published = []
        plugin.ctx.chat_published.append((uid, name, text, role, match_id))

    monkeypatch.setattr("host.group_chat.publish_chat", publish)


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
    assert ping["page_session"] == "gamehub:gm:room"
    assert ping["actor"] == "admin"
    assert ping["ready"] is True and ping["adapter_ready"] is True
    assert {"gomoku", "reversi", "doudizhu"} <= set(ping["games"])

    session = await data_of(api.page_session())
    assert session == {"session_id": "gamehub:gm:room", "actor": "admin"}


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
    assert initial["page_session"] == "gamehub:gm:room"

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
    assert lobby["page_session"] == "gamehub:gm:room"
    assert lobby["agent_name"] == "nori"
    assert lobby["agent_avatar"] == "./assets/agent-avatar.png"
    assert lobby["match_id"] == ""
    assert lobby["current"] is None
    assert lobby["mode_options"] is None
    assert any(entry["kind"] == "gomoku" for entry in lobby["games"])
    assert lobby["notice_target_session_id"] == "gamehub:gm:room"
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
    assert scoped["chat_session"] == "gamehub:gm:room"
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


async def test_ui_config_external_not_mirrored_tracks_the_match_notice_target(tmp_path):
    """``external_not_mirrored`` mirrors where *this match's* AI lines go.

    Only an informational flag: it must never turn the chat read-only, because
    player lines always land in ``gamehub:gm:room``.
    """
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    # Page-owned match: proactive AI lines stay in the page session.
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]
    config = await data_of(api.ui_config(match_id))
    assert config["external_not_mirrored"] is False
    assert config["chat_read_only"] is False

    # Lobby (no page match yet) inherits the same default.
    assert (await data_of(api.ui_config()))["external_not_mirrored"] is False

    # A chat-origin match answers in its own chat session.
    chat_room, _seat, _token = await plugin.rooms.create(
        kind="gomoku",
        actor="webchat:dm:friend",
        session_id="webchat:dm:friend",
        name="朋友",
        source="chat",
    )
    chat_config = await data_of(api.ui_config(chat_room.match.id))
    assert chat_config["source"] == {"kind": "chat", "session_id": "webchat:dm:friend"}
    assert chat_config["external_not_mirrored"] is True
    assert chat_config["chat_read_only"] is False, "非镜像提示不得降级为只读"
    assert chat_config["chat_session"] == "gamehub:gm:room"

    # GAME_SID itself is never "external", even when recorded as seat source.
    same_room, _seat, _token = await plugin.rooms.create(
        kind="gomoku",
        actor="admin2",
        session_id="gamehub:gm:room",
        name="另一位",
        source="chat",
    )
    same_config = await data_of(api.ui_config(same_room.match.id))
    assert same_config["external_not_mirrored"] is False
    assert same_config["chat_read_only"] is False


async def test_ui_config_external_not_mirrored_follows_the_notice_override(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    # An override pointing elsewhere moves the AI's proactive lines away.
    plugin.settings.notice_target_session_id = "webchat:dm:me"
    config = await data_of(api.ui_config(match_id))
    assert config["external_not_mirrored"] is True
    assert config["notice_target_status"] == "override"
    assert config["chat_read_only"] is False

    # An override pointing back at the page session is not external.
    plugin.settings.notice_target_session_id = "gamehub:gm:room"
    config = await data_of(api.ui_config(match_id))
    assert config["external_not_mirrored"] is False
    assert config["notice_target_status"] == "override"
    assert config["chat_read_only"] is False

    # An unusable override keeps its diagnostic error visible, and must not
    # promise an external send (the destination is not actually reachable).
    plugin.settings.notice_target_session_id = "webchat:dm:me"
    plugin.ctx.session_ok = False
    config = await data_of(api.ui_config(match_id))
    assert config["notice_target_status"] == "invalid"
    assert config["notice_target_error"]
    assert config["external_not_mirrored"] is False
    assert config["chat_read_only"] is False


async def test_notice_target_awaits_both_sync_and_async_resolvers(tmp_path):
    """``SessionManager.resolve_session`` may be sync or async: awaiting must
    work for both, and a resolver returning None is reported as invalid."""
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    assert (await api._notice_target())[:2] == ("gamehub:gm:room", "default")

    # Older/simple hosts may expose a sync resolver.
    plugin.ctx.session_mgr = SimpleNamespace(
        resolve_session=lambda sid: SimpleNamespace(sid=sid)
    )
    assert (await api._notice_target())[1] == "default"

    # A resolver returning None (no session) is invalid, not usable.
    plugin = make_plugin(tmp_path)
    plugin.ctx.session_ok = False
    sid, status, error = await GamehubApi(plugin)._notice_target()
    assert status == "invalid" and error


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
    assert result["session_id"] == "gamehub:gm:room"
    assert len(plugin.ctx.published) == 1
    _session, chain, mentioned = plugin.ctx.published[0]
    assert mentioned is True
    assert "通知联调" in chain[0].text

    plugin.ctx.session_ok = False
    await error_of(api.notice_test(), 503)


async def test_ui_config_effective_target_uses_runtime_sync_resolver(tmp_path):
    """The per-match effective target follows the runtime's sync resolver."""
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    plugin.notice_target_session_sync = lambda room: "webchat:dm:resolved"
    config = await data_of(api.ui_config(match_id))
    assert config["effective_notice_target_session_id"] == "webchat:dm:resolved"
    assert config["effective_notice_target_status"] == "chat"
    assert config["external_not_mirrored"] is True
    # The configured diagnostic is preserved alongside the effective one.
    assert config["notice_target_session_id"] == "gamehub:gm:room"
    assert config["notice_target_status"] == "default"

    # A resolver returning nothing falls back to the configured target.
    plugin.notice_target_session_sync = lambda room: None
    config = await data_of(api.ui_config(match_id))
    assert config["effective_notice_target_session_id"] == "gamehub:gm:room"
    assert config["external_not_mirrored"] is False


async def test_ui_config_invalid_effective_target_never_promises_external(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    plugin.settings.notice_target_session_id = "webchat:dm:gone"
    plugin.ctx.session_ok = False
    config = await data_of(api.ui_config(match_id))
    assert config["effective_notice_target_session_id"] == "webchat:dm:gone"
    assert config["effective_notice_target_status"] == "invalid"
    assert config["effective_notice_target_error"]
    assert config["external_not_mirrored"] is False
    lobby = await data_of(api.ui_config())
    assert lobby["external_not_mirrored"] is False
    assert lobby["effective_notice_target_status"] == "invalid"

    # An unstored SID may resolve even when its adapter is not registered.
    plugin.ctx.session_mgr.resolve_session = lambda sid: SimpleNamespace(adapter_name="webchat")
    plugin.ctx.adapter_mgr = SimpleNamespace(get_adapter=lambda name: None)
    config = await data_of(api.ui_config(match_id))
    assert config["effective_notice_target_status"] == "invalid"
    assert "适配器不可用" in config["effective_notice_target_error"]
    assert config["external_not_mirrored"] is False


async def test_notice_test_match_scope(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    scoped_calls: list[tuple[str, str]] = []

    async def fake_game_notice(plugin_, body, session, match_id):
        scoped_calls.append((session.sid, match_id))

    monkeypatch.setattr("host.notices._publish_game_notice", fake_game_notice)

    # No page match: intentionally unscoped (no guess).
    result = await data_of(api.notice_test())
    assert result["match_id"] == ""
    assert scoped_calls == []

    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    # Omitted id + active page match + GAME_SID target: scoped to it.
    result = await data_of(api.notice_test())
    assert result["match_id"] == match_id
    assert scoped_calls == [("gamehub:gm:room", match_id)]

    # Explicit validated id is honoured the same way.
    result = await data_of(api.notice_test({"match_id": match_id}))
    assert result["match_id"] == match_id

    # Unknown explicit id is rejected.
    await error_of(api.notice_test({"match_id": "nope"}), 404)

    # Non-game effective target: published unscoped via the stock path.
    plugin.notice_target_session_sync = lambda room: "webchat:dm:resolved"
    plugin.ctx.session_ok = True
    result = await data_of(api.notice_test({"match_id": match_id}))
    assert result["session_id"] == "webchat:dm:resolved"
    assert result["match_id"] == ""
    assert len(scoped_calls) == 2, "non-game target must not take the scoped path"
    assert len(plugin.ctx.published) >= 1
    assert plugin.ctx.published[-1][0].sid == "webchat:dm:resolved"


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
    assert gomoku["agent_name"] == "nori"
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
    assert row["notice_target"]["session_id"] == "gamehub:gm:room"
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

        # The socket is attached to the room: applies fan live match frames
        # (the board/bubble channel — without the attach this registry stays
        # empty and the page only ever refreshes on reconnect).
        room = plugin.rooms.get_room(match_id)
        assert room is not None and room.sockets.get(ws) == 0
        version_before = room.match.version
        assert await room.apply(
            0, {"type": "move", "move": "H8"}, client_version=version_before
        ) is not None
        await wait_for(lambda: any(
            p.get("type") == "match" and p["match"]["version"] > version_before
            for p in ws.sent
        ))
    finally:
        await ws.queue.put(None)
        await asyncio.wait_for(task, timeout=2.0)

    # Disconnect unsubscribes from the hub.
    assert ws_hub.subscriber_count(match_id) == 0


async def test_ws_spectator_invalid_token_missing_match_and_no_rooms(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    # No token: spectator view.
    spectator = FakeWS()
    await spectator.queue.put(None)
    await asyncio.wait_for(serve_ws(plugin, spectator, match_id, ""), timeout=2.0)
    assert spectator.sent[0]["type"] == "match"
    assert spectator.sent[0]["match"]["me"] is None

    # Bad token: an explicit error, then the spectator view (never a seat).
    invalid = FakeWS()
    await invalid.queue.put(None)
    await asyncio.wait_for(serve_ws(plugin, invalid, match_id, "nope"), timeout=2.0)
    assert invalid.sent[0] == {
        "type": "error", "code": "bad_token", "message": "座位口令无效，已转为观战"
    }
    assert invalid.sent[1]["type"] == "match"
    assert invalid.sent[1]["match"]["me"] is None

    # Unknown match.
    missing = FakeWS()
    await asyncio.wait_for(serve_ws(plugin, missing, "missing", ""), timeout=2.0)
    assert missing.sent == [
        {"type": "error", "code": "not_found", "message": "对局不存在"}
    ]

    # Runtime not wired yet.
    plugin.rooms = None
    unwired = FakeWS()
    await asyncio.wait_for(serve_ws(plugin, unwired, "any", ""), timeout=2.0)
    assert unwired.sent == [
        {"type": "error", "code": "not_ready", "message": "对局服务不可用"}
    ]


# ----------------------------------------------------------------------
#  Host agent identity: name + selfie reference image
# ----------------------------------------------------------------------
async def test_ui_config_host_avatar_resolves_from_the_persona_accessor(tmp_path):
    """The host seat avatar is the active persona's reference image, resolved
    through ``PersonaManager.get_reference_image`` (never a guessed path) —
    and an absent persona/image/manager degrades to "" so the seat renders a
    name initial instead."""
    ref = tmp_path / "seki.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def from_image(persona_id=None):
        return ref

    async def none(persona_id=None):
        return None

    cases = [
        (SimpleNamespace(get_reference_image=from_image), True),
        (SimpleNamespace(get_reference_image=none), False),
        (None, False),  # no persona manager at all
    ]
    for persona_mgr, expect_avatar in cases:
        plugin = make_plugin(tmp_path)
        plugin.ctx.persona_mgr = persona_mgr
        config = await data_of(GamehubApi(plugin).ui_config())
        if expect_avatar:
            assert config["host_avatar"].startswith(
                "/api/plugin/kira-plugin-gamehub/host-avatar?v="
            )
        else:
            assert config["host_avatar"] == ""
        # The game_agent avatar stays a separate, plugin-owned asset.
        assert config["agent_avatar"] == "./assets/agent-avatar.png"


async def test_ui_config_names_host_and_agent_separately(tmp_path):
    """Host name comes from the runtime (active persona), agent from config."""
    plugin = make_plugin(tmp_path)
    plugin.host_agent_name = "汐祈（Seki）"
    plugin.settings.agent_name = "nori"
    api = GamehubApi(plugin)

    config = await data_of(api.ui_config())
    assert config["host_agent_name"] == "汐祈（Seki）"
    assert config["agent_name"] == "nori"
    assert config["agent_name"] != config["host_agent_name"]


async def test_lineup_labels_carry_both_identities(tmp_path):
    """Three-seat lineup copy names the host AI and the game_agent separately."""
    plugin = make_plugin(tmp_path)
    plugin.host_agent_name = "汐祈（Seki）"
    plugin.settings.agent_name = "nori"
    api = GamehubApi(plugin)

    config = await data_of(api.ui_config())
    doudizhu = next(g for g in config["games"] if g["kind"] == "doudizhu")
    labels = {item["value"]: item["label"] for item in doudizhu["lineups"]}
    assert labels["nori_agent"] == "汐祈（Seki） + nori"
    assert labels["nori_bot"] == "汐祈（Seki） + 陪练"
    assert "汐祈（Seki）" in labels["agent_bot"]
    assert "nori" in labels["agent_bot"]


async def test_ui_config_refreshes_the_host_name_from_the_persona(tmp_path):
    """A persona switch must reach the lobby without restarting the plugin."""
    plugin = make_plugin(tmp_path)
    plugin.host_agent_name = "旧名字"
    current = {"name": "汐祈（Seki）"}

    async def get_active_persona():
        return SimpleNamespace(name=current["name"])

    plugin.ctx.persona_mgr = SimpleNamespace(
        get_active_persona=get_active_persona,
        get_reference_image=lambda persona_id=None: None,
    )

    async def refresh_host_name():
        name = await plugin.ctx.persona_mgr.get_active_persona()
        plugin.host_agent_name = str(getattr(name, "name", "") or "").strip() or "AI"
        return plugin.host_agent_name

    plugin._runtime = SimpleNamespace(refresh_host_name=refresh_host_name)
    api = GamehubApi(plugin)

    config = await data_of(api.ui_config())
    assert config["host_agent_name"] == "汐祈（Seki）"

    current["name"] = "阿狸"
    config = await data_of(api.ui_config())
    assert config["host_agent_name"] == "阿狸"
    assert config["agent_name"] == "nori"  # configured game_agent name, untouched


async def test_games_payload_carries_the_host_name(tmp_path):
    """The lobby copy prints the host name, so /games must ship it.

    The page renders as soon as /games answers; if the name only arrived via
    /ui-config, the buttons would keep the neutral fallback (「与AI对战」).
    """
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    async def refresh_host_name():
        plugin.host_agent_name = "汐祈（Seki）"
        return plugin.host_agent_name

    plugin._runtime = SimpleNamespace(refresh_host_name=refresh_host_name)

    data = await data_of(api.games())
    assert data["host_agent_name"] == "汐祈（Seki）"
    doudizhu = next(entry for entry in data["games"] if entry["kind"] == "doudizhu")
    labels = [item["label"] for item in doudizhu["lineups"]]
    assert labels, "doudizhu exposes lineup labels"
    assert all("AI" not in label or "汐祈" in label for label in labels), labels
    assert any("汐祈（Seki）" in label for label in labels), labels


# ----------------------------------------------------------------------
#  Match-page chat window
# ----------------------------------------------------------------------


async def test_chat_send_publishes_and_records_the_player_line(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    missing = await error_of(api.chat_send({"text": "在吗"}), 400)
    assert missing.code == "match_required"
    match_id = (await data_of(api.create_match({"kind": "gomoku"})))["match"]["id"]

    sent = await data_of(api.chat_send({"text": "这把我该怎么走？"}))
    message = sent["message"]
    assert message["role"] == "player"
    assert message["text"] == "这把我该怎么走？"

    # The line rides the notice channel into the dedicated game session, in
    # the player's voice — not a [System:] notice the model would answer as
    # an operator.
    assert plugin.ctx.chat_published[-1] == ("admin", "Human", "这把我该怎么走？", "player", match_id)

    history = await data_of(api.chat_history(match_id=match_id))
    assert history["enabled"] is True
    assert [m["text"] for m in history["messages"]] == ["这把我该怎么走？"]
    assert history["messages"][0]["role"] == "player"


async def test_chat_send_validates_paces_and_caps_length(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    empty = await error_of(api.chat_send({"text": "   "}), 400)
    assert empty.code == "text_empty"
    (await error_of(api.chat_send({"text": "字" * 501}), 400)).code == "text_too_long"

    # The 500-char boundary is accepted at full length (no silent trim).
    await data_of(api.create_match({"kind": "gomoku"}))
    boundary = await data_of(api.chat_send({"text": "字" * 500}))
    assert len(boundary["message"]["text"]) == 500

    # Pacing: the next line inside the window is rate-limited.
    paced = await error_of(api.chat_send({"text": "又来了"}), 429)
    assert paced.code == "rate_limited"
    assert not plugin.ctx.published or len(plugin.ctx.published) == 1


async def test_chat_disabled_and_undeliverable(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)

    plugin.settings.page_chat_enabled = False
    disabled = await error_of(api.chat_send({"text": "在吗"}), 403)
    assert disabled.code == "chat_disabled"
    history = await data_of(api.chat_history())
    assert history["enabled"] is False

    plugin.settings.page_chat_enabled = True
    await data_of(api.create_match({"kind": "gomoku"}))
    import host.group_chat as chat_module

    async def failing_send(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(chat_module, "publish_chat", failing_send)
    unreachable = await error_of(api.chat_send({"text": "喂"}), 503)
    assert unreachable.code == "not_ready"


# ----------------------------------------------------------------------
#  P1-9 regression (serve_ws level): stopping via room close must release
#  the handler parked in receive.
# ----------------------------------------------------------------------
class _ClosingFakeWS(FakeWS):
    """Fake that models the real framework: a server-side close ends a
    pending ``receive`` (it raises, ``serve_ws`` treats it as disconnect)."""

    async def receive_json(self) -> Any:
        item = await self.queue.get()
        if item is None or self.closed is not None:
            raise RuntimeError("connection closed by server")
        return item

    async def close(self, code: int = 1000, message: bytes = b"") -> None:
        self.closed = (code, message)
        # Wake a receive parked in queue.get(), like the real server does.
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


async def test_stop_closing_the_room_releases_the_serve_ws_loop(tmp_path):
    plugin = make_plugin(tmp_path)
    api = GamehubApi(plugin)
    created = await data_of(api.create_match({"kind": "gomoku"}))
    match_id = created["match"]["id"]

    ws = _ClosingFakeWS()
    task = asyncio.create_task(serve_ws(plugin, ws, match_id, created["token"]))
    try:
        await wait_for(lambda: len(ws.sent) >= 1)
        room = plugin.rooms.get_room(match_id)
        assert room is not None

        # The runtime stop path: flag first, then close_all (4003).
        plugin.stopping = True
        await room.close_all()
        # The parked handler must finish WITHOUT any further client frame.
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        if not task.done():
            await ws.queue.put(None)
            await asyncio.wait_for(task, timeout=2.0)

    assert ws.closed is not None and ws.closed[0] == 4003
    assert ws_hub.subscriber_count(match_id) == 0
    assert room.sockets.get(ws) is None


# ----------------------------------------------------------------------
#  T-9 (partial): direct unit tests for host/bridge (previously only
#  covered indirectly through host.web re-exports).
# ----------------------------------------------------------------------
async def test_outbound_bridge_forwards_to_the_installed_sink_and_drops_when_unwired():
    from host.bridge import emit_outbound, set_outbound_sink

    set_outbound_sink(None)
    # Unwired: silently dropped.
    await emit_outbound("gamehub:gm:room", object())

    received: list[tuple[str, Any]] = []

    async def sink(sid: str, chain: Any) -> None:
        received.append((sid, chain))

    set_outbound_sink(sink)
    try:
        chain = object()
        await emit_outbound("gamehub:gm:room", chain)
        assert received == [("gamehub:gm:room", chain)]
    finally:
        set_outbound_sink(None)


def test_ws_hub_subscribe_broadcast_and_unsubscribe():
    """Broken sockets are dropped from fan-out without breaking others."""
    import host.bridge as bridge_module

    hub = bridge_module.MatchWSHub()

    class _Sock:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            if self.fail:
                raise RuntimeError("socket gone")
            self.sent.append(payload)

    good, bad = _Sock(), _Sock(fail=True)
    hub.subscribe("bridge-match", good)
    hub.subscribe("bridge-match", bad)
    assert hub.subscriber_count("bridge-match") == 2

    async def scenario():
        await hub.broadcast("bridge-match", {"type": "match", "match": {}})
        assert good.sent == [{"type": "match", "match": {}}]
        # The broken socket was removed; the healthy one keeps receiving.
        await hub.broadcast("bridge-match", {"type": "match", "match": {}})
        assert len(good.sent) == 2
        assert hub.subscriber_count("bridge-match") == 1
        hub.unsubscribe("bridge-match", good)
        assert hub.subscriber_count("bridge-match") == 0

    asyncio.run(scenario())


# ----------------------------------------------------------------------
#  chat_store readiness regression: construction and initialization must
#  complete under the SAME lock — a second caller may neither skip init
#  against an early-assigned engine nor return before the DDL finished
#  (parent follow-up on the lazy-store P2 fix).
# ----------------------------------------------------------------------
class _TwoPhaseStore:
    """MatchStore stand-in: assigns ``engine`` first, then blocks mid-DDL."""

    def __init__(self, data_dir: Any) -> None:
        self.data_dir = data_dir
        self.engine: Any = None
        self.init_calls = 0
        self.ddl_done = False
        self.ddl_released = asyncio.Event()

    async def init(self) -> None:
        self.init_calls += 1
        self.engine = object()  # early assignment = the old race window
        await self.ddl_released.wait()  # DDL "in progress"
        self.ddl_done = True


async def test_chat_store_second_caller_waits_for_init_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from host import store as store_module

    plugin = make_plugin(tmp_path)
    plugin._runtime = None  # force the lazily owned store path
    api = GamehubApi(plugin)
    monkeypatch.setattr(store_module, "MatchStore", _TwoPhaseStore)

    first = asyncio.create_task(api.chat_store())
    # Park init mid-flight: engine already assigned, DDL still blocked.
    await wait_for(lambda: getattr(api._own_store, "engine", None) is not None)
    assert api._own_store.engine is not None
    assert api._own_store.ddl_done is False

    second = asyncio.create_task(api.chat_store())
    await asyncio.sleep(0.05)
    # The second caller must remain pending: the old code returned here with
    # an engine that existed but a schema that did not.
    assert not second.done()
    assert not first.done()

    api._own_store.ddl_released.set()
    ready_a, ready_b = await asyncio.gather(first, second)
    assert ready_a is ready_b is api._own_store
    assert ready_a.ddl_done is True  # readiness, not just constructor count
    assert ready_a.init_calls == 1  # the recheck prevents a double init


async def test_chat_store_runtime_store_fallback_init_is_serialized(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    shared = _TwoPhaseStore(tmp_path)
    plugin._runtime = SimpleNamespace(store=shared)
    api = GamehubApi(plugin)

    first = asyncio.create_task(api.chat_store())
    await wait_for(lambda: shared.engine is not None)
    second = asyncio.create_task(api.chat_store())
    await asyncio.sleep(0.05)
    assert not second.done()  # same lock/recheck covers the runtime store

    shared.ddl_released.set()
    a, b = await asyncio.gather(first, second)
    assert a is b is shared
    assert shared.init_calls == 1 and shared.ddl_done is True


async def test_chat_store_dispose_serializes_with_in_flight_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from host import store as store_module

    plugin = make_plugin(tmp_path)
    plugin._runtime = None
    api = GamehubApi(plugin)
    monkeypatch.setattr(store_module, "MatchStore", _TwoPhaseStore)

    init_task = asyncio.create_task(api.chat_store())
    await wait_for(lambda: getattr(api._own_store, "engine", None) is not None)

    dispose_task = asyncio.create_task(api.dispose())
    await asyncio.sleep(0.05)
    assert not dispose_task.done()  # dispose waits on the same lock as init

    api._own_store.ddl_released.set()
    ready = await init_task
    await asyncio.wait_for(dispose_task, timeout=2.0)
    assert ready.ddl_done is True
    assert api._own_store is None  # disposed only after init completed
