"""Invitation-only spectator listener; no host routes or player credentials."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .bridge import ws_hub
from .web import ApiError, envelope

COOKIE = "gamehub_spectator"

def listener_port(settings, host_port: int) -> int:
    port = int(settings.spectator_port or host_port + 10000)
    if not 1 <= port <= 65535 or port == host_port:
        raise ValueError("Spectator port must be 1..65535 and different from WebUI")
    return port


class SpectatorServer:
    def __init__(self, plugin):
        self.plugin = plugin
        self.runner = None
        self.port = 0
        self.secret = secrets.token_bytes(32)
        self.invites = {}
        self.visitors = {}
        self.last_send = {}
        self.sockets = set()
        self.web_root = Path(__file__).resolve().parents[1] / "web"

    async def room(self, match_id):
        room = await self.plugin.rooms.get_or_load(match_id)
        if room is None or room.status_info().outcome is not None:
            self.invites.pop(match_id, None)
            raise ApiError(410, "观战邀请已失效", "invite_expired")
        return room

    async def invitation(self, match_id):
        await self.room(match_id)
        active_ids = {room.match.id for room in self.plugin.rooms.active_rooms()}
        self.invites = {key: value for key, value in self.invites.items() if key in active_ids}
        _, token = self.invites.setdefault(match_id, self._new_invite())
        return token

    @staticmethod
    def _new_invite():
        token = secrets.token_urlsafe(32)
        return hashlib.sha256(token.encode()).digest(), token

    def _verify_invite(self, match_id, token):
        saved = self.invites.get(match_id)
        if not saved or not hmac.compare_digest(saved[0], hashlib.sha256(token.encode()).digest()):
            raise ApiError(403, "观战口令无效", "bad_token")

    def _parse_origin(self, value: str) -> tuple[str, str] | None:
        """Strict ``Origin`` parser → ``(scheme, authority)`` or ``None``.

        Fails closed on anything that is not a bare origin:

        - ``urlsplit`` raising (malformed IPv6 such as ``http://[::1``) is
          caught here — before this fix it escaped the guard middleware and
          surfaced as a 500;
        - ``.port`` is accessed so an out-of-range/undecodable port raises
          inside the same guard (an invalid port fails closed, never 500);
        - only http/https schemes with a hostname;
        - Origin syntax carries no userinfo and no path/query/fragment, so
          any of those means the value is not an origin and is rejected even
          when the authority would match.
        """
        try:
            parsed = urlsplit(str(value or "").strip())
            if parsed.scheme not in ("http", "https"):
                return None
            if parsed.username is not None or parsed.password is not None:
                return None  # credentials are never part of an Origin
            if parsed.path:  # an origin has no path (not even "/")
                return None
            if parsed.query or parsed.fragment:
                return None
            if not parsed.hostname:
                return None
            parsed.port  # validation access; raises ValueError on bad ports
        except ValueError:
            return None
        return parsed.scheme, parsed.netloc.lower()

    def _configured_public_origin(self) -> tuple[str, str] | None:
        """``(scheme, authority)`` of ``spectator_public_url``, fail closed.

        The configured value is a base **URL** (invite links are built from
        it), so a path is legitimate and simply ignored: only scheme and
        authority are extracted. Anything that fails to parse — including a
        malformed IPv6 authority or an invalid port — yields ``None`` so the
        whitelist entry silently disappears instead of raising or matching
        loosely.
        """
        raw = str(getattr(self.plugin.settings, "spectator_public_url", "") or "").strip()
        if not raw:
            return None
        try:
            parsed = urlsplit(raw)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return None
            if parsed.username is not None or parsed.password is not None:
                return None
            if parsed.query or parsed.fragment:
                return None
            parsed.port  # validation access; raises ValueError on bad ports
        except ValueError:
            return None
        return parsed.scheme, parsed.netloc.lower()

    def _origin_allowed(self, origin: str, request) -> bool:
        """Strict same-authority Origin check (CSRF boundary; P1-10).

        - Malformed or foreign origins (unparseable, non-http(s) scheme, no
          host, credentials, path/query/fragment, invalid port, different
          authority) are rejected with 403 by the caller — never a 500.
        - No forwarded header (``X-Forwarded-*``) is ever trusted.
        - When ``spectator_public_url`` is configured its origin is
          **authoritative for that authority**: if the origin's authority is the
          configured one, the configured scheme must match too. A pinned
          deployment therefore cannot be reached with an origin of the opposite
          scheme on the same authority, even when the reverse proxy forwards the
          public ``Host`` verbatim (the Host branch below would otherwise accept
          the plaintext/``http`` downgrade).
        - Without a matching configured origin, direct access keeps the strict
          rule: the Origin authority must equal this listener's ``Host``.
          Behind a TLS-terminating reverse proxy the browser's Origin is
          ``https://`` while the listener speaks plain HTTP, and
          ``request.scheme`` cannot see that — so the **authority**
          (host:port) equality is the boundary, not the scheme (approved
          deviation; scheme stays scheme-agnostic only on this branch, and only
          where no public origin has been pinned).
        """
        parsed_origin = self._parse_origin(origin)
        if parsed_origin is None:
            return False
        origin_scheme, origin_authority = parsed_origin
        configured = self._configured_public_origin()
        if configured is not None:
            configured_scheme, configured_authority = configured
            if origin_authority == configured_authority:
                # Pinned authority: the configured scheme is part of the rule.
                return origin_scheme == configured_scheme
        host_authority = str(getattr(request, "host", "") or "").strip().lower()
        if host_authority and origin_authority == host_authority:
            return True
        return False

    async def authorize(self, request):
        match_id = request.match_info.get("match_id") or request.query.get("match_id", "")
        credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
        credential = credential or request.query.get("access", "")
        visitor = self.visitors.get(credential)
        if not visitor or visitor["match_id"] != match_id or visitor["expires"] < time.monotonic():
            raise ApiError(403, "请通过有效邀请链接进入", "bad_token")
        if not hmac.compare_digest(visitor["cookie"], request.cookies.get(COOKIE, "")):
            raise ApiError(403, "观战身份已失效", "bad_token")
        room = await self.room(match_id)
        self._verify_invite(match_id, visitor["invite"])
        return room, visitor

    def authorize_fast(self, request) -> None:
        """In-memory part of :meth:`authorize` (no DB room reload).

        Used for the per-WS-frame path: the match liveness + invite check (the
        parts that hit the database and re-derive the room) stay on the 1s
        loop in :meth:`websocket`, so a broadcast frame no longer costs a
        ``get_or_load`` plus two HMAC derivations (review P2).
        """
        match_id = request.match_info.get("match_id") or request.query.get("match_id", "")
        credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
        credential = credential or request.query.get("access", "")
        visitor = self.visitors.get(credential)
        if not visitor or visitor["match_id"] != match_id or visitor["expires"] < time.monotonic():
            raise ApiError(403, "请通过有效邀请链接进入", "bad_token")
        if not hmac.compare_digest(visitor["cookie"], request.cookies.get(COOKIE, "")):
            raise ApiError(403, "观战身份已失效", "bad_token")

    def app(self):
        @web.middleware
        async def guard(request, handler):
            origin = request.headers.get("Origin")
            if origin and not self._origin_allowed(origin, request):
                return web.json_response({"ok": False, "error": "Origin rejected"}, status=403)
            try:
                response = await handler(request)
            except ApiError as exc:
                response = web.json_response({"ok": False, "error": exc.message, "code": exc.code}, status=exc.status)
            except (ValueError, KeyError, TypeError):
                response = web.json_response({"ok": False, "error": "Invalid request"}, status=400)
            if not response.prepared:
                response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
            return response

        app = web.Application(middlewares=[guard], client_max_size=4096)
        app.router.add_post("/spectator/join", self.join)
        base = "/api/plugin/kira-plugin-gamehub"
        app.router.add_get(base + "/matches/{match_id}", self.snapshot)
        app.router.add_get(base + "/chat", self.history)
        app.router.add_post(base + "/chat", self.chat)
        app.router.add_get(base + "/ui-config", self.config)
        app.router.add_get("/ws/plugin/kira-plugin-gamehub/matches/{match_id}/ws", self.websocket)
        app.router.add_get("/{asset:.*}", self.asset)
        app.on_shutdown.append(self.shutdown)
        return app

    async def asset(self, request):
        asset = request.match_info["asset"] or "match.html"
        path = (self.web_root / asset).resolve()
        allowed = asset == "match.html" or path.suffix in {".js", ".css", ".png", ".svg", ".webp"}
        if not path.is_relative_to(self.web_root) or not path.is_file() or not allowed:
            raise web.HTTPNotFound()
        if asset == "match.html":
            text = path.read_text(encoding="utf-8").replace('<body>', '<body data-spectator="true">')
            return web.Response(text=text, content_type="text/html")
        return web.FileResponse(path)

    async def join(self, request):
        body = await request.json()
        match_id, token = str(body.get("match_id", "")), str(body.get("token", ""))
        self._verify_invite(match_id, token)
        await self.room(match_id)
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 24 or any(ord(c) < 32 for c in name):
            raise ApiError(400, "临时昵称须为 1–24 个字符", "bad_name")
        now = time.monotonic()
        self.visitors = {k: v for k, v in self.visitors.items() if v["expires"] > now and v["match_id"] in self.invites}
        live_uids = {v["uid"] for v in self.visitors.values()}
        self.last_send = {uid: at for uid, at in self.last_send.items() if uid in live_uids}
        if len(self.visitors) >= 1000:
            raise ApiError(429, "观战人数已满", "rate_limited")
        cookie = request.cookies.get(COOKIE, "")
        if len(cookie) != 64 or any(c not in "0123456789abcdef" for c in cookie):
            cookie = secrets.token_hex(32)
        uid = match_id + ":" + hmac.new(self.secret, (match_id + cookie).encode(), "sha256").hexdigest()[:32]
        credential = secrets.token_urlsafe(32)
        self.visitors[credential] = dict(match_id=match_id, invite=token, cookie=cookie, uid=uid, name=name, expires=now + 86400)
        response = web.json_response(envelope({"credential": credential, "uid": uid, "name": name}))
        response.set_cookie(COOKIE, cookie, httponly=True, secure=request.secure, samesite="Strict", max_age=86400)
        return response

    async def snapshot(self, request):
        room, _ = await self.authorize(request)
        return web.json_response(envelope({"match": room.view(None)}))

    async def history(self, request):
        room, _ = await self.authorize(request)
        data = await self.plugin.api.chat_history(match_id=room.match.id)
        return web.json_response(data)

    async def config(self, request):
        await self.authorize(request)
        settings = self.plugin.settings
        return web.json_response(envelope({"chat_enabled": settings.page_chat_enabled, "host_agent_name": self.plugin.host_agent_name, "agent_name": settings.agent_name}))

    async def chat(self, request):
        room, visitor = await self.authorize(request)
        if not self.plugin.settings.page_chat_enabled:
            raise ApiError(403, "对局聊天未开放", "chat_disabled")
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text or len(text) > 500:
            raise ApiError(400, "消息须为 1–500 个字符", "bad_request")
        now = time.monotonic()
        if now - self.last_send.get(visitor["uid"], -1e9) < 1:
            raise ApiError(429, "发言过快", "rate_limited")
        self.last_send[visitor["uid"]] = now
        from .group_chat import publish_chat

        await publish_chat(self.plugin, visitor["uid"], visitor["name"], text, role="spectator", match_id=room.match.id)
        store = await self.plugin.api.chat_store()
        line = await store.chat_append("spectator", visitor["name"], text, match_id=room.match.id, uid=visitor["uid"])
        await ws_hub.broadcast(room.match.id, {"type": "chat_message", **line})
        return web.json_response(envelope({"message": line, "enabled": True}))

    async def websocket(self, request):
        room, visitor = await self.authorize(request)
        if sum(1 for item in self.sockets if item[1] == visitor["uid"]) >= 4 or len(self.sockets) >= 200:
            raise ApiError(429, "连接过多", "rate_limited")
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1024)
        await ws.prepare(request)
        record = (ws, visitor["uid"])
        self.sockets.add(record)
        server = self

        class PublicSocket:
            async def send_json(self, payload):
                try:
                    # Cheap per-frame check (memory only). Match liveness and
                    # invite revocation — the DB/HMAC-heavy parts — stay on
                    # the 1s loop below.
                    server.authorize_fast(request)
                except ApiError:
                    await ws.close(code=4003, message=b"Invitation expired")
                    return
                await ws.send_json(payload)

        public = PublicSocket()
        ws_hub.subscribe(room.match.id, public)
        await room.attach(public, None)
        try:
            while not ws.closed:
                try:
                    await self.authorize(request)
                except ApiError:
                    await ws.close(code=4003, message=b"Invitation expired")
                    break
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break
                if message.type == web.WSMsgType.TEXT and message.data == '{"type":"ping"}':
                    await public.send_json({"type": "pong"})
        finally:
            ws_hub.unsubscribe(room.match.id, public)
            await room.detach(public)
            self.sockets.discard(record)
        return ws

    async def shutdown(self, app):
        for ws, _ in list(self.sockets):
            await ws.close(code=1001, message=b"Server shutdown")

    async def start(self):
        from core.utils.path_utils import get_data_path

        config_file = get_data_path() / "webui.json"
        config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
        self.port = listener_port(self.plugin.settings, int(config.get("port", 5267)))
        self.runner = web.AppRunner(self.app(), access_log=None, shutdown_timeout=3)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, self.plugin.settings.spectator_host, self.port).start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self.invites.clear()
        self.visitors.clear()
        self.last_send.clear()

    async def invite_url(self, match_id, origin):
        token = await self.invitation(match_id)
        base = self.plugin.settings.spectator_public_url.rstrip("/")
        if not base:
            parsed = urlsplit(origin)
            hostname = parsed.hostname or "localhost"
            if ":" in hostname:
                hostname = f"[{hostname}]"
            base = f"http://{hostname}:{self.port}"
        return f"{base}/match.html?id={match_id}#invite={token}"
