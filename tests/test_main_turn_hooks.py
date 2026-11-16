"""Contract tests for ``main.py``'s AFTER_XML_PARSE turn hook.

Key contract: the hook must reach ``submit_play_from_actions`` for ANY
parsed action list shape — including ``actions == []``. A reply whose only
play tag was nested inside ``<msg>`` parses to no MessageChain and no
RootTagAction; the previous ``not actions`` guard swallowed that case, so
the parse-level misplaced-tag feedback (``_consume_misplaced``) never ran
and the turn idled silently to the watchdog fill.

The parse-level cases run against the REAL host ``parse_xml``: the function
node is AST-extracted from the actual ``message_delivery.py`` source and
compiled (no logic copied or reimplemented; the compile filename stays the
host source). Importing the full module would drag in the adapter/agent/
chat stack, so the exec namespace provides only stdlib ``ET``, the genuine
``core.tag`` types (loaded directly, with a stdlib-only marker module for
the one heavy import edge when the chat stack is absent) and a minimal
``MessageChain`` stand-in (``parse_xml`` merely constructs/returns it).
These cases skip ONLY when the host source file itself is missing.

The plugin folder name contains hyphens, so ``main`` is loaded under the
same synthetic package alias as ``test_runtime`` (``gamehub_under_test``);
the hook's deferred ``from .tools_turn import …`` therefore resolves to the
aliased module, and the monkeypatch targets that module.

Covered:

- ``actions=[]`` still calls the submission path (pure nested-tag reply);
- the nested tag attempt recorded by the real parse is converted into a
  bounded format rejection by the submission path;
- a nested tag with an EMPTY body is also an attempt → format rejection,
  not a generic text-only nudge;
- a proper root-level tag still submits, stops the event, and clears the
  rejection trail;
- non-list actions / missing grant: the hook must not call anything.
"""

from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

PACKAGE = "gamehub_under_test"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _find_repo_root() -> Path:
    """The checkout that contains the host ``core/`` package.

    Walking up beats a hard-coded ``parents[N]`` (T-8): the plugin may sit at
    any install depth, and a wrong guess made the parse-contract tests skip
    silently instead of running.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "core").is_dir():
            return parent
    return Path(__file__).resolve().parents[4]


REPO_ROOT = _find_repo_root()
PARSE_SOURCE = REPO_ROOT / "core" / "workflow" / "src" / "im" / "message_delivery.py"


def _identity_decorator(*args: Any, **kwargs: Any):
    def wrap(fn):
        return fn

    return wrap


def _ensure_core_plugin_importable() -> Any:
    """Return the ``core.plugin`` module ``main`` needs at import time.

    The real host module is preferred. When the host stack is unavailable
    (standalone run) a minimal stub is installed; an existing bare stub
    (installed by another test module) is augmented in place, never
    replaced. ``REPO_ROOT`` is appended (never prepended): the repo root
    ships its own ``main.py`` entry point that must not shadow the plugin's.
    """
    if (REPO_ROOT / "core").is_dir() and str(REPO_ROOT) not in sys.path:
        sys.path.append(str(REPO_ROOT))
    import logging

    try:
        core_plugin = importlib.import_module("core.plugin")
    except Exception:
        core_plugin = sys.modules.setdefault(
            "core.plugin", types.ModuleType("core.plugin")
        )
    fallback = {
        "logger": logging.getLogger("gamehub-test"),
        "Priority": types.SimpleNamespace(LOW=10, MEDIUM=50, HIGH=90),
        "on": types.SimpleNamespace(
            im_message=lambda **kw: _identity_decorator(),
            im_batch_message=lambda **kw: _identity_decorator(),
            llm_request=lambda **kw: _identity_decorator(),
            llm_response=lambda **kw: _identity_decorator(),
            after_xml_parse=lambda **kw: _identity_decorator(),
            step_result=lambda **kw: _identity_decorator(),
            tool_result=lambda **kw: _identity_decorator(),
        ),
        "register": types.SimpleNamespace(
            tool=lambda **kw: _identity_decorator(),
            page=lambda *a, **kw: _identity_decorator(),
            api=lambda **kw: _identity_decorator(),
            ws=lambda **kw: _identity_decorator(),
        ),
        "PageMenu": lambda **kw: None,
    }
    for name, value in fallback.items():
        if not hasattr(core_plugin, name):
            setattr(core_plugin, name, value)
    if not hasattr(core_plugin, "BasePlugin"):

        class BasePlugin:  # minimal stand-in (hook tests bypass __init__)
            def __init__(self, ctx, cfg):
                self.ctx = ctx
                self.plugin_cfg = cfg

        core_plugin.BasePlugin = BasePlugin
    if not hasattr(core_plugin, "PluginPage"):

        class PluginPage:
            @classmethod
            def from_folder(cls, path):
                return None

        core_plugin.PluginPage = PluginPage
    return core_plugin


def _load_main() -> Any:
    """Import ``main`` as a package submodule of the synthetic plugin alias."""
    loaded = sys.modules.get(f"{PACKAGE}.main")
    if loaded is not None:
        return loaded
    _ensure_core_plugin_importable()
    package = sys.modules.get(PACKAGE)
    if package is None or not getattr(package, "__path__", None):
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.main")


def _aliased_tools_turn() -> Any:
    """The ``tools_turn`` copy the hook's deferred import resolves to."""
    return importlib.import_module(f"{PACKAGE}.tools_turn")


MAIN = _load_main()


class _Grant:
    def __init__(self, match_id: str = "abcd1234", seat: int = 1) -> None:
        self.match_id = match_id
        self.key = "grant-key"
        self.seat = seat
        self.kind = "nori_turn"
        self.move_count = 0
        self.state = "active"
        self.last_rejection = None
        self.tag_misplaced = ""
        self.tag_misplaced_at = 0.0


class _Result:
    def __init__(self, move: str) -> None:
        self.ok = True
        self.status = "played"
        self.error = ""
        self.move = move
        self.text = "已落子。"
        self.terminal = True
        self.say = ""


class _HookDirector:
    """Duck-typed director for the hook + real submission path."""

    def __init__(self, grant: _Grant) -> None:
        self.grant = grant
        self._stopping = False
        self.submits: list[str] = []
        self.notes: list[str] = []
        self.rejected: list[tuple] = []

    def active_grant_for(self, sid: str):
        # The real director filters by session; mirror that so the
        # missing-grant case is actually exercised.
        return self.grant if sid == "s1" else None

    def note_tool_progress(self, sid) -> None:
        self.notes.append(sid)

    async def note_tag_rejected(
        self, sid, grant, move, error, *, reason: str = "invalid"
    ) -> None:
        self.rejected.append((sid, move, error, reason))

    async def submit_play(self, match_id, key, move, say=None, *, persist_say=False):
        self.submits.append(move)
        return _Result(move)


class _Event:
    def __init__(self, sid: str = "s1") -> None:
        self.sid = sid
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _plugin(director: _HookDirector) -> Any:
    """A ``GamehubPlugin`` instance without running ``__init__``."""
    plugin = MAIN.GamehubPlugin.__new__(MAIN.GamehubPlugin)
    plugin._director = director
    return plugin


# ----------------------------------------------------------------------
#  Hook contract (no real parse needed)
# ----------------------------------------------------------------------
async def test_hook_reaches_submission_for_empty_actions(monkeypatch) -> None:
    """``actions=[]`` must still reach the submission path: a pure
    nested-tag reply parses to no actions, and only the submission path can
    consume the misplaced-tag marker."""
    calls: list[tuple] = []

    async def recorder(plugin, director, grant, actions, sid, event=None):
        calls.append((grant, actions, sid))

    monkeypatch.setattr(
        _aliased_tools_turn(), "submit_play_from_actions", recorder
    )
    director = _HookDirector(_Grant())
    event = _Event()
    await MAIN.GamehubPlugin.submit_turn_play_tag(_plugin(director), event, actions=[])
    assert len(calls) == 1
    assert calls[0][0] is director.grant and calls[0][1] == []
    assert calls[0][2] == "s1"


async def test_hook_skips_non_list_actions_and_missing_grant(monkeypatch) -> None:
    """Non-list actions (parse failure) and grant-less sessions never reach
    the submission path."""
    calls: list[tuple] = []

    async def recorder(plugin, director, grant, actions, sid, event=None):
        calls.append((grant, actions, sid))

    monkeypatch.setattr(
        _aliased_tools_turn(), "submit_play_from_actions", recorder
    )
    director = _HookDirector(_Grant())
    await MAIN.GamehubPlugin.submit_turn_play_tag(
        _plugin(director), _Event(), actions=None
    )
    await MAIN.GamehubPlugin.submit_turn_play_tag(
        _plugin(director), _Event(sid="other"), actions=[]
    )
    assert calls == []


async def test_notice_model_hook_is_a_thin_delegate(monkeypatch) -> None:
    """``route_notice_model`` only forwards to ``host.notice_model.apply``."""
    calls: list[tuple] = []
    module = importlib.import_module(f"{PACKAGE}.host.notice_model")
    monkeypatch.setattr(module, "apply", lambda plugin, event: calls.append((plugin, event)))
    plugin = _plugin(_HookDirector(_Grant()))
    event = _Event()

    await MAIN.GamehubPlugin.route_notice_model(plugin, event)

    assert calls == [(plugin, event)]


# ----------------------------------------------------------------------
#  Real parse → hook → submission path
# ----------------------------------------------------------------------
class _StubMessageElement:
    """Marker ``BaseMessageElement`` stand-in: ``parse_xml`` only
    isinstance-checks produced elements, and nothing produces one here."""


class _StubMessageChain:
    """Minimal ``MessageChain`` stand-in: ``parse_xml`` only constructs and
    returns it; nothing is delivered in these tests."""

    def __init__(self, message_elements):
        self.message_list = list(message_elements or [])


def _real_tag_types() -> tuple[Any, Any, Any]:
    """``(RootTagAction, TagSet, BaseMessageElement)`` from genuine sources.

    ``core.tag.base`` imports ``core.chat.message_elements``; importing it
    through the real ``core.chat`` package would pull the whole chat stack,
    which the focused environment may not carry. When the direct import
    fails, stdlib-only marker modules are planted for exactly ``core``,
    ``core.chat`` and ``core.chat.message_elements`` (then removed again)
    so the genuine ``core.tag`` code still loads; the element type falls
    back to the shared marker (``parse_xml`` only isinstance-checks it).
    No production code is touched.
    """
    try:
        from core.chat.message_elements import BaseMessageElement
        from core.tag import RootTagAction, TagSet

        return RootTagAction, TagSet, BaseMessageElement
    except Exception:
        pass

    created: list[str] = []

    def _plant(name: str, *, path: Any = None) -> None:
        if name in sys.modules:
            return
        marker = types.ModuleType(name)
        if path is not None:
            marker.__path__ = [str(path)]
        sys.modules[name] = marker
        created.append(name)

    try:
        _plant("core", path=REPO_ROOT / "core")
        _plant("core.chat", path=REPO_ROOT / "core" / "chat")
        _plant("core.chat.message_elements")
        sys.modules["core.chat.message_elements"].BaseMessageElement = (
            _StubMessageElement
        )
        from core.tag import RootTagAction, TagSet
    finally:
        for name in created:
            sys.modules.pop(name, None)
    return RootTagAction, TagSet, _StubMessageElement


def _real_parse_xml():
    """Compile ONLY ``MessageDeliveryService.parse_xml`` from the actual
    host source and return it as a plain async function.

    AST extraction of the function node — no logic copied or reimplemented,
    the compile filename stays the host source so tracebacks point at the
    real code. The exec namespace needs stdlib ``ET`` plus the three types
    from :func:`_real_tag_types` and the ``MessageChain`` stand-in. Skips
    ONLY when the host source file itself is missing.
    """
    if not PARSE_SOURCE.is_file():
        pytest.skip("host message_delivery.py source not available")
    import ast
    import xml.etree.ElementTree as ET

    root_tag_action, _tag_set, base_element = _real_tag_types()
    parsed = ast.parse(PARSE_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(parsed):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "parse_xml"
        ):
            node.decorator_list = []  # drop @staticmethod
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {
                "ET": ET,
                "RootTagAction": root_tag_action,
                "BaseMessageElement": base_element,
                "MessageChain": _StubMessageChain,
            }
            exec(compile(module, str(PARSE_SOURCE), "exec"), namespace)
            return namespace["parse_xml"]
    raise AssertionError(
        "parse_xml disappeared from host message_delivery.py; update the "
        "AST extraction in this test"
    )


async def _run_hook_over_reply(plugin, reply_text: str, grant: _Grant, event: _Event):
    """The real core sequence: parse with the session tag_set, then the hook."""
    _root_tag_action, TagSet, _base_element = _real_tag_types()
    aliased = _aliased_tools_turn()
    tag = aliased.PlayTurnTag(plugin, grant, match_kind="doudizhu")
    tag_set = TagSet()
    tag_set.register(tag)
    # note_response runs BEFORE parse in the core and clears stale markers.
    grant.tag_misplaced = ""
    grant.tag_misplaced_at = 0.0
    parse = _real_parse_xml()
    actions = await parse(reply_text, tag_set)
    # The hook resolves the grant from the director, exactly like production.
    await MAIN.GamehubPlugin.submit_turn_play_tag(plugin, event, actions)
    return actions, tag


async def test_pure_nested_tag_reply_empty_actions_still_gets_format_feedback():
    """``<msg><play…>v</play…></msg>`` parses to actions == [] — the hook
    must still run and turn the parse-level marker into a bounded format
    rejection (this exact case was silently dropped by the old guard)."""
    grant = _Grant()
    director = _HookDirector(grant)
    plugin = _plugin(director)
    event = _Event()
    actions, _tag = await _run_hook_over_reply(
        plugin,
        f"<msg>叫三分 <play_{grant.match_id}>bid 3</play_{grant.match_id}></msg>",
        grant, event,
    )
    assert actions == []  # swallowed: no MessageChain, no RootTagAction
    assert director.submits == []  # nothing was submitted
    assert director.notes == ["s1"]  # the attempt stands the text nudge down
    assert director.rejected == [
        ("s1", "格式错误",
         "落子标签必须放在回复末尾、与<msg>同级（不要放进<msg>内）", "format")
    ]
    # (The rejection trail on the grant — ``last_rejection`` — is asserted
    # against the REAL director in test_turn_tag; this fake only records.)
    assert event.stopped is False  # the reply keeps delivering


async def test_nested_empty_body_tag_is_rejected_not_misread_as_text_only():
    """``<msg><play…></play…></msg>``: the marker is keyed on the timestamp,
    so an empty body is still an attempt → explicit format rejection."""
    grant = _Grant()
    director = _HookDirector(grant)
    plugin = _plugin(director)
    event = _Event()
    actions, _tag = await _run_hook_over_reply(
        plugin, f"<msg><play_{grant.match_id}></play_{grant.match_id}></msg>",
        grant, event,
    )
    assert actions == []
    assert director.submits == []
    assert director.rejected and director.rejected[0][3] == "format"
    assert director.notes == ["s1"]


async def test_proper_root_tag_still_submits_and_stops():
    """The happy path is untouched: a root-level tag submits, stops the
    event, and clears the rejection trail."""
    grant = _Grant()
    grant.last_rejection = ("ZZ99", "这里已经有子")
    director = _HookDirector(grant)
    plugin = _plugin(director)
    event = _Event()
    actions, _tag = await _run_hook_over_reply(
        plugin,
        f"<msg>叫三分！</msg>\n<play_{grant.match_id}>bid 3</play_{grant.match_id}>",
        grant, event,
    )
    assert director.submits == ["bid 3"]
    assert event.stopped is True
    assert grant.last_rejection is None
    assert director.rejected == []


async def test_marker_freshness_window_still_applies():
    """A stale marker (older than the freshness window) never misfires on a
    later tag-less reply — the note_response clearing plus the window keep
    the feedback precise."""
    grant = _Grant()
    grant.tag_misplaced = "bid 3"
    grant.tag_misplaced_at = time.monotonic() - 3600.0
    director = _HookDirector(grant)
    plugin = _plugin(director)
    event = _Event()
    await MAIN.GamehubPlugin.submit_turn_play_tag(plugin, event, actions=[])
    assert director.rejected == []
    assert director.notes == []
