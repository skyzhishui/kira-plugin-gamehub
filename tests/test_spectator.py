"""Real HTTP/WS boundary tests for the independent spectator application."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from host.settings import Settings
from host.spectator import SpectatorServer, listener_port
from host.web import ApiError, GamehubApi
from test_web_api import make_plugin


@pytest.fixture
async def spectator(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path)
    plugin.api = GamehubApi(plugin)
    plugin.host_agent_name = "AI"
    created = (await plugin.api.create_match({"kind": "gomoku"}))["data"]
    server = SpectatorServer(plugin)
    match_id = created["match"]["id"]
    token = await server.invitation(match_id)
    client = TestClient(TestServer(server.app()), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield plugin, server, client, match_id, token, created["token"]
    finally:
        await client.close()
        if plugin.api._own_store:
            await plugin.api._own_store.close()


async def enter(client, match_id, token, name="观众甲"):
    response = await client.post("/spectator/join", json={"match_id": match_id, "token": token, "name": name})
    assert response.status == 200
    return (await response.json())["data"]


def test_default_port_and_invalid_port():
    assert listener_port(Settings({}), 5267) == 15267
    assert listener_port(Settings({"spectator_port": 16000}), 5267) == 16000
    with pytest.raises(ValueError):
        listener_port(Settings({}), 60000)
    with pytest.raises(ValueError):
        listener_port(Settings({"spectator_port": 5267}), 5267)


async def test_invitation_auth_and_read_only_surface(spectator):
    plugin, server, client, match_id, token, seat = spectator
    base = "/api/plugin/kira-plugin-gamehub"
    assert (await client.get(f"{base}/matches/{match_id}")).status == 403
    assert (await client.post("/spectator/join", json={"match_id": match_id, "token": "wrong", "name": "x"})).status == 403
    identity = await enter(client, match_id, token)
    headers = {"Authorization": "Bearer " + identity["credential"]}
    response = await client.get(f"{base}/matches/{match_id}", headers=headers)
    view = (await response.json())["data"]["match"]
    assert view["me"] is None
    assert seat not in str(view)
    wrong_match = await client.get(f"{base}/matches/another-match", headers=headers)
    assert wrong_match.status == 403
    original_cookie = client.session.cookie_jar.filter_cookies(client.make_url("/"))["gamehub_spectator"].value
    client.session.cookie_jar.clear()
    assert (await client.get(f"{base}/matches/{match_id}", headers=headers)).status == 403
    client.session.cookie_jar.update_cookies({"gamehub_spectator": original_cookie}, response_url=client.make_url("/"))
    for path in [f"/matches/{match_id}/control", "/identity", "/matches"]:
        assert (await client.get(base + path, headers=headers)).status == 404
    assert (await client.post(f"{base}/matches/{match_id}/actions", headers=headers, json={"type": "resign"})).status == 405
    assert (await client.get("/index.html")).status == 404
    page = await client.get("/match.html")
    assert 'data-spectator="true"' in await page.text()
    assert (await client.get("/shared/spectator.js")).status == 200
    assert (await client.post("/spectator/join", headers={"Origin": "https://evil.example"}, json={})).status == 403


async def test_listener_shutdown_closes_websockets(spectator):
    plugin, server, client, match_id, token, seat = spectator
    identity = await enter(client, match_id, token)
    ws = await client.ws_connect(f"/ws/plugin/kira-plugin-gamehub/matches/{match_id}/ws?access={identity['credential']}")
    await ws.receive_json()
    assert len(server.sockets) == 1
    await server.shutdown(None)
    for _ in range(4):
        frame = await asyncio.wait_for(ws.receive(), 3)
        if frame.type.name in {"CLOSE", "CLOSED"}:
            break
    assert frame.type.name in {"CLOSE", "CLOSED"}


async def test_temporary_identity_scoped_history_chat_and_expiry(spectator, monkeypatch):
    plugin, server, client, match_id, token, seat = spectator
    calls = []

    async def publish(plugin, uid, name, text, **kwargs):
        calls.append((uid, name, text, kwargs))

    monkeypatch.setattr("host.group_chat.publish_chat", publish)
    identity = await enter(client, match_id, token)
    repeated = await enter(client, match_id, token)
    assert identity["uid"] == repeated["uid"]
    headers = {"Authorization": "Bearer " + identity["credential"]}
    base = "/api/plugin/kira-plugin-gamehub"
    store = await plugin.api.chat_store()
    await store.chat_append("player", "old", "other match secret", match_id="other")
    response = await client.post(f"{base}/chat?match_id={match_id}", headers=headers, json={"text": "AI @AI 请回复", "role": "player", "uid": "admin"})
    assert response.status == 200
    message = (await response.json())["data"]["message"]
    assert message["role"] == "spectator" and message["sender"] == "观众甲"
    assert calls[-1][0] == identity["uid"] and calls[-1][3]["role"] == "spectator"
    history = await client.get(f"{base}/chat?match_id={match_id}", headers=headers)
    assert "other match secret" not in await history.text()
    assert (await client.post(f"{base}/chat?match_id={match_id}", headers=headers, json={"text": "again"})).status == 429
    ws = await client.ws_connect(f"/ws/plugin/kira-plugin-gamehub/matches/{match_id}/ws?access={identity['credential']}")
    assert (await ws.receive_json())["match"]["me"] is None
    room = await plugin.rooms.get_or_load(match_id)
    await room.apply(0, {"type": "resign"}, client_version=room.match.version)
    for _ in range(4):
        frame = await asyncio.wait_for(ws.receive(), 3)
        if frame.type.name in {"CLOSE", "CLOSED"}:
            break
    assert ws.closed or frame.type.name == "CLOSE"
    assert (await client.get(f"{base}/matches/{match_id}", headers=headers)).status == 410
    assert (await client.post("/spectator/join", json={"match_id": match_id, "token": token, "name": "x"})).status in {403, 410}


@pytest.mark.parametrize(
    "role, is_notice, trigger",
    [
        ("spectator", False, False),  # observers: buffered, never wake the host
        ("player", False, True),      # every player line is a strong signal
        ("system", True, True),       # notices always flush
    ],
)
async def test_group_route_buffers_spectators_and_flushes_only_on_trigger(
    role, is_notice, trigger
):
    """Spectator lines are buffered for context but never wake the host;
    player lines (no mention needed) and notices flush the buffer as one
    batch."""
    from host.group_chat import GAME_SID, route_group_message

    class Buffer:
        lock = asyncio.Lock()
        def __init__(self): self.items = []
        def add(self, item): self.items.append(item)
        def get_length(self): return len(self.items)
        def pop(self, count): del self.items[:count]

    buffer = Buffer()
    flushed = []
    async def flush(sid):
        flushed.append(list(buffer.items))
        buffer.items.clear()
    plugin = SimpleNamespace(ctx=SimpleNamespace(get_buffer=lambda sid: buffer, flush_session_messages=flush))
    event = SimpleNamespace(
        session=GAME_SID, extra={"gamehub_role": role},
        is_notice=is_notice, is_mentioned=False, stop=lambda: None,
    )

    await route_group_message(plugin, event)

    if trigger:
        assert len(flushed) == 1 and flushed[0] == [event]
        assert buffer.items == []
    else:
        assert buffer.items == [event] and flushed == []


async def test_native_group_event_against_real_host_message_classes(monkeypatch):
    """Load real message dataclasses without importing the host service graph."""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    def _find_host_root() -> Path | None:
        """Walk up to the checkout that actually contains ``core/``.

        The plugin may be installed at any depth (a sibling plugin dir, a
        monorepo worktree), so a hard-coded ``parents[N]`` silently skips this
        test on every other layout.
        """
        for parent in Path(__file__).resolve().parents:
            if (parent / "core" / "chat" / "message_utils.py").is_file():
                return parent
        return None

    root = _find_host_root()
    if root is None:
        pytest.skip("host checkout with core/ not found next to the plugin")
    monkeypatch.syspath_prepend(str(root))
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    package = types.ModuleType("core.chat")
    package.__path__ = [str(root / "core/chat")]
    monkeypatch.setitem(sys.modules, "core.chat", package)
    logging = types.ModuleType("core.logging_manager")
    logging.get_logger = lambda *args: __import__("logging").getLogger("test")
    monkeypatch.setitem(sys.modules, "core.logging_manager", logging)
    load("core.adapter.adapter_info", root / "core/adapter/adapter_info.py")
    load("core.chat.session", root / "core/chat/session.py")
    load("core.chat.message_elements", root / "core/chat/message_elements.py")
    messages = load("core.chat.message_utils", root / "core/chat/message_utils.py")
    from host.group_chat import publish_chat

    events = []
    async def publish(event): events.append(event)
    plugin = SimpleNamespace(ctx=SimpleNamespace(
        adapter_mgr=SimpleNamespace(get_adapter=lambda name: SimpleNamespace(info=SimpleNamespace(name="gamehub"))),
        event_bus=SimpleNamespace(publish=publish),
    ))
    await publish_chat(plugin, "match:hash", "Guest", "hello", role="spectator", match_id="match")
    event = events[-1]
    assert isinstance(event, messages.KiraMessageEvent)
    assert str(event.session) == "gamehub:gm:room"
    assert event.message.sender.user_id == "match:hash"
    assert event.message.sender.nickname == "Guest"
    assert event.extra == {"gamehub_role": "spectator", "match_id": "match"}
    assert not event.is_mentioned and not event.is_notice


async def test_player_rename_is_rejected_at_api_boundary(spectator):
    plugin, server, client, match_id, token, seat = spectator
    with pytest.raises(Exception) as error:
        await plugin.api.actions(match_id, seat, {"type": "rename", "name": "bypass"})
    assert error.value.code == "rename_lobby_only"


async def test_every_player_message_is_a_strong_signal_without_mention_or_notice():
    from host.group_chat import GAME_SID, route_group_message

    class Buffer:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.items = []

        def add(self, item): self.items.append(item)
        def get_length(self): return len(self.items)
        def pop(self, count): del self.items[:count]

    buffer = Buffer()
    batches = []

    async def flush(sid):
        assert sid == GAME_SID
        batches.append(list(buffer.items))
        buffer.items.clear()

    plugin = SimpleNamespace(ctx=SimpleNamespace(
        get_buffer=lambda sid: buffer, flush_session_messages=flush,
    ))
    for text in ("你好", "嗯", "这步不错", "不用唤醒词也应触发"):
        event = SimpleNamespace(
            session=GAME_SID, extra={"gamehub_role": "player"},
            is_notice=False, is_mentioned=False, text=text, stop=lambda: None,
        )
        await route_group_message(plugin, event)
        assert batches[-1] == [event]
    assert len(batches) == 4


# ----------------------------------------------------------------------
#  P1-10 regression: Origin check (same authority direct/TLS-proxy passes;
#  foreign/malformed rejected; configured public origin fallback) + the
#  DB-free authorize_fast fast path.
# ----------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, host: str = "10.0.0.8:15267"):
        self.host = host
        self.match_info = {"match_id": "m1"}
        self.query: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}


def _origin_allowed(server, origin, request=None):
    return server._origin_allowed(origin, request or _FakeRequest())


def test_origin_same_authority_direct_http_is_allowed():
    plugin = make_plugin(None)
    server = SpectatorServer(plugin)
    assert _origin_allowed(server, "http://10.0.0.8:15267")
    # Without a pinned public origin any scheme on the same authority is the
    # CSRF boundary — the listener cannot see a TLS-terminating proxy's scheme.
    assert _origin_allowed(server, "https://10.0.0.8:15267")


@pytest.mark.parametrize(
    "configured, host, origin, allowed",
    [
        # Regression: the proxy forwards the *public* Host verbatim, so the
        # Host branch would match the same authority. The pinned configured
        # scheme must still be enforced (no http downgrade of an https origin).
        ("https://watch.example.com", "watch.example.com", "https://watch.example.com", True),
        ("https://watch.example.com", "watch.example.com", "http://watch.example.com", False),
        # Same rule for an explicitly plaintext public URL.
        ("http://watch.example.com:8080", "watch.example.com:8080", "http://watch.example.com:8080", True),
        ("http://watch.example.com:8080", "watch.example.com:8080", "https://watch.example.com:8080", False),
        # The configured branch is authority-scoped: an origin on a *different*
        # authority that matches Host keeps the scheme-agnostic Host rule (the
        # TLS proxy path where Host is rewritten stays intact).
        ("https://watch.example.com", "internal-lan:15267", "https://internal-lan:15267", True),
        ("https://watch.example.com", "internal-lan:15267", "http://internal-lan:15267", True),
    ],
)
def test_origin_configured_scheme_is_authoritative_for_its_authority(
    configured, host, origin, allowed
):
    plugin = make_plugin(None)
    plugin.settings.spectator_public_url = configured
    server = SpectatorServer(plugin)
    assert _origin_allowed(server, origin, _FakeRequest(host=host)) is allowed


def test_origin_unparseable_configured_url_leaves_host_rule_scheme_agnostic():
    plugin = make_plugin(None)
    plugin.settings.spectator_public_url = "https://[::1"
    server = SpectatorServer(plugin)
    request = _FakeRequest(host="10.0.0.8:15267")
    assert _origin_allowed(server, "http://10.0.0.8:15267", request)
    assert _origin_allowed(server, "https://10.0.0.8:15267", request)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",       # foreign host
        "http://10.0.0.8:9999",           # same host, other port
        "null",                           # opaque origin
        "ftp://10.0.0.8:15267",           # only http(s) are origins
        "not a url",
        "",
        # Malformed authorities: url_split raises, the guard must catch it
        # and fail closed instead of surfacing a 500.
        "http://[::1",
        "https://[::1]:15267",
        "http://10.0.0.8:99999",          # invalid port
        "http://10.0.0.8:notaport",
        # Origin syntax has no credentials/path/query/fragment.
        "http://user:pass@10.0.0.8:15267",
        "http://u@10.0.0.8:15267",
        "http://10.0.0.8:15267/evil",
        "http://10.0.0.8:15267?x=1",
        "http://10.0.0.8:15267#frag",
    ],
)
def test_origin_foreign_malformed_or_dirty_is_rejected(origin):
    server = SpectatorServer(make_plugin(None))
    assert _origin_allowed(server, origin) is False


@pytest.mark.parametrize(
    "configured, origin, allowed",
    [
        # The configured origin matches on scheme AND authority.
        ("https://watch.example.com", "https://watch.example.com", True),
        ("https://watch.example.com", "http://watch.example.com", False),
        ("https://watch.example.com", "https://watch.example.com:8443", False),
        ("https://watch.example.com", "https://evil.example.com", False),
        # The config value is a base URL: a path is legitimate and stripped.
        ("https://watch.example.com/base/", "https://watch.example.com", True),
        ("https://watch.example.com/base/", "http://watch.example.com", False),
        # Strictness applies through the configured branch too.
        ("https://watch.example.com", "https://u:pw@watch.example.com", False),
        ("https://watch.example.com", "https://watch.example.com/evil", False),
        ("https://watch.example.com", "https://watch.example.com?x=1", False),
    ],
)
def test_origin_configured_public_url_matches_scheme_and_authority(
    configured, origin, allowed
):
    """Proxy target: the Host header is internal, the browser Origin is the
    configured public one. Only scheme+authority are compared."""
    plugin = make_plugin(None)
    plugin.settings.spectator_public_url = configured
    server = SpectatorServer(plugin)
    request = _FakeRequest(host="internal-lan:15267")
    assert _origin_allowed(server, origin, request) is allowed


def test_origin_malformed_configured_url_fails_closed_but_host_stays_valid():
    """A malformed configured URL must not break the Host branch or raise."""
    plugin = make_plugin(None)
    plugin.settings.spectator_public_url = "https://[::1"
    server = SpectatorServer(plugin)
    # Host branch unaffected by the broken configured value.
    assert _origin_allowed(server, "http://10.0.0.8:15267")
    # An Origin that does not match Host is rejected (never a 500).
    request = _FakeRequest(host="internal-lan:15267")
    assert _origin_allowed(server, "http://anything-else.example", request) is False


@pytest.mark.parametrize(
    "origin",
    ["http://[::1", "http://10.0.0.8:99999", "https://evil.example.com"],
)
async def test_guard_answers_403_for_rejected_origins(spectator, origin):
    """The middleware must answer 403 (never an unhandled 500) end to end."""
    _plugin, _server, client, match_id, _token, _seat_token = spectator
    resp = await client.get(
        f"/api/plugin/kira-plugin-gamehub/matches/{match_id}",
        headers={"Origin": origin},
    )
    assert resp.status == 403


async def test_authorize_fast_checks_identity_without_the_db(spectator):
    plugin, server, client, match_id, token, _seat_token = spectator
    join = await client.post(
        "/spectator/join", json={"match_id": match_id, "token": token, "name": "访客"}
    )
    credential = (await join.json())["data"]["credential"]
    cookie = client.session.cookie_jar.filter_cookies(
        client.make_url("/spectator/join")
    ).get("gamehub_spectator")
    request = _FakeRequest()
    request.match_info = {"match_id": match_id}
    request.headers["Authorization"] = f"Bearer {credential}"
    request.cookies["gamehub_spectator"] = cookie.value
    # No room reload involved: a credential that was never issued fails fast.
    stranger = _FakeRequest()
    stranger.match_info = {"match_id": match_id}
    stranger.headers["Authorization"] = "Bearer not-a-credential"
    with pytest.raises(ApiError):
        server.authorize_fast(stranger)
    server.authorize_fast(request)  # valid identity: no raise, no DB
