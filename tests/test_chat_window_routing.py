"""P0 regression: match-page chat window routing across overlapping rooms.

Scenario (real objects; no LLM provider):

* Room A and room B both live in the shared ``gamehub:gm:room`` session, and
  both are driven by the page actor ``PAGE_ACTOR`` (the same identity for A and
  then B, after A is resigned). This proves the lifecycle, not two actors.
* Player lines enter through the real ``GamehubApi.chat_send`` ingress and the
  real ``host.group_chat.publish_chat`` event builder, then go through the real
  ``route_group_message`` partitioned flush into the real
  ``SessionBufferManager`` + ``publish_buffered_messages`` batching.
* Notices are published through the real ``host.notices.send_notice`` against a
  fake host ctx (``session_mgr.resolve_session`` + ``publish_notice``). Its
  game-session branch builds the event and publishes it on the bus; the test
  asserts on what ``send_notice`` actually did, not on a built event alone.
* Each delivered reply is attributed by the real ``GamehubPlugin.record_window_reply``
  (``ON_MESSAGE_SENT``) hook, which writes through ``GamehubRuntime``.

Checked against the real ``MatchStore`` history and the shared ``ws_hub``
frames. No real LLM: the reply is the delivered chain the host would send.

The plugin ``main`` module is the ``MAIN`` alias loaded by
``test_main_turn_hooks`` (one loader, one hook class across the suite).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = next((p for p in PLUGIN_ROOT.parents if (p / "core" / "chat").is_dir()), None)
if REPO_ROOT is None:
    pytest.skip("requires a KiraAI host checkout", allow_module_level=True)
for _path in (PLUGIN_ROOT, REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

try:
    from core.chat.message_elements import Text
    from core.chat.message_utils import MessageChain
    from core.chat.session import Session
    from core.chat.session_buffer import SessionBufferManager
except ModuleNotFoundError as exc:
    if not exc.name or exc.name.startswith("core"):
        raise
    pytest.skip(f"host dependency unavailable: {exc.name}", allow_module_level=True)

from test_main_turn_hooks import MAIN  # noqa: E402  (shared alias + core.plugin stubs)
from host import group_chat  # noqa: E402
from host.bridge import ws_hub  # noqa: E402
from host.notices import send_notice  # noqa: E402
from host.session import GAME_SID  # noqa: E402
from host.settings import Settings  # noqa: E402
from host.store import MatchStore  # noqa: E402
from host.web import PAGE_ACTOR, GamehubApi  # noqa: E402
from room import RoomManager  # noqa: E402

_BATCHING_PATH = REPO_ROOT / "core" / "workflow" / "src" / "im" / "batching.py"
_ALIAS = MAIN.__package__  # synthetic package the plugin ``main`` lives under


def _load_real_batching() -> Any:
    """Load the real ``publish_buffered_messages`` by file path.

    The ``core.workflow`` package ``__init__`` pulls the agent/provider stack
    (``jinja2``/``openai``) that the plugin venv does not need for batching.
    The module's only ``core`` import is ``core.chat`` (real).
    """
    spec = importlib.util.spec_from_file_location("gamehub_real_batching", _BATCHING_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_batching = _load_real_batching()


class _RecordingBus:
    """Captures batch events and routes inbound group events like the host bus.

    ``publish`` of a group line runs the real ``route_group_message`` (the
    plugin's own router) so ``chat_send`` / ``send_notice`` exercise the real
    batching; batch events are recorded for assertions.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.router: Any = None

    async def publish(self, event: Any) -> None:
        if self.router is not None and not hasattr(event, "messages"):
            await self.router(event)
            return
        self.events.append(event)


class _Adapter:
    name = "gamehub"
    platform = "gamehub"


class _AdapterHandle:
    """What ``adapter_mgr.get_adapter("gamehub")`` returns; ``.info`` feeds events."""

    info = _Adapter()


class _AdapterMgr:
    def get_adapter(self, name: str):
        return _AdapterHandle() if name == "gamehub" else None


class _FakeCtx:
    """Host ctx surface: real session buffers, real batching, a recording bus.

    ``session_mgr`` and ``publish_notice`` mirror the host surface that
    ``send_notice`` probes. ``publish_notice`` is the stock (non-game) path and
    records its calls, so a test can prove a game notice never takes it.
    """

    def __init__(self) -> None:
        self.session_buffer_mgr = SessionBufferManager(max_count=100)
        self.event_bus = _RecordingBus()
        self.adapter_mgr = _AdapterMgr()
        self.flushed: list[str] = []
        self.stock_notices: list[Any] = []
        self.session_mgr = SimpleNamespace(resolve_session=self._resolve)

    def _resolve(self, sid: str) -> Any:
        # The real Session: ``str(session)`` is ``adapter:type:id``, the form
        # ``route_group_message`` compares against ``GAME_SID``.
        adapter, session_type, session_id = sid.split(":", 2)
        return Session(adapter_name=adapter, session_type=session_type, session_id=session_id)

    async def publish_notice(self, session: Any, chain: Any, **kwargs: Any) -> None:
        self.stock_notices.append((session, kwargs))

    def get_buffer(self, sid: str) -> Any:
        return self.session_buffer_mgr.get_buffer(sid)

    async def flush_session_messages(
        self, sid: str, filter_fn=None,
    ) -> bool:
        """Mirror ``PluginContext.flush_session_messages`` over the real batching."""
        self.flushed.append(sid)
        return await _batching.publish_buffered_messages(
            self.session_buffer_mgr, self.event_bus, sid, filter_fn=filter_fn,
        )


class _WS:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)


def _runtime_module() -> Any:
    """``host.runtime`` under the same alias as ``MAIN`` (its ``ws_hub`` is rebound below)."""
    return importlib.import_module(f"{_ALIAS}.host.runtime")


@pytest.fixture()
async def world(tmp_path: Path, monkeypatch):
    """Real store, rooms, runtime, plugin hook and batching over a temp dir."""
    store = MatchStore(tmp_path)
    await store.init()
    rooms = RoomManager(
        repository=store,
        config=Settings({"max_active_matches": 5}),
        bot_name="汐祈（Seki）",
        practice_name="小白",
        opponent_name="nori",
    )
    ctx = _FakeCtx()
    settings = Settings({
        "max_active_matches": 5, "agent_name": "nori", "practice_name": "小白",
        "page_chat_enabled": True, "page_chat_talk_default": "judge",
        "page_bubble_hold_s": 3.0, "site_base_url": "",
    })
    plugin = SimpleNamespace(
        data_dir=tmp_path, rooms=rooms, ctx=ctx, settings=settings,
        host_agent_name="汐祈", notice_target=None, stopping=False,
        _ready=True, _adapter_ready=True,
        match_origin=lambda match: {"kind": "webui", "session_id": ""},
    )

    # Real hook wiring: the same GamehubRuntime the plugin starts, with the real
    # store and rooms. ``ws_hub`` is rebound so the synthetic copy broadcasts to
    # the same hub the page API and these tests subscribe to.
    runtime_mod = _runtime_module()
    monkeypatch.setattr(runtime_mod, "ws_hub", ws_hub)
    runtime = runtime_mod.GamehubRuntime(plugin)
    runtime.store = store
    runtime.rooms = rooms
    plugin._runtime = runtime

    # The real GamehubPlugin hook methods, bound to this lightweight instance.
    hook_plugin = MAIN.GamehubPlugin.__new__(MAIN.GamehubPlugin)
    hook_plugin.runtime = runtime

    # Route every published group line through the real plugin router, so
    # ``chat_send`` and the notice path reach the real batching end to end.
    async def _router(event):
        await group_chat.route_group_message(plugin, event)

    ctx.event_bus.router = _router
    api = GamehubApi(plugin)
    yield SimpleNamespace(
        store=store, rooms=rooms, plugin=plugin, ctx=ctx, api=api,
        hook=hook_plugin, runtime=runtime,
    )
    await store.close()


def _subscribe(match_id: str) -> _WS:
    ws = _WS()
    ws_hub.subscribe(match_id, ws)
    return ws


def _unsubscribe(match_id: str, ws: _WS) -> None:
    ws_hub.unsubscribe(match_id, ws)


async def _open_room(world, kind: str = "gomoku"):
    """One room for ``PAGE_ACTOR`` (the page's single identity)."""
    room, _seat, _token = await world.rooms.create(
        kind=kind, actor=PAGE_ACTOR, session_id=GAME_SID, opponent="ai",
        name="Human", source="webui",
    )
    return room


async def _send(world, text: str, match_id: str):
    """Real ``chat_send`` ingress: the real ``publish_chat`` builds the event.

    The recording bus routes it through real batching. Persistence and the
    player WS frame come from ``chat_send`` itself.
    """
    world.api._chat_last_send = -1e9
    return (await world.api.chat_send({"text": text, "match_id": match_id}))["data"]


async def _notice(world, text: str, match_id: str = "") -> bool:
    """Publish a notice through the real ``send_notice`` (fake host ctx).

    ``send_notice`` resolves the game session, builds the scoped notice event
    and publishes it on the bus, which routes it through real batching.
    """
    return await send_notice(world.plugin, GAME_SID, text, match_id=match_id)


async def _deliver_reply(world, batch, text: str, *, ok: bool = True):
    """The host's ON_MESSAGE_SENT call for one delivered reply.

    Drives the real ``record_window_reply`` hook with the batch that triggered
    the reply and a delivery result, exactly as ``execute.py`` does.
    """
    chain = MessageChain([Text(text)])
    result = SimpleNamespace(ok=ok, err="" if ok else "delivery failed")
    await world.hook.record_window_reply(batch, chain, result)


def _texts(rows: list[dict[str, Any]]) -> list[str]:
    return [row["text"] for row in rows]


def _last_batch(world):
    return world.ctx.event_bus.events[-1]


# ----------------------------------------------------------------------
#  1) Lifecycle: same PAGE_ACTOR, room A resigned, then room B
# ----------------------------------------------------------------------


async def test_ended_room_replies_stay_in_their_room_after_new_room_opens(world):
    a = await _open_room(world)
    await world.rooms.resign_for_actor(PAGE_ACTOR)
    assert a.status != "active"

    # Keep A's reply pending while the same page actor starts a new match.
    await _send(world, "A 局还能继续吗？", a.match.id)
    batch_a = _last_batch(world)
    assert batch_a.sid == GAME_SID
    assert [m.extra["match_id"] for m in batch_a.messages] == [a.match.id]

    b = await _open_room(world)  # same PAGE_ACTOR opens a new room after A ended
    assert b.status == "active" and b.match.id != a.match.id

    ws_a = _subscribe(a.match.id)
    ws_b = _subscribe(b.match.id)
    try:
        # A notice for the NEW room B, published by the real send_notice.
        assert await _notice(world, "B 的回合通知", match_id=b.match.id) is True
        batch_b = _last_batch(world)
        assert batch_b.sid == GAME_SID
        assert [m.extra["match_id"] for m in batch_b.messages] == [b.match.id]
        assert world.ctx.stock_notices == [], "game notice must not take the stock path"

        await _deliver_reply(world, batch_b, "B 的回复")
        # A's reply is delivered only now, after B has already been answered
        # (a true late A, after the newer room's reply).
        await _deliver_reply(world, batch_a, "A 的回复")

        a_rows = (await world.api.chat_history(match_id=a.match.id))["data"]["messages"]
        b_rows = (await world.api.chat_history(match_id=b.match.id))["data"]["messages"]
        assert _texts(a_rows) == ["A 局还能继续吗？", "A 的回复"]
        assert _texts(b_rows) == ["B 的回复"]
        assert [r["role"] for r in a_rows] == ["player", "agent"]

        assert [f["text"] for f in ws_a.frames] == ["A 的回复"]
        assert [f["text"] for f in ws_b.frames] == ["B 的回复"]
    finally:
        _unsubscribe(a.match.id, ws_a)
        _unsubscribe(b.match.id, ws_b)


# ----------------------------------------------------------------------
#  2) Spectator lines never flush a reply nor enter player history
# ----------------------------------------------------------------------


async def test_spectator_line_is_buffered_without_flushing_or_recording(world):
    b = await _open_room(world)
    ws_b = _subscribe(b.match.id)
    try:
        spectator = group_chat._build_event(
            world.plugin, "agent", "nori", "旁观发言", role="spectator", match_id=b.match.id,
        )
        await group_chat.route_group_message(world.plugin, spectator)
        assert world.ctx.flushed == [], "spectator lines must never flush a reply"
        assert world.ctx.event_bus.events == [], "nothing published without a trigger"
        assert await world.store.chat_recent(20, match_id=b.match.id) == []
        assert ws_b.frames == []
    finally:
        _unsubscribe(b.match.id, ws_b)


# ----------------------------------------------------------------------
#  3) Failed delivery, unscoped and mixed batches never mirror
# ----------------------------------------------------------------------


async def test_failed_delivery_is_not_mirrored(world):
    room = await _open_room(world)
    ws = _subscribe(room.match.id)
    try:
        await _send(world, "失败投递的问题", room.match.id)
        batch = _last_batch(world)
        await _deliver_reply(world, batch, "没送达", ok=False)
        rows = await world.store.chat_recent(20, match_id=room.match.id)
        assert [r["role"] for r in rows] == ["player"], "only the player line is recorded"
        assert [f["type"] for f in ws.frames] == ["chat_message"], "no agent frame broadcast"
        assert all(f.get("role") != "agent" for f in ws.frames)
    finally:
        _unsubscribe(room.match.id, ws)


async def test_unscoped_notice_reply_is_not_mirrored_to_the_only_room(world):
    room = await _open_room(world)
    ws = _subscribe(room.match.id)
    try:
        # An unscoped notice (no match id) does not reach the bus via
        # send_notice (it takes the stock host path), so route an unscoped
        # notice event directly: the only way an unscoped trigger batch exists.
        notice = await group_chat.build_notice_event(world.plugin, "通知投递测试")
        await group_chat.route_group_message(world.plugin, notice)
        batch = _last_batch(world)
        assert batch.sid == GAME_SID
        await _deliver_reply(world, batch, "测试回复")
        assert await world.store.chat_recent(20, match_id=room.match.id) == []
        assert ws.frames == []
    finally:
        _unsubscribe(room.match.id, ws)


async def test_mixed_match_batch_is_not_mirrored(world):
    a = await _open_room(world)
    await world.rooms.resign_for_actor(PAGE_ACTOR)
    b = await _open_room(world)
    ws_a = _subscribe(a.match.id)
    ws_b = _subscribe(b.match.id)
    try:
        # Two rooms' triggers form one batch (no single owner): the reply must
        # not be attributed to either room. Build the batch from real events.
        ev_a = group_chat._build_event(
            world.plugin, "human", "Human", "A 的问题", role="player", match_id=a.match.id,
        )
        ev_b = group_chat._build_event(
            world.plugin, "human", "Human", "B 的问题", role="player", match_id=b.match.id,
        )
        mixed = SimpleNamespace(
            sid=GAME_SID, messages=[ev_a.message, ev_b.message],
        )
        await _deliver_reply(world, mixed, "混合回复")
        for room in (a, b):
            rows = await world.store.chat_recent(20, match_id=room.match.id)
            assert all(r["role"] != "agent" for r in rows)
        assert all(f.get("role") != "agent" for f in ws_a.frames + ws_b.frames)
    finally:
        _unsubscribe(a.match.id, ws_a)
        _unsubscribe(b.match.id, ws_b)
