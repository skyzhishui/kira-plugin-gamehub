"""Real HTTP/WS boundary tests for the independent spectator application."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from host.settings import Settings
from host.spectator import SpectatorServer, listener_port
from host.web import GamehubApi
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
    store = await plugin.api._chat_store()
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


async def test_group_route_buffers_spectators_and_flushes_only_on_trigger():
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
    def event(role, notice=False):
        return SimpleNamespace(session=GAME_SID, extra={"gamehub_role": role}, is_notice=notice, stop=lambda: None)
    observer = event("spectator")
    await route_group_message(plugin, observer)
    assert buffer.items == [observer] and not flushed
    player = event("player")
    await route_group_message(plugin, player)
    assert flushed == [[observer, player]]
    await route_group_message(plugin, event("spectator"))
    await route_group_message(plugin, event("system", True))
    assert len(flushed) == 2 and not buffer.items


async def test_native_group_event_against_real_host_message_classes(monkeypatch):
    """Load real message dataclasses without importing the host service graph."""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
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
