"""GamehubRuntime start-up contract.

The runtime is the only place where the two identities meet: the host agent
name comes from the host's active persona, the ``game_agent`` name from the
plugin config. Both must reach the room layer at start, and a persona switch
must be able to refresh them without a restart.

The plugin folder name contains hyphens, so it cannot be imported as a normal
package. This module loads it under a synthetic package name with a stubbed
``core.plugin`` (the only host module the runtime imports at module scope),
which keeps the real wiring under test.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from host.settings import Settings
from room import CLOSE_STOPPING

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "gamehub_under_test"


def _stub_host_modules() -> None:
    """Provide the host modules imported at module scope by the runtime.

    Only names used at import time are needed; behaviour under test is the
    plugin's own room wiring, not the host adapter layer.
    """
    core = sys.modules.get("core")
    if core is None:
        core = types.ModuleType("core")
        core.__path__ = []  # type: ignore[attr-defined]
        sys.modules["core"] = core

    plugin_stub = sys.modules.setdefault("core.plugin", types.ModuleType("core.plugin"))
    if not hasattr(plugin_stub, "logger"):
        plugin_stub.logger = __import__("logging").getLogger("gamehub-test")

    adapter = sys.modules.setdefault("core.adapter", types.ModuleType("core.adapter"))
    adapter.__path__ = []  # type: ignore[attr-defined]
    info = sys.modules.setdefault(
        "core.adapter.adapter_info", types.ModuleType("core.adapter.adapter_info")
    )
    if not hasattr(info, "AdapterInfo"):
        info.AdapterInfo = type("AdapterInfo", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})


def _load_runtime() -> Any:
    """Import ``host.runtime`` as a package submodule of the plugin."""
    loaded = sys.modules.get(f"{PACKAGE}.host.runtime")
    if loaded is not None:
        return loaded

    _stub_host_modules()

    # The plugin root has no ``__init__.py`` (the host loads ``main.py`` as a
    # module), so alias it as a namespace package instead.
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = package

    return importlib.import_module(f"{PACKAGE}.host.runtime")


class _Repo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def save(self, match, status) -> None:
        self.rows[match.id] = match.to_dict()

    async def load_active(self):
        return []


class _Service:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def attach_judge(self, judge) -> None: ...

    def on_event(self, room, event) -> None:
        """Room-event consumer stand-in (the real TableTalk is async)."""


def _settings(**overrides: Any):
    base = {
        "agent_name": "nori",
        "practice_name": "小白",
        "max_active_matches": 5,
    }
    base.update(overrides)
    return Settings(base)


class _Plugin:
    """Minimal plugin surface used by ``GamehubRuntime.start``."""

    def __init__(self, persona_name: str | None, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.settings = _settings()
        self.stopping = False
        self._persona_name = persona_name
        self.ctx = types.SimpleNamespace(
            persona_mgr=types.SimpleNamespace(
                get_active_persona=self._get_active_persona
            ),
        )

    async def _get_active_persona(self):
        if self._persona_name is None:
            return None
        return types.SimpleNamespace(name=self._persona_name)


@pytest.fixture()
def runtime_factory(tmp_path: Path, monkeypatch):
    runtime_module = _load_runtime()
    # Peripheral services (chat/talk/judge/adapter) are out of scope here.
    monkeypatch.setattr(runtime_module, "TableTalk", lambda plugin: _Service())
    monkeypatch.setattr(runtime_module, "NoticeJudge", lambda plugin: _Service())
    monkeypatch.setattr(runtime_module, "GameAgent", lambda plugin: None)
    monkeypatch.setattr(runtime_module, "TurnDirector", None)
    monkeypatch.setattr(runtime_module, "set_outbound_sink", lambda sink: None)

    async def factory(persona_name: str | None):
        plugin = _Plugin(persona_name, tmp_path)
        runtime = runtime_module.GamehubRuntime(plugin)
        await runtime.start()
        return plugin, runtime

    return factory


async def test_start_reads_both_identities_from_their_sources(runtime_factory):
    """A missing local (e.g. a renamed variable) must fail here, not in prod."""
    plugin, _runtime = await runtime_factory("汐祈（Seki）")

    assert plugin.host_agent_name == "汐祈（Seki）"
    rooms = plugin._rooms
    assert rooms.bot_name == "汐祈（Seki）", "host seat name comes from the persona"
    assert rooms.opponent_name == "nori", "agent seat name comes from the config"


async def test_start_falls_back_to_neutral_ai_without_a_persona(runtime_factory):
    plugin, _runtime = await runtime_factory(None)
    assert plugin.host_agent_name == "AI"
    assert plugin._rooms.bot_name == "AI"


async def test_refresh_host_name_propagates_to_the_rooms(runtime_factory):
    plugin, runtime = await runtime_factory("汐祈（Seki）")
    plugin._persona_name = "阿狸"

    assert await runtime.refresh_host_name() == "阿狸"
    assert plugin.host_agent_name == "阿狸"
    assert plugin._rooms.bot_name == "阿狸"


# ----------------------------------------------------------------------
#  P1-9 regression: stop() must end with stopping → producers (incl. room
#  drive tasks) → flush_all → close_all → store.close; close truly
#  unblocks receive.
# ----------------------------------------------------------------------
class _FakeWS:
    def __init__(self) -> None:
        self.closed: tuple[int, bytes] | None = None

    async def send_json(self, payload) -> None: ...

    async def close(self, code: int = 1000, message: bytes = b"") -> None:
        self.closed = (code, message)


async def test_stop_flushes_closes_ws_then_store(runtime_factory, monkeypatch):
    plugin, runtime = await runtime_factory("汐祈（Seki）")
    rooms = runtime.rooms
    store = runtime.store

    room, _seat, _token = await rooms.create(
        kind="gomoku", actor="alice", session_id="s-alice", name="alice"
    )
    ws = _FakeWS()
    await room.attach(ws, 0)

    # Track a dirty room so flush_all matters, and record call order.
    order: list[str] = []
    original_flush = rooms.flush_all

    async def traced_flush():
        order.append("flush_all")
        return await original_flush()

    monkeypatch.setattr(rooms, "flush_all", traced_flush)
    original_close = store.close

    async def traced_close():
        order.append("store_close")
        await original_close()

    monkeypatch.setattr(store, "close", traced_close)

    await runtime.stop()

    assert plugin.stopping is True
    # Every cached room (incl. over rooms) got CLOSE_STOPPING on its sockets.
    assert ws.closed is not None and ws.closed[0] == CLOSE_STOPPING
    # flush_all and close_all both ran, and the store closed strictly last.
    assert "flush_all" in order and "store_close" in order
    assert order[-1] == "store_close"


async def test_stop_cancels_in_flight_drive_tasks(runtime_factory):
    plugin, runtime = await runtime_factory(None)
    rooms = runtime.rooms
    # A practice match whose bot seat owes a paced move = a live drive task.
    room, _seat, _token = await rooms.create(
        kind="gomoku",
        actor="alice",
        session_id="s-alice",
        name="alice",
        mode_options={"mode": "bot", "difficulty": "easy", "delegation": False},
    )
    await room.kick_ai()
    await runtime.stop()
    assert not room._drive_tasks or all(t.done() or t.cancelled() for t in room._drive_tasks)
    assert not room._bot_tasks or all(t.done() or t.cancelled() for t in room._bot_tasks)


async def test_stop_on_partially_started_runtime_is_safe(tmp_path):
    """start() may fail midway: stop() only touches what exists."""
    runtime_module = _load_runtime()
    plugin = _Plugin("汐祈（Seki）", tmp_path)
    runtime = runtime_module.GamehubRuntime(plugin)
    # Nothing was started at all.
    await runtime.stop()
    assert plugin.stopping is True
    # Partial start: only the store exists; rooms/director/talk stay None.
    from host.store import MatchStore

    runtime.store = MatchStore(tmp_path)
    await runtime.store.init()
    await runtime.stop()
    assert runtime._started is False
