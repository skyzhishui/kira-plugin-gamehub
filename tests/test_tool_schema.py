"""Wire-format regressions: what the provider actually receives per request.

Two surfaces are covered:

- ``BoundStateTool`` serialises its *instance* name. The turn-scoped
  ``state_<match>`` tool differs from its ``game_state`` parent only by
  ``self.name``; a class-level override would serialise the parent name and
  silently hand the model the static tool (whose notice-turn guard answers
  "不可用"), so the payload must carry the instance name and the parent class
  attributes must stay untouched. Its description is its own contract
  (P1-14): the tag is the move surface, not ``play`` / ``game_state``.
- ``host.prompt.inject_into_request`` adds the request-scoped surface (the
  read tool and the ``play_<match>`` tag) plus the ephemeral context block.
  The host's ``core.prompt_manager`` is a thin boundary, stubbed the same way
  ``test_runtime`` stubs ``core.plugin``, so this runs in the standalone
  suite instead of always skipping.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

import tools as game_tools
from tools_turn import BoundStateTool, build_turn_tools, state_tool_name


class _HostToolSet:
    """Mirror of ``core.agent.tool.ToolSet`` for the provider wire format."""

    def __init__(self, tools=()) -> None:
        self.tools = list(tools)

    def add(self, *tools) -> None:
        self.tools.extend(tools)

    def to_list(self):
        return [
            {"type": "function", "function": tool.get_schema()}
            for tool in self.tools
        ]


def _assert_payload(payload):
    assert payload, "payload must not be empty"
    for entry in payload:
        assert set(entry) == {"type", "function"}
        assert entry["type"] == "function"
        function = entry["function"]
        assert isinstance(function, dict)
        assert function.get("name"), "tools[n] must include a function name"
        assert function.get("description")
        params = function.get("parameters")
        assert isinstance(params, dict) and params.get("type") == "object"


def _grant(match_id: str = "abc123", seat: int = 1, kind: str = "nori_turn"):
    return SimpleNamespace(match_id=match_id, key="grant-key", seat=seat, kind=kind)


def test_bound_state_tool_serialises_its_instance_name_and_own_contract():
    grant = _grant()
    tool = BoundStateTool(SimpleNamespace(), grant)
    assert tool.name == state_tool_name("abc123") == "state_abc123"
    # The parent class attribute must stay untouched: a class-level override
    # would leak into the static tool and into every later instance.
    assert BoundStateTool.name == "game_state"
    assert game_tools.GameStateTool.name == "game_state"

    # Its own description: the ``play_<match>`` tag is the move surface, and
    # the bound path renders no plan/TTL section nor the static tool hint.
    assert tool.description == game_tools.BOUND_STATE_DESCRIPTION
    assert tool.description != game_tools.GameStateTool.description
    assert "play_" in tool.description
    assert "先调用 game_state" not in tool.description
    assert "剩余 TTL" not in tool.description

    payload = _HostToolSet(build_turn_tools(SimpleNamespace(), grant)).to_list()
    _assert_payload(payload)
    assert [entry["function"]["name"] for entry in payload] == ["state_abc123"]
    assert payload[0]["function"]["description"] == game_tools.BOUND_STATE_DESCRIPTION


def test_each_turn_gets_its_own_bound_name():
    names = [
        entry["function"]["name"]
        for match_id in ("m-one", "m-two")
        for entry in _HostToolSet(
            build_turn_tools(SimpleNamespace(), _grant(match_id))
        ).to_list()
    ]
    # The sanitiser strips the separator: no two matches collide.
    assert names == ["state_mone", "state_mtwo"]


class _Prompt:
    """Minimal ``core.prompt_manager.Prompt`` container (host boundary stub)."""

    def __init__(self, content="", name="", source="user", persist=True) -> None:
        self.content = content
        self.name = name
        self.source = source
        self.persist = persist


@pytest.fixture()
def inject_prompt(monkeypatch):
    """Import ``host.prompt`` against a stubbed host prompt container."""
    core = sys.modules.get("core")
    if core is None:
        core = types.ModuleType("core")
        core.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "core", core)
    prompt_manager = types.ModuleType("core.prompt_manager")
    prompt_manager.Prompt = _Prompt
    monkeypatch.setitem(sys.modules, "core.prompt_manager", prompt_manager)
    previous = sys.modules.pop("host.prompt", None)
    import host

    previous_attr = getattr(host, "prompt", None)
    try:
        yield importlib.import_module("host.prompt").inject_into_request
    finally:
        if previous is None:
            sys.modules.pop("host.prompt", None)
        else:
            sys.modules["host.prompt"] = previous
        if previous_attr is None:
            host.__dict__.pop("prompt", None)
        else:
            host.prompt = previous_attr


def _live_match():
    from engine import NewMatchOptions, NewSeat, create_match

    return create_match(
        NewMatchOptions(
            id="abc123",
            kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t", name="Alice", joined=True),
                NewSeat(kind="ai", token="", name="nori"),
            ],
            seed=1,
            now=1000,
            mode_options={"mode": "nori", "driver": "turn"},
        )
    )


def test_inject_into_request_adds_the_bound_surface_and_ephemeral_block(
    inject_prompt,
):
    grant = _grant()
    director = SimpleNamespace(
        active_grant_for=lambda sid: grant if sid == "private-u1" else None,
        tools_for=lambda sid: (
            build_turn_tools(SimpleNamespace(), grant) if sid == "private-u1" else []
        ),
        match_kind_of=lambda match_id: "gomoku",
    )
    room = SimpleNamespace(status="active", match=_live_match())
    plugin = SimpleNamespace(
        _director=director,
        _rooms=SimpleNamespace(active_rooms=lambda: [room]),
        notice_target_session_sync=lambda _room: "private-u1",
    )

    class _TagSet:
        def __init__(self) -> None:
            self.tags: list[object] = []

        def register(self, tag) -> None:
            self.tags.append(tag)

    request = SimpleNamespace(
        tool_set=_HostToolSet(), user_prompt=[],
    )
    tag_set = _TagSet()

    inject_prompt(plugin, SimpleNamespace(sid="private-u1"), request, tag_set)

    # The read tool reached the request payload, with its instance name.
    _assert_payload(request.tool_set.to_list())
    assert [entry["function"]["name"] for entry in request.tool_set.to_list()] == [
        "state_abc123"
    ]
    # The move channel is the request-scoped tag, not a native tool.
    assert [getattr(tag, "name", "") for tag in tag_set.tags] == ["play_abc123"]
    # The context block is ephemeral: advisory, named, never persisted.
    assert request.user_prompt and request.user_prompt[-1].name == "gamehub_context"
    assert request.user_prompt[-1].persist is False
    assert "<play_abc123>" in request.user_prompt[-1].content

    # Another session gets no surface and no block.
    other = SimpleNamespace(tool_set=_HostToolSet(), user_prompt=[])
    inject_prompt(plugin, SimpleNamespace(sid="private-u2"), other, _TagSet())
    assert other.tool_set.to_list() == [] and other.user_prompt == []
