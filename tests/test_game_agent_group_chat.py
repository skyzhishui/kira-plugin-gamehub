"""Game agent public move lines bridge into the shared group chat.

The agent's own successful move may carry one public ``say``. That line must
reach the shared ``GAME_SID`` buffer before the director's notice for the same
move, and it must also land in the match-scoped chat read model + WS fan-out.
Host-delegated moves, rule-bot steps, stale/failed moves and internal thinking
never leak into the group. A publishing failure is cosmetic and must not block
the director or table talk.

The plugin folder name contains hyphens, so the runtime is imported under a
synthetic package name (same approach as ``test_runtime.py``); the host
``core.plugin`` logger is the only host module needed at import time.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from engine.match import Match
from host.settings import Settings
from room import RoomManager

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "gamehub_groupchat_under_test"


def _ensure_host_logger() -> None:
    """Install the minimal host surface the runtime imports at module scope.

    Only ``core.plugin.logger`` is needed. The real package is not imported on
    purpose: it opens ``<cwd>/data/log.log`` as a side effect, which would
    create artifacts when the plugin suite runs standalone. A light ``core``
    package path is still registered so later tests can import ``core.*``.
    """
    repo_root = PLUGIN_ROOT.parents[2]
    real_core = repo_root / "core"
    core = sys.modules.get("core")
    if core is None:
        core = types.ModuleType("core")
        sys.modules["core"] = core
    if not getattr(core, "__path__", None) and real_core.is_dir():
        core.__path__ = [str(real_core)]  # type: ignore[attr-defined]

    stub = sys.modules.get("core.plugin")
    if stub is None:
        stub = types.ModuleType("core.plugin")
        sys.modules["core.plugin"] = stub
    if not hasattr(stub, "logger"):
        stub.logger = __import__("logging").getLogger("gamehub-groupchat-test")


def _load_runtime() -> Any:
    loaded = sys.modules.get(f"{PACKAGE}.host.runtime")
    if loaded is not None:
        return loaded

    _ensure_host_logger()

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.host.runtime")


class _Repo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def save(self, match: Match, status: str) -> None:
        self.rows[match.id] = match.to_dict()

    async def load_active(self) -> list[Match]:
        return []


class _Store:
    """In-memory match-scoped chat read model."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    async def chat_append(
        self, role: str, sender: str, text: str, *, match_id: str = "", uid: str = "",
        ts_ms: int | None = None,
    ) -> dict[str, Any]:
        line = {
            "id": str(len(self.lines) + 1),
            "ts": 1000 + len(self.lines),
            "role": role,
            "sender": sender,
            "text": text,
            "match_id": match_id,
            "uid": uid,
        }
        self.lines.append(line)
        return line


class _FailingStore(_Store):
    async def chat_append(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("store down")


class _Hook:
    """Sync hook by default (production director/talk are sync)."""

    def __init__(self, name: str, order: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.order = order
        self.fail = fail

    def on_event(self, room: Any, event: dict[str, Any]) -> None:
        self.order.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} down")


class _AsyncHook:
    async def on_event(self, room: Any, event: dict[str, Any]) -> None:
        self.called = True


async def _drain(room: Any) -> None:
    """Wait for every room event task scheduled by ``notify_event``."""
    for _ in range(50):
        pending = [task for task in list(room._tasks) if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("room event tasks did not settle")


def _settings(**overrides: Any) -> Settings:
    base = {
        "agent_name": "nori",
        "practice_name": "小白",
        "max_active_matches": 5,
        "bot_move_delay_min_ms": 0,
        "bot_move_delay_max_ms": 0,
    }
    base.update(overrides)
    return Settings(base)


async def _agent_room() -> Any:
    manager = RoomManager(
        repository=_Repo(), config=_settings(), now_fn=lambda: 1000,
        bot_name="AI", practice_name="小白",
    )
    room, _seat, _token = await manager.create(
        kind="gomoku", actor="alice", session_id="private-alice", name="玩家",
        now=1000,
        # Explicit agent driver: a bare gomoku ai seat migrates to turn.
        mode_options={"mode": "nori", "driver": "agent"},
    )
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    return room


async def _legal_say_event(room: Any, say: str = "这手走这里") -> dict[str, Any]:
    """Capture the real room event for one successful agent move with a say."""
    events: list[dict[str, Any]] = []

    async def capture(_room: Any, event: dict[str, Any]) -> None:
        events.append(event)

    room.event_hook = capture
    match, error = await room.apply_ai_move(1, "J9", say=say)
    assert error == "" and match is not None
    await _drain(room)
    assert events, "a successful move must notify the event hook"
    return events[-1]


class _Env:
    def __init__(self, runtime_module: Any, order: list[str] | None = None) -> None:
        self.module = runtime_module
        self.publishes: list[dict[str, Any]] = []
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.order = order if order is not None else []
        self.group_chat = importlib.import_module(f"{PACKAGE}.host.group_chat")
        self.store = _Store()

    def runtime(self, *, store: Any | None = None, director: Any = None, talk: Any = None) -> Any:
        plugin = SimpleNamespace(settings=_settings(), stopping=False)
        runtime = self.module.GamehubRuntime(plugin)
        runtime.store = self.store if store is None else store
        runtime.director = director
        runtime.talk = talk
        return runtime

    def patch_publish(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
        async def publish(plugin: Any, uid: str, name: str, text: str, **kwargs: Any) -> None:
            self.order.append("publish")
            if fail:
                raise RuntimeError("group down")
            self.publishes.append({
                "uid": uid, "name": name, "text": text, **kwargs,
            })

        monkeypatch.setattr(self.group_chat, "publish_agent_say", publish)

    def patch_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def broadcast(match_id: str, payload: dict[str, Any]) -> None:
            self.frames.append((match_id, payload))

        monkeypatch.setattr(self.module.ws_hub, "broadcast", broadcast)

    def patch_hooks(self, monkeypatch: pytest.MonkeyPatch, *, runtime: Any) -> None:
        monkeypatch.setattr(runtime, "director", SimpleNamespace(on_event=_Hook("director", self.order).on_event))
        monkeypatch.setattr(runtime, "talk", SimpleNamespace(on_event=_Hook("talk", self.order).on_event))


@pytest.fixture()
def env() -> _Env:
    return _Env(_load_runtime())


async def test_legal_agent_say_publishes_group_history_and_ws(env, monkeypatch):
    room = await _agent_room()
    event = await _legal_say_event(room)
    runtime = env.runtime()
    env.patch_publish(monkeypatch)
    env.patch_ws(monkeypatch)

    await runtime._dispatch_event(room, event)

    assert env.publishes == [{
        "uid": "game_agent",
        "name": "nori",
        "text": "这手走这里",
        "match_id": room.match.id,
    }]
    assert env.store.lines == [{
        "id": "1",
        "ts": 1000,
        "role": "agent",
        "sender": "nori",
        "text": "这手走这里",
        "match_id": room.match.id,
        "uid": "game_agent",
    }]
    assert env.frames == [(room.match.id, {"type": "chat_message", **env.store.lines[0]})]
    # The original Match.chat bubble is preserved, not replaced.
    assert room.match.chat[-1].text == "这手走这里"


async def test_say_enters_buffer_before_director_notice(env, monkeypatch):
    room = await _agent_room()
    event = await _legal_say_event(room)
    runtime = env.runtime()
    env.patch_publish(monkeypatch)
    env.patch_ws(monkeypatch)
    env.patch_hooks(monkeypatch, runtime=runtime)
    room.event_hook = runtime._dispatch_event

    room.notify_event(event)
    await _drain(room)

    assert env.order == ["publish", "director", "talk"]
    # Async hooks are awaited too (director/talk may switch to async later).
    async_hook = _AsyncHook()
    runtime.director = SimpleNamespace(on_event=async_hook.on_event)
    runtime.talk = None
    await runtime._dispatch_event(room, event)
    assert async_hook.called


async def test_publish_agent_say_prebuffers_spectator_before_publishing(env, monkeypatch):
    """The helper itself must buffer first, stop, and use the spectator role."""
    built: list[dict[str, Any]] = []
    order: list[str] = []

    class Event:
        def __init__(self, role: str, match_id: str) -> None:
            self.extra = {"gamehub_role": role, "match_id": match_id}
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True
            order.append("stop")

    def build(_plugin: Any, uid: str, name: str, text: str, *, role: str, match_id: str = "") -> Event:
        built.append({"uid": uid, "name": name, "text": text, "role": role, "match_id": match_id})
        return Event(role, match_id)

    class Buffer:
        lock = asyncio.Lock()

        def __init__(self) -> None:
            self.items: list[Any] = []

        def add(self, item: Any) -> None:
            self.items.append(item)
            order.append("buffer")

        def get_length(self) -> int:
            return len(self.items)

        def pop(self, count: int) -> None:
            del self.items[:count]

    buffer = Buffer()

    async def publish(event: Any) -> None:
        order.append("publish")

    monkeypatch.setattr(env.group_chat, "_build_event", build)
    plugin = SimpleNamespace(ctx=SimpleNamespace(
        get_buffer=lambda sid: buffer,
        event_bus=SimpleNamespace(publish=publish),
    ))

    event = await env.group_chat.publish_agent_say(
        plugin, "game_agent", "nori", "该我了", match_id="match-1",
    )

    assert built == [{
        "uid": "game_agent", "name": "nori", "text": "该我了",
        "role": "spectator", "match_id": "match-1",
    }]
    assert buffer.items == [event]
    assert order[:2] == ["buffer", "stop"] and order[-1] == "publish"
    assert event.extra["gamehub_prebuffered"] is True
    assert event.stopped is True


async def test_spectator_route_does_not_double_buffer_or_trigger(env):
    from host.group_chat import GAME_SID, route_group_message

    class Buffer:
        lock = asyncio.Lock()

        def __init__(self) -> None:
            self.items: list[Any] = []

        def add(self, item: Any) -> None:
            self.items.append(item)

        def get_length(self) -> int:
            return len(self.items)

        def pop(self, count: int) -> None:
            del self.items[:count]

    buffer = Buffer()
    flushed: list[str] = []

    async def flush(sid: str) -> None:
        flushed.append(sid)

    plugin = SimpleNamespace(
        ctx=SimpleNamespace(get_buffer=lambda sid: buffer, flush_session_messages=flush)
    )
    prebuffered = SimpleNamespace(
        session=GAME_SID,
        extra={"gamehub_role": "spectator", "gamehub_prebuffered": True},
        is_notice=False,
        stop=lambda: None,
    )
    buffer.items.append(prebuffered)
    await route_group_message(plugin, prebuffered)

    assert buffer.items == [prebuffered], "pre-buffered agent line is not added twice"
    assert flushed == [], "spectator line must never arm a host reply"

    observer = SimpleNamespace(
        session=GAME_SID,
        extra={"gamehub_role": "spectator"},
        is_notice=False,
        stop=lambda: None,
    )
    await route_group_message(plugin, observer)
    assert buffer.items == [prebuffered, observer]
    assert flushed == []


async def test_host_delegated_bot_and_turn_moves_are_not_forwarded(env, monkeypatch):
    runtime = env.runtime()
    env.patch_publish(monkeypatch)
    env.patch_ws(monkeypatch)

    def snapshot(match_id: str, seats: list[str], options: dict[str, Any]) -> Any:
        return SimpleNamespace(
            id=match_id,
            version=1,
            kind="gomoku",
            mode_options=options,
            seats=[SimpleNamespace(kind=kind) for kind in seats],
        )

    cases = [
        snapshot("host", ["human", "human"], {}),
        snapshot("bot", ["human", "bot"], {"mode": "bot", "driver": "bot"}),
        snapshot("turn", ["human", "ai"], {"mode": "nori", "driver": "turn"}),
    ]
    for match in cases:
        await runtime._dispatch_event(
            SimpleNamespace(match=match),
            {"type": "move", "seat": 1, "say": "不该公开", "match": match},
        )
        await runtime._dispatch_event(
            SimpleNamespace(match=match),
            {"type": "move", "seat": 0, "say": "不该公开", "match": match},
        )

    assert env.publishes == []
    assert env.store.lines == []
    assert env.frames == []


async def test_failed_or_stale_or_silent_moves_are_not_forwarded(env, monkeypatch):
    room = await _agent_room()
    runtime = env.runtime()
    env.patch_publish(monkeypatch)
    env.patch_ws(monkeypatch)
    events: list[dict[str, Any]] = []

    async def capture(_room: Any, event: dict[str, Any]) -> None:
        events.append(event)

    room.event_hook = capture
    await _drain(room)
    events.clear()  # the start/move events fired before this hook was installed
    failed, error = await room.apply_ai_move(1, "ZZ99", say="非法也别说")
    assert failed is None and error
    await _drain(room)
    assert events == []

    # A stale (late) action is rejected inside the room lock and never notifies.
    moved, error = await room.apply_ai_move(1, "J9", say="合法一手")
    assert moved is not None and error == ""
    await _drain(room)
    assert [event["type"] for event in events] == ["move"]
    stale, _ = await room.apply_ai_move(1, "K9", expected_move_count=1, say="迟到了")
    assert stale is None
    await _drain(room)
    assert len(events) == 1

    # A move without a public say is not mirrored into the group.
    await runtime._dispatch_event(
        SimpleNamespace(match=room.match),
        {"type": "move", "seat": 1, "say": "", "match": room.match},
    )
    await runtime._dispatch_event(
        SimpleNamespace(match=room.match),
        {"type": "start", "seat": 1, "say": "开局台词", "match": room.match},
    )
    assert env.publishes == []


async def test_duplicate_event_publishes_once(env, monkeypatch):
    room = await _agent_room()
    event = await _legal_say_event(room)
    runtime = env.runtime()
    env.patch_publish(monkeypatch)
    env.patch_ws(monkeypatch)

    await runtime._dispatch_event(room, event)
    await runtime._dispatch_event(room, event)

    assert len(env.publishes) == 1
    assert len(env.store.lines) == 1
    assert len(env.frames) == 1


async def test_event_snapshot_not_late_room_match_decides_driver(env, monkeypatch):
    room = await _agent_room()
    event = await _legal_say_event(room)
    runtime = env.runtime()
    env.patch_publish(monkeypatch)
    env.patch_ws(monkeypatch)
    # The live room may have advanced; classification must use the event's own
    # snapshot instead of re-reading room.match after the fact.
    room.match = SimpleNamespace(
        id=room.match.id, version=999, kind="gomoku",
        mode_options={"mode": "nori", "driver": "turn"},
        seats=[SimpleNamespace(kind="human"), SimpleNamespace(kind="ai")],
    )

    await runtime._dispatch_event(room, event)

    assert len(env.publishes) == 1


async def test_publish_and_history_failures_do_not_block_dispatch(env, monkeypatch):
    room = await _agent_room()
    event = await _legal_say_event(room)
    runtime = env.runtime(store=_FailingStore())
    env.patch_publish(monkeypatch, fail=True)
    env.patch_ws(monkeypatch)
    env.patch_hooks(monkeypatch, runtime=runtime)

    await runtime._dispatch_event(room, event)

    assert env.order == ["publish", "director", "talk"]
    # The WS frame still goes out with the fallback payload.
    assert env.frames == [(room.match.id, {
        "type": "chat_message",
        "role": "agent",
        "sender": "nori",
        "text": "这手走这里",
        "match_id": room.match.id,
        "uid": "game_agent",
    })]
