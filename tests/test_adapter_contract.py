"""Internal adapter contract: capability metadata and outbound SID routing.

The gamehub adapter is not a chat platform. It exposes the session-backed IM
surface so notices can drive host agent turns and replies can be rendered by
the match page. Two contracts matter here:

- **Capability metadata**: the host enables expensive media output tags from
  ``supported_elements``. The runtime only renders ``Text``
  (``host.runtime._chain_text``), so the adapter must advertise ``["text"]``
  and nothing else.
- **SID routing**: group replies go to ``gamehub:gm:<id>`` and direct replies
  to ``gamehub:dm:<id>``. ``host.bridge.emit_outbound`` stays the single sink;
  the chain is passed through unchanged, and an unwired sink drops silently.

The plugin suite runs with a standalone interpreter that does not load the
host service graph (adapter registry/config/logging). The real
``core.adapter.*`` and ``core.chat.message_utils`` modules are therefore loaded
straight from the checkout with only ``core.logging_manager`` stubbed, mirroring
the lightweight host stubs used elsewhere in this suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Real host modules the adapter imports, loaded without executing the package
# ``__init__`` files (which pull in the full host service graph).
_HOST_MODULES = (
    ("core.adapter.adapter_info", "core/adapter/adapter_info.py"),
    ("core.adapter.message_format_metadata", "core/adapter/message_format_metadata.py"),
    ("core.adapter.access", "core/adapter/access.py"),
    ("core.adapter.context", "core/adapter/context.py"),
    ("core.adapter.base", "core/adapter/base.py"),
    ("core.adapter.capabilities", "core/adapter/capabilities.py"),
    ("core.chat.session", "core/chat/session.py"),
    ("core.chat.message_elements", "core/chat/message_elements.py"),
    ("core.chat.message_utils", "core/chat/message_utils.py"),
)


def _find_host_root() -> Path | None:
    """Walk up to the checkout that actually contains ``core/``.

    The plugin may be installed at any depth, so a hard-coded ``parents[N]``
    would silently skip these tests on another layout.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "core" / "chat" / "message_utils.py").is_file():
            return parent
    return None


def _load_host_module(monkeypatch: pytest.MonkeyPatch, root: Path, name: str, rel: str):
    path = root / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _load_adapter(monkeypatch: pytest.MonkeyPatch):
    """Load the plugin adapter over real host modules with a logging stub."""
    root = _find_host_root()
    if root is None:
        pytest.skip("host checkout with core/ not found next to the plugin")

    # Register the package roots as namespace packages so the adapter's
    # absolute imports resolve to the real files without running the heavy
    # package initializers (adapter registry -> config -> logging).
    for name, sub in (
        ("core", "core"),
        ("core.adapter", "core/adapter"),
        ("core.chat", "core/chat"),
    ):
        package = types.ModuleType(name)
        package.__path__ = [str(root / sub)]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, package)

    logging_stub = types.ModuleType("core.logging_manager")
    logging_stub.get_logger = lambda *args, **kwargs: logging.getLogger(
        "gamehub-adapter-test"
    )
    monkeypatch.setitem(sys.modules, "core.logging_manager", logging_stub)

    for name, rel in _HOST_MODULES:
        _load_host_module(monkeypatch, root, name, rel)

    # Loaded as a top-level module: the relative ``..host.bridge`` import fails
    # and the adapter's fallback ``from host.bridge import emit_outbound`` is
    # used, which resolves through the plugin root already on sys.path.
    spec = importlib.util.spec_from_file_location(
        "gamehub_adapter_under_test", PLUGIN_ROOT / "adapter" / "adapter.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "gamehub_adapter_under_test", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def adapter_harness(monkeypatch):
    """The plugin adapter instance plus the host classes it was built from."""
    module = _load_adapter(monkeypatch)
    adapter_info_cls = sys.modules["core.adapter.adapter_info"].AdapterInfo
    context_cls = sys.modules["core.adapter.context"].AdapterContext
    im_capability_cls = sys.modules["core.adapter.capabilities"].IMCapability
    message_chain_cls = sys.modules["core.chat.message_utils"].MessageChain

    ctx = context_cls(
        info=adapter_info_cls(
            enabled=True,
            adapter_id="gamehub",
            name="gamehub",
            platform="gamehub",
            description="internal test adapter",
        ),
        event_queue=asyncio.Queue(),
    )
    adapter = module.GamehubAdapter(ctx)
    return types.SimpleNamespace(
        module=module,
        adapter=adapter,
        im=adapter.get_capability(im_capability_cls),
        MessageChain=message_chain_cls,
    )


@pytest.fixture()
def outbound(monkeypatch):
    """Capture ``emit_outbound`` calls without changing the bridge semantics."""
    from host import bridge

    seen: list[tuple[str, object]] = []

    async def sink(sid: str, chain: object) -> None:
        seen.append((sid, chain))

    bridge.set_outbound_sink(sink)
    try:
        yield seen
    finally:
        bridge.set_outbound_sink(None)


async def test_capability_metadata_is_text_only(adapter_harness):
    """The host gates media tags on this, so anything but text is a bug."""
    metadata = await adapter_harness.im.get_message_metadata()

    assert metadata.supported_elements == ["text"]
    # No emoji catalog: the adapter has no sticker/emoji rendering path.
    assert metadata.emojis is None


@pytest.mark.parametrize(
    ("entry", "method", "target", "sid"),
    [
        ("im", "send_group_message", "room", "gamehub:gm:room"),
        ("im", "send_direct_message", "admin", "gamehub:dm:admin"),
        ("adapter", "send_group_message", "room", "gamehub:gm:room"),
        ("adapter", "send_direct_message", "admin", "gamehub:dm:admin"),
    ],
)
async def test_replies_route_to_their_session_sid(
    adapter_harness, outbound, entry, method, target, sid
):
    """Capability and compatibility entry points share one routing rule."""
    chain = adapter_harness.MessageChain([])
    sender = adapter_harness.im if entry == "im" else adapter_harness.adapter

    result = await getattr(sender, method)(target, chain)

    assert outbound == [(sid, chain)]
    assert outbound[0][1] is chain  # passed through unchanged, no re-render
    assert result.ok is True
    assert result.message_id is None


async def test_unwired_sink_drops_silently(adapter_harness):
    """The bridge contract: no sink installed means no delivery and no error."""
    from host import bridge

    bridge.set_outbound_sink(None)
    result = await adapter_harness.im.send_group_message(
        "room", adapter_harness.MessageChain([])
    )
    assert result.ok is True