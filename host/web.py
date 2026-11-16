"""Lobby/match REST + WS logic (P3).

``main.py`` adds thin ``@register.api`` / ``@register.ws`` wrappers that call
:class:`GamehubApi`; the methods here are plain async functions so they can be
unit-tested without aiohttp request/response objects.

Envelope: every REST method returns ``{"ok": bool, "data": ..., "error": str}``
and raises :class:`ApiError` (with an HTTP-like ``status``) for failures.
``main.py`` renders the raised error into the same envelope with that status.

KiraAI differences from nori:

- Single-admin WebUI: the page actor is ``"admin"`` and the page session is
  ``GAME_SID`` (``gamehub:gm:room``); there is no identity resolver, session
  catalog or multi-platform web-user validation.
- Identity is a small ``identity.json`` in the plugin data dir holding only a
  display ``name``.
- WS is served by :func:`serve_ws` for a Starlette/FastAPI ``WebSocket``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path
from typing import Any

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

try:  # production: plugin package (``plugins.<plugin>.host.web``)
    from .. import modes
    from ..engine import games
    from ..engine.match import SOURCE_CHAT, SOURCE_WEBUI, seat_by_token, status_of
    from ..identity import MAX_NICKNAME_LEN
    from ..talk import _bot_nickname
    from ..targets import platform_of_session_id
except ImportError:  # standalone tests import plugin-root modules as top-level
    import modes
    from engine import games
    from engine.match import SOURCE_CHAT, SOURCE_WEBUI, seat_by_token, status_of
    from identity import MAX_NICKNAME_LEN
    from talk import _bot_nickname
    from targets import platform_of_session_id

from .bridge import ws_hub
from .notices import send_notice
from .avatar import avatar_url, clear_avatar, host_avatar_url, save_avatar

try:  # ``host/session.py`` imports host-only modules (core.adapter)
    from .session import GAME_SID, PAGE_USER_ID
except ImportError:  # standalone tests: keep the same literal constants
    GAME_SID = "gamehub:gm:room"
    PAGE_USER_ID = "admin"

# Single-admin WebUI: the page actor is fixed.
PAGE_ACTOR = PAGE_USER_ID

# Match-page chat window limits (kept in sync with the WebUI front end).
CHAT_MAX_CHARS = 500
CHAT_MIN_INTERVAL_S = 1.0

IDENTITY_FILE = "identity.json"
DEFAULT_DISPLAY_NAME = "Human"
MAX_DISPLAY_NAME_LEN = MAX_NICKNAME_LEN

# RoomError / engine error code -> HTTP-like status.
_STATUS_BY_CODE: dict[str, int] = {
    "not_found": 404,
    "bad_token": 403,
    "stale_version": 409,
    "game_over": 409,
    "actor_busy": 409,
    "match_full": 409,
    "bad_mode": 409,
    "no_ai_seat": 409,
    "illegal": 400,
    "bad_seat": 400,
    "bad_action": 400,
    "bad_opponent": 400,
    "bad_kind": 400,
    "bad_request": 400,
    "empty_move": 400,
    "bad_name": 400,
    "empty": 400,
}

NOTICE_TEST_TEXT = (
    "[System: 这是游戏中心的通知联调。你正在和对手进行一局游戏；"
    "用一句玩家口吻的话回应，不要用「播报/解说/本局」这类旁白话术。]"
)


class ApiError(Exception):
    """Business failure carrying an HTTP-like status for the thin wrappers."""

    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.code = str(code or "")


def envelope(data: Any = None, error: str = "") -> dict[str, Any]:
    """Success envelope (mirrors what the thin wrappers render)."""
    return {"ok": True, "data": data, "error": error or ""}


def error_envelope(exc: ApiError) -> dict[str, Any]:
    """Failure envelope for :class:`ApiError` (``main.py`` pairs it with status).

    ``error`` keeps the Chinese message as the server-side fallback; ``code``
    is the stable machine-readable key the WebUI localises (``web/shared/i18n.js``).
    """
    return {"ok": False, "data": None, "error": exc.message, "code": exc.code}


def _fail(message: str, status: int = 400, code: str = "") -> ApiError:
    return ApiError(status, message, code=code)


def _raise_room_error(exc: BaseException) -> None:
    """Map a room/engine error to :class:`ApiError` (re-raised otherwise)."""
    code = str(getattr(exc, "code", "") or "")
    if code:
        message = str(getattr(exc, "message", "") or exc)
        raise _fail(message, _STATUS_BY_CODE.get(code, 400), code=code) from exc
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        raise _fail(str(exc) or "请求不合法", 400, code="bad_request") from exc
    raise exc


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def _seat_of_actor(match: Any, actor: str) -> int | None:
    for index, seat in enumerate(match.seats):
        if seat.actor and seat.actor == actor:
            return index
    return None


def _seat_for_token(match: Any, token: str) -> int | None:
    if not token:
        return None
    return seat_by_token(match, token)


def _is_over(room: Any) -> bool:
    match = getattr(room, "match", None)
    if match is None:
        return True
    try:
        return status_of(match).outcome is not None
    except Exception:  # noqa: BLE001 - unknown kind counts as ended
        return True


class GamehubApi:
    """REST/WS assembler: every mutation goes through the room manager."""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        # Fallback when the plugin has no data dir yet (tests / early boot).
        self._identity_cache: str | None = None
        # Chat-window state: lazy store (when no runtime yet) + send pacing.
        # The lock closes the "two concurrent first requests" race where the
        # second caller could grab ``_own_store`` before its DDL finished
        # (review P2); ``init`` is idempotent, so lock + re-check is enough.
        self._own_store: Any = None
        self._own_store_lock = asyncio.Lock()
        self._chat_last_send: float = -1e9

    # ------------------------------------------------------------------
    #  Shared helpers
    # ------------------------------------------------------------------

    @property
    def _rooms(self) -> Any:
        return getattr(self._plugin, "rooms", None)

    @property
    def _settings(self) -> Any:
        return getattr(self._plugin, "settings", None)

    def _bot_name(self) -> str:
        try:
            return _bot_nickname(self._plugin)
        except Exception:  # noqa: BLE001 - cosmetic name only
            return "对手"

    def _agent_name(self) -> str:
        """Configured agent display name (advanced practice seat / UI)."""
        settings = self._settings
        name = str(getattr(settings, "agent_name", "") or "").strip()
        return name or self._bot_name()

    def _practice_name(self) -> str:
        settings = self._settings
        return str(getattr(settings, "practice_name", "") or "").strip() or "小白"

    def _require_rooms(self) -> Any:
        rooms = self._rooms
        if rooms is None:
            raise _fail("对局服务不可用（插件尚未就绪）", 503, code="not_ready")
        return rooms

    async def _room(self, match_id: str) -> Any:
        rooms = self._require_rooms()
        room = await rooms.get_or_load(str(match_id or "").strip())
        if room is None:
            raise _fail("对局不存在", 404, code="not_found")
        return room

    def _find_page_room(self) -> Any | None:
        rooms = self._rooms
        if rooms is None:
            return None
        return rooms.find_by_actor(PAGE_ACTOR)

    def _view(self, room: Any, seat: int | None) -> dict[str, Any]:
        return room.view(seat)

    def _match_url(self, match_id: str, token: str) -> str:
        base = str(getattr(self._settings, "site_base_url", "") or "").rstrip("/")
        path = f"/page/plugin/kira-plugin-gamehub/hub/match.html?id={match_id}"
        if token:
            path = f"{path}&t={token}"
        return f"{base}{path}" if base else path

    def _grant_state(self, match_id: str) -> dict[str, Any] | None:
        director = getattr(self._plugin, "_director", None)
        grant_of = getattr(director, "grant_of", None)
        if not callable(grant_of):
            return None
        try:
            current = grant_of(str(match_id or ""))
        except Exception:  # noqa: BLE001 - lookup failure means no grant
            return None
        if current is None:
            return None
        return {
            "key": getattr(current, "key", ""),
            "seat": getattr(current, "seat", None),
            "kind": getattr(current, "kind", ""),
            "state": getattr(current, "state", ""),
            "move_count": getattr(current, "move_count", None),
            "notice_sent": bool(getattr(current, "notice_sent", False)),
        }

    async def _refresh_turn(self, room: Any, reason: str) -> bool:
        """Mint a turn grant after a mode/delegation switch (best-effort)."""
        director = getattr(self._plugin, "_director", None)
        refresh = getattr(director, "refresh_turn", None)
        if not callable(refresh):
            return False
        try:
            return bool(await refresh(room, reason=reason))
        except Exception:  # noqa: BLE001 - a failed re-grant must not fail the switch
            logger.warning("gamehub: refresh_turn failed (%s)", reason, exc_info=True)
            return False

    # ------------------------------------------------------------------
    #  Identity (identity.json in the plugin data dir)
    # ------------------------------------------------------------------

    def _identity_path(self) -> Path | None:
        data_dir = getattr(self._plugin, "data_dir", None)
        if data_dir is None:
            return None
        return Path(data_dir) / IDENTITY_FILE

    def _read_identity(self) -> dict[str, str]:
        path = self._identity_path()
        if path is None:
            return {"name": self._identity_cache or DEFAULT_DISPLAY_NAME}
        name = ""
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    name = str(raw.get("name") or "").strip()
            except Exception:  # noqa: BLE001 - a broken file falls back to default
                logger.warning("gamehub: identity.json unreadable", exc_info=True)
        if name:
            self._identity_cache = name
        return {"name": name or self._identity_cache or DEFAULT_DISPLAY_NAME}

    def _write_identity(self, name: str) -> dict[str, str]:
        path = self._identity_path()
        payload = {"name": name}
        self._identity_cache = name
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
            except OSError as exc:
                raise _fail(f"身份保存失败：{exc}", 500, code="internal") from exc
        return payload

    def _display_name(self) -> str:
        return self._read_identity()["name"]

    def _identity_payload(self) -> dict[str, Any]:
        identity = self._read_identity()
        return {
            "actor": PAGE_ACTOR,
            "name": identity["name"],
            "effective_name": identity["name"],
            "page_session": GAME_SID,
        }

    # ------------------------------------------------------------------
    #  Seams
    # ------------------------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        return envelope(
            {
                "pong": True,
                "page_session": GAME_SID,
                "actor": PAGE_ACTOR,
                "ready": bool(getattr(self._plugin, "_ready", False)),
                "adapter_ready": bool(getattr(self._plugin, "_adapter_ready", False)),
                "games": [g["kind"] for g in games.catalog()],
            }
        )

    async def page_session(self) -> dict[str, Any]:
        return envelope({"session_id": GAME_SID, "actor": PAGE_ACTOR})

    async def get_identity(self) -> dict[str, Any]:
        return envelope(self._identity_payload())

    async def set_identity(self, body: Any = None) -> dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        name = str(body.get("name") or "").strip()
        if not name:
            raise _fail("昵称不能为空", 400, code="bad_name")
        if len(name) > MAX_DISPLAY_NAME_LEN:
            raise _fail(
                f"昵称最多 {MAX_DISPLAY_NAME_LEN} 个字", 400, code="bad_name"
            )
        self._write_identity(name)
        logger.info("gamehub: display name updated: %s", name)
        return envelope(self._identity_payload())

    async def ui_config(self, match_id: str = "") -> dict[str, Any]:
        """Light UI config; ``match_id`` adds the effective per-match context."""
        settings = self._settings
        talk_mode = str(getattr(settings, "page_chat_talk_default", "judge") or "judge")
        target_sid, target_status, target_error = await self._notice_target()
        base = {
            "chat_enabled": bool(getattr(settings, "page_chat_enabled", True)),
            "chat_session": GAME_SID,
            "chat_user": PAGE_ACTOR,
            "chat_read_only": False,
            "bubble_hold_s": float(getattr(settings, "page_bubble_hold_s", 3.0)),
            "talk_mode": talk_mode,
            "page_session": GAME_SID,
            "actor": PAGE_ACTOR,
            "agent_name": self._agent_name(),
            "host_agent_name": await self._refresh_host_agent_name(),
            "agent_avatar": await self._agent_avatar(),
            "host_avatar": await host_avatar_url(self._plugin),
            "practice_name": self._practice_name(),
            "games": [
                {**entry, "agent_name": self._agent_name(), "practice_name": self._practice_name()}
                for entry in games.catalog(
                    self._host_agent_name(), self._agent_name()
                )
            ],
            "notice_target_session_id": target_sid,
            "notice_target_platform": platform_of_session_id(target_sid),
            "notice_target_status": target_status,
            "notice_target_error": target_error,
        }
        match_id = str(match_id or "").strip()
        if not match_id:
            room = self._find_page_room()
            options = None
            current = None
            if room is not None:
                options = dict(modes.options_of(room.match))
                current = self._view(room, _seat_of_actor(room.match, PAGE_ACTOR))
            return envelope(
                {
                    **base,
                    "match_id": "",
                    "current": current,
                    "match": current,
                    "mode_options": options,
                    "delegation": bool((options or {}).get("delegation")),
                    "source": None,
                    "match_over": False,
                    "context_injectable": True,
                    "external_not_mirrored": False,
                }
            )

        room = await self._room(match_id)
        match = room.match
        over = _is_over(room)
        options = dict(modes.options_of(match))
        talk = getattr(self._plugin, "_talk", None)
        mode_for = getattr(talk, "_mode_for", None)
        if callable(mode_for):
            try:
                talk_mode = str(mode_for(match) or "") or talk_mode
            except Exception:  # noqa: BLE001 - per-match tier is cosmetic
                logger.debug("gamehub: per-match talk tier lookup failed", exc_info=True)
        origin = self._match_origin(match)
        view = self._view(room, _seat_of_actor(match, PAGE_ACTOR))
        return envelope(
            {
                **base,
                "talk_mode": talk_mode,
                "match_id": match.id,
                "current": view,
                "match": view,
                "mode_options": options,
                "delegation": bool(options.get("delegation")),
                "grant": self._grant_state(match.id),
                "source": dict(origin),
                "match_over": over,
                "context_injectable": not over,
                "external_not_mirrored": False,
            }
        )

    def _host_agent_name(self) -> str:
        """Display name of the host AI (the ``nori`` mode seat).

        Never a constant: the runtime stores the active persona's name on the
        plugin. An empty value means the host has not been read yet, and copy
        falls back to the neutral ``NEUTRAL_HOST_NAME``.
        """
        return str(getattr(self._plugin, "host_agent_name", "") or "").strip()

    async def _refresh_host_agent_name(self) -> str:
        """Re-read the host name so a persona switch lands without a restart."""
        runtime = getattr(self._plugin, "_runtime", None)
        refresh = getattr(runtime, "refresh_host_name", None)
        if not callable(refresh):
            return self._host_agent_name()
        try:
            return str(await refresh() or "").strip()
        except Exception:  # noqa: BLE001 - a stale name must not break the page
            logger.debug("gamehub: host name refresh failed", exc_info=True)
            return self._host_agent_name()

    async def _agent_avatar(self) -> str:
        """URL of the ``game_agent`` avatar (bundled default or uploaded file).

        The avatar is plugin-owned: the bundled ``web/assets/agent-avatar.png``
        is the default and the WebUI may replace it with an upload.
        """
        return avatar_url(self._plugin)

    async def _notice_target(self) -> tuple[str, str, str]:
        """``(session_id, status, error)`` for the effective notice target.

        Status is ``default`` (dedicated game session), ``override`` (plugin
        ``notice_target_session_id``), ``invalid`` (override/session unusable)
        or ``unknown`` (host context unavailable).
        """
        settings = self._settings
        override = str(getattr(settings, "notice_target_session_id", "") or "").strip()
        sid = override or GAME_SID
        ctx = getattr(self._plugin, "ctx", None)
        session_mgr = getattr(ctx, "session_mgr", None)
        resolver = getattr(session_mgr, "resolve_session", None)
        if not callable(resolver):
            return sid, "unknown", "session manager unavailable"
        try:
            # ``SessionManager.resolve_session`` is async; awaiting the call is
            # mandatory (a bare call leaks a coroutine and reports every target
            # as usable). Unstored SIDs still resolve to a synthetic session.
            session = resolver(sid)
            if inspect.isawaitable(session):
                session = await session
            if session is None:
                return sid, "invalid", f"会话不存在：{sid}"
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            return sid, "invalid", f"会话解析失败：{exc}"
        return sid, "override" if override else "default", ""

    def _match_origin(self, match: Any) -> dict[str, str]:
        """Chat-origin matches reply into their session; page matches use gamehub."""
        getter = getattr(self._plugin, "match_origin", None)
        if callable(getter):
            try:
                origin = getter(match)
                if isinstance(origin, dict):
                    return {str(k): str(v) for k, v in origin.items()}
            except Exception:  # noqa: BLE001 - fall back to the local rule
                logger.debug("gamehub: match_origin lookup failed", exc_info=True)
        for seat in getattr(match, "seats", []) or []:
            if getattr(seat, "source", "") == SOURCE_CHAT and getattr(
                seat, "session_id", ""
            ):
                return {"kind": SOURCE_CHAT, "session_id": str(seat.session_id)}
        return {"kind": SOURCE_WEBUI, "session_id": ""}

    async def notice_test(self) -> dict[str, Any]:
        """Publish one synthetic notice into the effective notice target."""
        sid, status, error = await self._notice_target()
        if status in ("invalid", "unknown"):
            raise _fail(f"通知目标不可用：{error or status}", 503, code="not_ready")
        sent = await send_notice(self._plugin, sid, NOTICE_TEST_TEXT)
        if not sent:
            raise _fail("notice 投递失败（会话或适配器不可用）", 503, code="not_ready")
        return envelope(
            {
                "session_id": sid,
                "platform": platform_of_session_id(sid),
                "notice_target_status": status,
                "dispatched": True,
            }
        )

    # ------------------------------------------------------------------
    #  Match-page chat window
    # ------------------------------------------------------------------

    def _chat_enabled(self) -> bool:
        settings = getattr(self._plugin, "settings", None)
        return bool(getattr(settings, "page_chat_enabled", True))

    async def chat_store(self) -> Any:
        """The live runtime store, or a lazy private one (tests / early boot).

        Public so the spectator listener can share it without reaching into
        a private attribute (review D-11).

        The whole body runs under one lock and returns only after the store
        is **initialized**: guarding construction alone still let a second
        caller grab the early-assigned ``engine`` attribute while the first
        caller's DDL was still running and query before the schema existed
        (parent follow-up on the P2 fix). The runtime store's fallback init
        shares the same lock/recheck, so a concurrent :meth:`dispose` also
        serializes against an in-flight init instead of racing it.
        """
        async with self._own_store_lock:
            runtime = getattr(self._plugin, "_runtime", None)
            store = getattr(runtime, "store", None)
            if store is None:
                if self._own_store is None:
                    from .store import MatchStore

                    self._own_store = MatchStore(self._plugin.data_dir)
                store = self._own_store
            if getattr(store, "engine", None) is None:
                await store.init()
            return store

    async def dispose(self) -> None:
        """Release the lazily opened private store (plugin terminate)."""
        async with self._own_store_lock:
            store, self._own_store = self._own_store, None
        closer = getattr(store, "close", None)
        if callable(closer):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.debug("gamehub: lazy chat store dispose failed", exc_info=True)

    async def chat_history(self, limit: int = 100, match_id: str | None = None) -> dict[str, Any]:
        """Recent chat-window lines (oldest first) + the enabled flag."""
        store = await self.chat_store()
        return envelope(
            {
                "enabled": self._chat_enabled(),
                "messages": await store.chat_recent(limit, match_id=match_id),
            }
        )

    async def chat_send(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Publish one player line into the game session and record it.

        The line rides the same notice channel as the turn notices, so the
        host model answers it in persona with the live board context injected;
        its reply reaches the page through the existing outbound bridge
        (``agent_message`` WS frame) and is recorded by the runtime.
        """
        if not self._chat_enabled():
            raise _fail("对局聊天未开放", 403, code="chat_disabled")
        text = str((body or {}).get("text") or "").strip()
        if not text:
            raise _fail("说点什么再发送吧", 400, code="text_empty")
        if len(text) > CHAT_MAX_CHARS:
            raise _fail(f"一条消息最多 {CHAT_MAX_CHARS} 字", 400, code="text_too_long")
        now = time.monotonic()
        if now - self._chat_last_send < CHAT_MIN_INTERVAL_S:
            raise _fail("发得太快了，稍等一下再发", 429, code="rate_limited")

        sender = self._display_name()
        from .group_chat import publish_chat

        match_id = str((body or {}).get("match_id") or "")
        if match_id:
            await self._room(match_id)
        try:
            await publish_chat(self._plugin, PAGE_ACTOR, sender, text, role="player", match_id=match_id)
        except Exception as exc:
            raise _fail("消息未能送达对局会话", 503, code="not_ready") from exc
        self._chat_last_send = now

        store = await self.chat_store()
        message = await store.chat_append("player", sender, text, match_id=match_id, uid=PAGE_ACTOR)
        if match_id:
            await ws_hub.broadcast(match_id, {"type": "chat_message", **message})
        return envelope({"message": message, "enabled": True})

    async def set_avatar(self, body: Any = None) -> dict[str, Any]:
        """Replace (or reset) the ``game_agent`` avatar from the WebUI.

        Body: ``{"data": "data:image/png;base64,..."}`` to upload, or
        ``{"reset": true}`` to fall back to the bundled default.
        """
        body = body if isinstance(body, dict) else {}
        if body.get("reset"):
            return envelope(clear_avatar(self._plugin))
        data = str(body.get("data") or "").strip()
        if not data:
            raise _fail("缺少头像数据", 400, code="bad_request")
        try:
            payload = save_avatar(self._plugin, data)
        except ValueError as exc:
            raise _fail(str(exc), 400, code="bad_request") from exc
        return envelope(payload)

    # ------------------------------------------------------------------
    #  Lobby
    # ------------------------------------------------------------------

    async def games(self) -> dict[str, Any]:
        room = self._find_page_room()
        current = None
        if room is not None:
            current = self._view(room, _seat_of_actor(room.match, PAGE_ACTOR))
        agent_name = self._agent_name()
        practice_name = self._practice_name()
        # The lobby copy prints the host agent's name, so the catalog payload
        # carries it too: the page can render in a single pass instead of
        # waiting for /ui-config (which would leave the neutral fallback name
        # baked into the buttons whenever /games answers first).
        host_name = await self._refresh_host_agent_name()
        catalog = [
            {**entry, "agent_name": agent_name, "practice_name": practice_name}
            for entry in games.catalog(host_name, agent_name)
        ]
        return envelope(
            {
                "host_agent_name": host_name,
                "games": catalog,
                "current": current,
                "active_matches": await self._active_match_summaries(),
            }
        )

    async def _active_match_summaries(self) -> list[dict[str, Any]]:
        rooms = self._rooms
        if rooms is None:
            return []
        active = list(rooms.active_rooms())
        active.sort(
            key=lambda room: int(getattr(room.match, "updated_at", 0) or 0),
            reverse=True,
        )
        return [await self._match_summary(room) for room in active]

    async def _match_summary(self, room: Any) -> dict[str, Any]:
        match = room.match
        try:
            game_name = games.get(match.kind).name["zh"]
        except Exception:  # noqa: BLE001 - retired kinds stay listable
            game_name = str(match.kind)
        status = room.status_info()
        names = [s.name for s in match.seats]
        if status.outcome is not None:
            turn_text = status.result_text or (status.outcome.text or "已结束")
        elif status.waiting_on:
            turn_text = "等待 " + "、".join(
                names[s] for s in status.waiting_on if 0 <= s < len(names)
            )
        else:
            turn_text = ""
        options = modes.options_of(match)
        can_control = str(getattr(room, "status", "") or "") == "active" and any(
            s.kind == "human" and s.token for s in match.seats
        )
        target_sid, status_text, status_error = await self._notice_target()
        return {
            "id": match.id,
            "kind": match.kind,
            "game_name": game_name,
            "players": [
                {"seat": i, "kind": s.kind, "actor": s.actor, "name": s.name}
                for i, s in enumerate(match.seats)
            ],
            "source": dict(self._match_origin(match)),
            "status": str(getattr(room, "status", "") or "active"),
            "move_count": len(match.log),
            "turn_text": turn_text,
            "mode_options": dict(options),
            "delegation": bool(options.get("delegation")),
            "grant": self._grant_state(match.id),
            "notice_target": {
                "session_id": target_sid,
                "platform": platform_of_session_id(target_sid),
                "status": status_text,
                "error": status_error,
            },
            "can_control": bool(can_control),
        }

    # ------------------------------------------------------------------
    #  Create / read match
    # ------------------------------------------------------------------

    async def create_match(self, body: Any = None) -> dict[str, Any]:
        rooms = self._require_rooms()
        body = body if isinstance(body, dict) else {}
        kind = str(body.get("kind") or "").strip()
        if games.maybe_get(kind) is None:
            raise _fail(f"没有这个游戏：{kind}", 400, code="bad_kind")

        nested = body.get("mode_options")
        mode_options, opponent = self._normalize_create_options(body, nested, kind)

        raw_options = body.get("options", {})
        if raw_options is None:
            raw_options = {}
        if not isinstance(raw_options, dict):
            raise _fail("options 必须是对象", 400, code="bad_request")

        name = str(body.get("name") or "").strip() or self._display_name()

        # One active match per actor: return the existing page match instead.
        existing = self._find_page_room()
        if existing is not None:
            return self._existing_match_response(existing)

        opponent_display_name = ""
        if mode_options.get("difficulty") == modes.DIFF_ADVANCED or kind == "doudizhu":
            opponent_display_name = self._agent_name()

        try:
            room, seat, token = await rooms.create(
                kind=kind,
                actor=PAGE_ACTOR,
                session_id=GAME_SID,
                opponent=opponent,
                name=name,
                source=SOURCE_WEBUI,
                mode_options=mode_options,
                opponent_name=opponent_display_name or None,
                options=raw_options,
            )
        except Exception as exc:  # noqa: BLE001 - RoomError mapping
            if str(getattr(exc, "code", "") or "") == "actor_busy":
                room = self._find_page_room()
                if room is not None:
                    return self._existing_match_response(room)
            _raise_room_error(exc)
            raise  # pragma: no cover - _raise_room_error always raises

        options = modes.options_of(room.match)
        logger.info("gamehub: page match created: %s (%s)", room.match.id, kind)
        return envelope(
            {
                "match": self._view(room, seat),
                "token": token,
                "url": self._match_url(room.match.id, token),
                "mode_options": dict(options),
            }
        )

    def _normalize_create_options(
        self, body: dict[str, Any], nested: Any, kind: str
    ) -> tuple[dict[str, Any], str]:
        """Apply nori's ``mode_options`` normalization/validation for creation."""
        # User-facing copy must name the host AI (the `nori` mode opponent) and
        # the configured game_agent (`agent` mode) separately.
        host_name = str(getattr(self._plugin, "host_agent_name", "") or "").strip()
        host_name = host_name or self._agent_name()
        if isinstance(nested, dict) and nested:
            mode = str(nested.get("mode") or "").strip().lower()
            difficulty = str(nested.get("difficulty") or "").strip().lower()
            delegation = bool(nested.get("delegation"))
            opponent = ""
        else:
            mode = str(body.get("mode") or "").strip().lower()
            difficulty = str(body.get("difficulty") or "").strip().lower()
            delegation = bool(body.get("delegation"))
            opponent = str(body.get("opponent") or "").strip().lower()
        if opponent == "nori":
            opponent = "ai"

        if mode == modes.MODE_NORI:
            if difficulty:
                raise _fail(
                    "mode=nori 固定标准难度（difficulty 仅 bot 模式有效）",
                    400,
                    code="bad_request",
                )
            mode_options: dict[str, Any] = {
                "mode": modes.MODE_NORI,
                "difficulty": modes.DIFF_STANDARD,
                "delegation": False,
            }
            opponent = "ai"
        elif mode == modes.MODE_BOT:
            if difficulty not in ("", modes.DIFF_EASY, modes.DIFF_ADVANCED):
                raise _fail("difficulty 只能是 easy 或 advanced", 400, code="bad_request")
            mode_options = {
                "mode": modes.MODE_BOT,
                "difficulty": difficulty or modes.DIFF_EASY,
                "delegation": delegation,
            }
            opponent = "advanced" if difficulty == modes.DIFF_ADVANCED else "bot"
        elif mode:
            raise _fail(
                f"不支持的模式：{mode}（nori=对{host_name}本人，bot=对战陪练）",
                400,
                code="bad_request",
            )
        elif opponent in ("", "ai"):
            mode_options = {
                "mode": modes.MODE_NORI,
                "difficulty": modes.DIFF_STANDARD,
                "delegation": False,
            }
            opponent = "ai"
        elif opponent == "bot":
            if difficulty not in ("", modes.DIFF_EASY, modes.DIFF_ADVANCED):
                raise _fail("difficulty 只能是 easy 或 advanced", 400, code="bad_request")
            mode_options = {
                "mode": modes.MODE_BOT,
                "difficulty": difficulty or modes.DIFF_EASY,
                "delegation": delegation,
            }
            opponent = "advanced" if difficulty == modes.DIFF_ADVANCED else "bot"
        else:
            raise _fail(
                f"对手只能是{host_name}本人（nori/ai）或陪练（bot）",
                400,
                code="bad_opponent",
            )

        if kind == "doudizhu":
            try:
                mode_options = modes.normalize_mode_options(
                    nested if isinstance(nested, dict) else {}, kind=kind
                )
            except ValueError as exc:
                raise _fail(str(exc), 400, code="bad_request") from exc
        elif kind in ("gomoku", "reversi") and isinstance(nested, dict):
            mode_options["level"] = nested.get("level", "intermediate")
        return mode_options, opponent

    def _existing_match_response(self, room: Any) -> dict[str, Any]:
        seat = _seat_of_actor(room.match, PAGE_ACTOR)
        token = room.match.seats[seat].token if seat is not None else ""
        return envelope(
            {
                "match": self._view(room, seat),
                "token": token,
                "url": self._match_url(room.match.id, token),
                "mode_options": dict(modes.options_of(room.match)),
                "existing": True,
            }
        )

    async def get_match(self, match_id: str, token: str = "") -> dict[str, Any]:
        room = await self._room(match_id)
        token = str(token or "").strip()
        seat = _seat_for_token(room.match, token) if token else None
        if token and seat is None:
            raise _fail("座位口令无效", 403, code="bad_token")
        return envelope({"match": self._view(room, seat)})

    # ------------------------------------------------------------------
    #  Control / actions
    # ------------------------------------------------------------------

    async def control(self, match_id: str, seat: int | None = None) -> dict[str, Any]:
        """Return the seat token for the requesting (human) seat only."""
        room = await self._room(match_id)
        match = room.match
        if _is_over(room):
            raise _fail("对局已结束，没有可操作的座位", 409, code="game_over")
        candidates = [
            i
            for i, info in enumerate(match.seats)
            if info.kind == "human" and bool(info.token)
        ]
        if not candidates:
            raise _fail(
                "该对局没有可操作的人类座位（对手座位不提供口令）",
                409,
                code="game_over",
            )
        requested = _int_or_none(seat)
        if requested is not None:
            if requested not in candidates:
                raise _fail("该座位不可操作（只能控制人类座位）", 403, code="bad_seat")
            chosen = requested
        else:
            chosen = candidates[0]
        info = match.seats[chosen]
        return envelope(
            {
                "match_id": match.id,
                "seat": chosen,
                "token": info.token,
                "url": self._match_url(match.id, info.token),
                "name": info.name,
                "actor": info.actor,
                "source": dict(self._match_origin(match)),
            }
        )

    async def actions(self, match_id: str, token: str, body: Any = None) -> dict[str, Any]:
        rooms = self._require_rooms()
        room = await self._room(match_id)
        match_id = room.match.id
        token = str(token or "").strip()
        if not token:
            raise _fail("缺少座位口令", 403, code="bad_token")
        if not isinstance(body, dict):
            raise _fail("请求体必须是 JSON 对象", 400, code="bad_action")
        action = dict(body)
        if action.get("type") == "rename":
            raise _fail("请在游戏大厅修改玩家昵称", 403, code="rename_lobby_only")
        client_version = _int_or_none(action.pop("client_version", None))
        if str(action.get("type") or "") == "delegate":
            return await self._delegate_action(room, token, client_version, action)
        try:
            match = await rooms.apply_for_token(
                match_id, token, action, client_version=client_version
            )
        except Exception as exc:  # noqa: BLE001 - RoomError mapping
            _raise_room_error(exc)
            raise  # pragma: no cover
        seat = _seat_for_token(match, token)
        return envelope({"match": self._view(room, seat)})

    async def _delegate_action(
        self,
        room: Any,
        token: str,
        client_version: int | None,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegation toggle from the seat-token action face."""
        match = room.match
        seat = _seat_for_token(match, token)
        if seat is None:
            raise _fail("座位口令无效", 403, code="bad_token")
        if match.seats[seat].kind != "human":
            raise _fail("委托只针对人类座位", 403, code="bad_seat")
        options = modes.options_of(match)
        if options.get("mode") != modes.MODE_BOT:
            raise _fail(
                f"只有对战陪练模式（mode=bot）支持把座位委托给{self._agent_name()}代走",
                409,
                code="bad_mode",
            )
        enabled = action.get("delegated")
        if enabled is None:
            enabled = True
        try:
            merged = await room.set_mode_options(
                delegation=bool(enabled), client_version=client_version
            )
        except Exception as exc:  # noqa: BLE001 - RoomError mapping
            _raise_room_error(exc)
            raise  # pragma: no cover
        granted = False
        if enabled:
            granted = await self._refresh_turn(room, "delegate_action")
        return envelope(
            {
                "match": self._view(room, seat),
                "mode_options": dict(merged),
                "delegation": bool(merged.get("delegation")),
                "turn_granted": granted,
            }
        )

    # ------------------------------------------------------------------
    #  Mode / delegation (REST)
    # ------------------------------------------------------------------

    async def get_mode(self, match_id: str) -> dict[str, Any]:
        room = await self._room(match_id)
        options = modes.options_of(room.match)
        return envelope(
            {
                "match_id": room.match.id,
                "mode_options": dict(options),
                "driver": options.get("driver"),
                "delegation": bool(options.get("delegation")),
                "grant": self._grant_state(room.match.id),
            }
        )

    async def set_mode(self, match_id: str, body: Any = None) -> dict[str, Any]:
        room = await self._room(match_id)
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("mode") or "").strip().lower()
        difficulty = str(body.get("difficulty") or "").strip().lower()
        options, error = modes.validate_mode_switch(
            room.match, mode=mode, difficulty=difficulty
        )
        if options is None:
            raise _fail(error or "模式切换不被允许", 409, code="bad_mode")
        changes: dict[str, Any] = {"mode": mode, "difficulty": difficulty}
        if "level" in body:
            changes["level"] = body["level"]
        try:
            merged = await room.set_mode_options(**changes)
        except Exception as exc:  # noqa: BLE001 - RoomError mapping
            _raise_room_error(exc)
            raise  # pragma: no cover
        granted = await self._refresh_turn(room, "mode_switch")
        return envelope(
            {
                "match_id": room.match.id,
                "mode_options": dict(merged),
                "turn_granted": granted,
            }
        )

    async def set_delegation(self, match_id: str, body: Any = None) -> dict[str, Any]:
        room = await self._room(match_id)
        if not isinstance(body, dict) or "enabled" not in body:
            raise _fail('请求体必须是 {"enabled": true|false}', 400, code="bad_request")
        enabled = bool(body.get("enabled"))
        options = modes.options_of(room.match)
        if options.get("mode") != modes.MODE_BOT:
            raise _fail(
                f"只有对战陪练模式（mode=bot）支持把座位委托给{self._agent_name()}代走",
                409,
                code="bad_mode",
            )
        try:
            merged = await room.set_mode_options(delegation=enabled)
        except Exception as exc:  # noqa: BLE001 - RoomError mapping
            _raise_room_error(exc)
            raise  # pragma: no cover
        granted = await self._refresh_turn(room, "delegation_enable") if enabled else False
        return envelope(
            {
                "match_id": room.match.id,
                "delegation": bool(merged.get("delegation")),
                "mode_options": dict(merged),
                "turn_granted": granted,
                # Disabling is stop-next-turn: in-flight grants still complete.
                "in_flight_grant": None
                if not enabled
                else self._grant_state(room.match.id),
            }
        )


async def serve_ws(
    plugin: Any, websocket: Any, match_id: str, token: str = ""
) -> None:
    """Serve one match WS connection until the client disconnects.

    - invalid token → ``{"type": "error"}`` then continue as a spectator;
    - first frame is the full ``{"type": "match", "match": <view>}`` snapshot;
    - ``{"type": "ping"}`` → ``{"type": "pong"}`` (echoes ``t``);
    - moves go through REST, so any other frame is ignored.
    """
    rooms = getattr(plugin, "rooms", None)
    match_id = str(match_id or "").strip()
    token = str(token or "").strip()

    async def send(payload: dict[str, Any]) -> None:
        send_json = getattr(websocket, "send_json", None)
        if callable(send_json):
            await send_json(payload)
            return
        send_str = getattr(websocket, "send_text", None)
        if callable(send_str):
            await send_str(json.dumps(payload, ensure_ascii=False))
            return
        raise TypeError("websocket lacks send_json/send_text")

    async def receive() -> Any:
        receive_json = getattr(websocket, "receive_json", None)
        if callable(receive_json):
            try:
                return await receive_json()
            except Exception:  # noqa: BLE001 - closed/binary frame ends the loop
                return None
        receive_text = getattr(websocket, "receive_text", None)
        if callable(receive_text):
            try:
                return json.loads(await receive_text())
            except Exception:  # noqa: BLE001 - closed/invalid frame ends the loop
                return None
        return None

    if rooms is None:
        await send({"type": "error", "code": "not_ready", "message": "对局服务不可用"})
        return

    room = await rooms.get_or_load(match_id)
    if room is None:
        await send({"type": "error", "code": "not_found", "message": "对局不存在"})
        return

    seat = _seat_for_token(room.match, token) if token else None
    if token and seat is None:
        await send(
            {"type": "error", "code": "bad_token", "message": "座位口令无效，已转为观战"}
        )

    ws_hub.subscribe(room.match.id, websocket)
    await send({"type": "match", "match": room.view(seat)})
    # Attach the socket to the room: every apply fans a live ``{"type":
    # "match"}`` frame through room.sockets. Without this the socket lived
    # only in ws_hub — the chat window stayed live while the board and the
    # seat bubbles silently froze until a reconnect (the page has no
    # polling, and pings keep the connection "healthy" forever). The attach
    # also re-enqueues the initial view via the room's own writer; the
    # duplicate first frame is harmless (the page applies views idempotently,
    # guarded by monotonic versions).
    await room.attach(websocket, seat)
    try:
        while True:
            # Live check: ``runtime.stop`` flips ``plugin.stopping`` first and
            # then closes this socket via ``room.close_all`` (4003), which
            # makes the blocking ``receive()`` below return/raise — so the
            # flag is actually observed without waiting for a client frame.
            if getattr(plugin, "stopping", False):
                await send({"type": "error", "code": "not_ready", "message": "插件正在停止"})
                break
            data = await receive()
            if data is None:
                break
            if not isinstance(data, dict):
                continue
            if str(data.get("type") or "") == "ping":
                await send({"type": "pong", "t": data.get("t")})
    finally:
        await room.detach(websocket)
        ws_hub.unsubscribe(room.match.id, websocket)
