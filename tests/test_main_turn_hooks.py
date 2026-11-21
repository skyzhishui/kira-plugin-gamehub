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
            message_sent=lambda **kw: _identity_decorator(),
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

    def active_grant_for_match(self, sid: str, match_id: str):
        # Mirror the real match-scoped lookup: only this match's grant, and
        # only on its own session.
        if sid != "s1" or match_id != self.grant.match_id:
            return None
        return self.grant

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
    """A reply batch on the shared session, triggered by its grant's match.

    ``messages`` carries a notice scoped to ``match_id``: the play tag is only
    honoured for a batch that is attributable to one match.
    """

    def __init__(self, sid: str = "s1", match_id: str = "abcd1234") -> None:
        self.sid = sid
        self.stopped = False
        self.messages = [
            types.SimpleNamespace(is_notice=True, extra={"match_id": match_id}),
        ]

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


async def test_post_game_batch_of_ended_match_never_submits_live_sibling_grant(monkeypatch) -> None:
    """A's post-game reply on the shared session must not land B's move.

    B holds the live grant on session ``s1``; the batch is A's (match
    ``aaaa0000``, which has no grant). The hook must not fall back to a
    session-only lookup, so the submission path is never reached.
    """
    from host.group_chat import GAME_SID

    live_b = _Grant(match_id="bbbb1111")
    director = _ScopedDirector(live_b, session=GAME_SID)
    plugin = _plugin(director)
    event = _Event(sid=GAME_SID, match_id="aaaa0000")

    async def must_not_submit(*args, **kwargs):
        raise AssertionError("A's batch submitted B's grant")

    aliased = _aliased_tools_turn()
    monkeypatch.setattr(aliased, "submit_play_from_actions", must_not_submit)
    await MAIN.GamehubPlugin.submit_turn_play_tag(
        plugin, event, actions=[types.SimpleNamespace()],
    )
    assert director.submits == []
    assert event.stopped is False


# ----------------------------------------------------------------------
#  Contract: strict match scope only on GAME_SID; external sids keep the
#  session lookup; the batch filter never mixes owners.
# ----------------------------------------------------------------------


class _ScopedDirector(_HookDirector):
    """Director double whose session lookup is not limited to ``s1``."""

    def __init__(self, grant: _Grant, session: str) -> None:
        super().__init__(grant)
        self.session = session

    def active_grant_for(self, sid: str):
        return self.grant if sid == self.session else None

    def active_grant_for_match(self, sid: str, match_id: str):
        if sid != self.session or match_id != self.grant.match_id:
            return None
        return self.grant


async def test_external_session_play_tag_keeps_session_lookup(monkeypatch) -> None:
    """An external notice target has no match_id; its grant must still submit."""
    from host.group_chat import GAME_SID  # noqa: F401 - import check

    grant = _Grant(match_id="ext00001")
    director = _ScopedDirector(grant, session="webchat:dm:admin")
    plugin = _plugin(director)
    event = _Event(sid="webchat:dm:admin")
    event.messages = [types.SimpleNamespace(is_notice=True, extra={})]  # unscoped

    seen: list[str] = []

    async def record_submit(plugin_, director_, grant_, actions, sid, event=None):
        seen.append(grant_.match_id)
        return True

    aliased = _aliased_tools_turn()
    monkeypatch.setattr(aliased, "submit_play_from_actions", record_submit)
    await MAIN.GamehubPlugin.submit_turn_play_tag(
        plugin, event, actions=[types.SimpleNamespace()],
    )
    assert seen == ["ext00001"], "external sid must keep the session-scoped grant"


async def test_game_session_unscoped_batch_never_submits(monkeypatch) -> None:
    """On GAME_SID an unscoped batch cannot pick a grant (strict scope)."""
    from host.group_chat import GAME_SID

    grant = _Grant(match_id="bbbb1111")
    director = _ScopedDirector(grant, session=GAME_SID)
    plugin = _plugin(director)
    event = _Event(sid=GAME_SID)
    event.messages = [types.SimpleNamespace(is_notice=True, extra={})]

    async def must_not_submit(*args, **kwargs):
        raise AssertionError("unscoped GAME_SID batch submitted a grant")

    aliased = _aliased_tools_turn()
    monkeypatch.setattr(aliased, "submit_play_from_actions", must_not_submit)
    await MAIN.GamehubPlugin.submit_turn_play_tag(
        plugin, event, actions=[types.SimpleNamespace()],
    )
    assert director.submits == []


def _load_prompt_module() -> Any:
    """``host.prompt`` with a minimal ``core.prompt_manager`` fallback.

    The real host module pulls in Pillow via the persona stack; when that is
    unavailable (standalone venv) only the ``Prompt`` symbol is stubbed, and
    the real module is used whenever it imports.
    """
    try:
        import core.prompt_manager  # noqa: F401 - real host module when present
    except Exception:
        stub = types.ModuleType("core.prompt_manager")

        class Prompt:  # noqa: D401 - structural stand-in for the host dataclass
            def __init__(self, content: str = "", name: str = "", source: str = "", persist: bool = True) -> None:
                self.content, self.name, self.source, self.persist = content, name, source, persist

        stub.Prompt = Prompt  # type: ignore[attr-defined]
        sys.modules["core.prompt_manager"] = stub
    return importlib.import_module(f"{PACKAGE}.host.prompt")


def test_prompt_grant_scope_is_strict_only_on_game_session() -> None:
    """host/prompt uses the same contract as the hook (one source of truth)."""
    from host.group_chat import GAME_SID  # noqa: F401 - the shared session id
    prompt_module = _load_prompt_module()

    grant = _Grant(match_id="ext00002")
    external = _ScopedDirector(grant, session="webchat:dm:admin")
    unscoped = _Event(sid="webchat:dm:admin")
    unscoped.messages = [types.SimpleNamespace(is_notice=True, extra={})]
    assert prompt_module._batch_grant(external, unscoped, "webchat:dm:admin") is grant

    game = _ScopedDirector(grant, session=GAME_SID)
    unscoped_game = _Event(sid=GAME_SID)
    unscoped_game.messages = [types.SimpleNamespace(is_notice=True, extra={})]
    assert prompt_module._batch_grant(game, unscoped_game, GAME_SID) is None


def test_same_room_filter_never_mixes_owners() -> None:
    """Scoped batches drain only their room; unscoped batches only unscoped."""
    from host.group_chat import _same_room_filter

    def item(match: str, role: str = "player"):
        return types.SimpleNamespace(extra={"match_id": match, "gamehub_role": role})

    a_player = item("aaaa")
    a_spectator = item("aaaa", "spectator")
    b_player = item("bbbb")
    unscoped_notice = item("", "notice")
    unscoped_spectator = item("", "spectator")

    scoped_a = _same_room_filter("aaaa")
    assert scoped_a(a_player) and scoped_a(a_spectator)
    assert not scoped_a(b_player)
    assert not scoped_a(unscoped_notice), "unscoped trigger must not ride into A"
    assert not scoped_a(unscoped_spectator)

    unscoped = _same_room_filter("")
    assert unscoped(unscoped_notice) and unscoped(unscoped_spectator)
    assert not unscoped(a_player), "unscoped trigger must not drain A's player line"
    assert not unscoped(b_player)


# ----------------------------------------------------------------------
#  Shared GAME_SID: an ended match A's post-game batch must not touch the
#  live sibling B (prompt, tools, lifecycle clock).
# ----------------------------------------------------------------------


class _SharedRequest:
    """Minimal request surface: a tool set and the ephemeral prompt list."""

    def __init__(self) -> None:
        self.tool_set = _SharedToolSet()
        self.user_prompt: list[Any] = []


class _SharedToolSet:
    def __init__(self) -> None:
        self.names: list[str] = []

    def add(self, *tools: Any) -> None:
        self.names.extend(str(getattr(tool, "name", "")) for tool in tools)


class _SharedTagSet:
    def __init__(self) -> None:
        self.tags: list[Any] = []

    def register(self, tag: Any) -> None:
        self.tags.append(tag)


async def test_ended_match_post_game_batch_leaves_live_sibling_untouched(monkeypatch) -> None:
    """A ended (post-game batch on GAME_SID) while B is live on the same session.

    Asserts the regression surface end-to-end through the real director
    lifecycle and the real prompt injection: A's request must carry no B
    state tool, no B play tag, no B board, and A's hook signals must not
    refresh B's silence clock, mark B's run in flight, or arm B's nudge.
    """
    from host.group_chat import GAME_SID
    from director import TurnGrant, TurnDirector, ST_ACTIVE
    from tools_turn import state_tool_name

    prompt_module = _load_prompt_module()

    class _Room:
        def __init__(self, match_id: str) -> None:
            self.status = "active"
            self.match = types.SimpleNamespace(id=match_id, kind="gomoku", seats=[])

    from engine import NewMatchOptions, NewSeat, create_match
    import context as game_context

    live_b = create_match(
        NewMatchOptions(
            id="bbbb1111", kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t0", name="Alice", joined=True),
                NewSeat(kind="ai", token="", name="nori"),
            ],
            seed=1, now=1000, mode_options={"mode": "nori", "driver": "turn"},
        )
    )
    plugin = _plugin(_HookDirector(_Grant()))
    plugin._rooms = types.SimpleNamespace(
        active_rooms=lambda: [types.SimpleNamespace(status="active", match=live_b)]
    )
    plugin.notice_target_session_sync = lambda _room: GAME_SID
    plugin._director = TurnDirector(plugin)
    director = plugin._director
    # Ended A left a start/over stage event on the shared session.
    game_context.note_stage_event(
        plugin, GAME_SID, "aaaa0000", 3, "对局结束 · 五子棋：黑方胜；A 的对局。",
    )
    now = time.monotonic()
    grant_b = TurnGrant(
        match_id="bbbb1111", seat=1, key="kb", session_id=GAME_SID,
        shared_session=True, deadline=now + 90.0, notice_sent=True,
        notice_sent_at=now - 5.0, state=ST_ACTIVE,
    )
    director._grants["bbbb1111"] = grant_b
    director._llm_req[GAME_SID] = now - 1.0  # not used for B's own scope

    # The prompt must expose B's surface only to B's own batch.
    b_batch = _Event(sid=GAME_SID, match_id="bbbb1111")
    assert director.tools_for(GAME_SID, match_id="bbbb1111") != []
    a_batch = _Event(sid=GAME_SID, match_id="aaaa0000")
    request_a = _SharedRequest()
    tags_a = _SharedTagSet()
    prompt_module.inject_into_request(plugin, a_batch, request_a, tags_a)
    assert state_tool_name("bbbb1111") not in request_a.tool_set.names
    assert tags_a.tags == [], "A's batch must not register B's play tag"
    block_a = next(
        (p.content for p in request_a.user_prompt if p.name == "gamehub_context"),
        "",
    )
    assert "bbbb1111" not in block_a, "A's batch must not show B's board"
    assert "对局结束 · 五子棋" in block_a, "A keeps its own ended-match event"
    assert "A 的对局" in block_a

    # A's lifecycle signals (run start, response, progress) are scoped to A
    # and must leave B's clock and run marks untouched.
    before_deadline = grant_b.deadline
    before_progress = grant_b.last_progress
    director.note_llm_request(GAME_SID, match_id="aaaa0000")
    director.note_progress(GAME_SID, match_id="aaaa0000")
    director.note_tool_progress(GAME_SID, match_id="aaaa0000")
    director.note_response(GAME_SID, match_id="aaaa0000")
    assert grant_b.deadline == before_deadline
    assert grant_b.last_progress == before_progress
    assert grant_b.last_tool_progress == 0.0
    assert grant_b.text_task is None and grant_b.text_nudges == 0
    assert director._llm_req.get(GAME_SID) == now - 1.0, "B's session mark untouched"
    assert f"{GAME_SID}\x00aaaa0000" in director._llm_req

    # Unattributable A batch on the shared session: touches nothing at all.
    director.note_progress(GAME_SID, match_id="")
    director.note_response(GAME_SID, match_id="")
    assert grant_b.deadline == before_deadline
    assert grant_b.last_progress == before_progress

    # B's own batch still works (no regression for the live match).
    request_b = _SharedRequest()
    tags_b = _SharedTagSet()
    prompt_module.inject_into_request(plugin, b_batch, request_b, tags_b)
    assert state_tool_name("bbbb1111") in request_b.tool_set.names
    assert [getattr(t, "name", "") for t in tags_b.tags] == ["play_bbbb1111"]
    block_b = next(
        (p.content for p in request_b.user_prompt if p.name == "gamehub_context"),
        "",
    )
    assert "bbbb1111" in block_b, "B's own batch shows B's board"
    await director.stop()
