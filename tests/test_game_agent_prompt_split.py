"""Focus tests for the stable/dynamic prompt split in ``game_agent``.

Covered contracts:

- static rules / move syntax / seat identity live in the stable prefix
  (system + init user + ``play`` tool definition) exactly once per request;
- every dynamic snapshot carries exactly one current-position block and the
  stable blocks are never repeated inside it;
- multi-round turns inside one hand do not accumulate snapshots, and old
  ``user`` snapshots are never replayed from cross-hand history;
- doudizhu: the authorised seat sees its own private hand, other seats'
  private hands never leak, and no dynamic landlord/farmer side leaks into
  the stable prefix.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine import NewMatchOptions, NewSeat, agent_view_text, apply_match_action, create_match, games
from engine.match import Match
from game_agent import GameAgent, _stable_rules_block, build_play_tool_def
from host import llm as host_llm
from host.settings import Settings
from room import GameRoom, RoomManager

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="session", autouse=True)
def _host_core_importable() -> None:
    """Same lightweight ``core.*`` stubs as ``test_game_agent`` (host adapter)."""
    if (REPO_ROOT / "core").is_dir() and str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    try:
        import core.agent.tool  # noqa: F401
    except ImportError:
        from core.agent.message import OpenAIMessage  # noqa: F401  (pydantic only)

        stub = types.ModuleType("core.agent.tool")

        class ToolSet:
            def __init__(self, tools=None):
                self.tools = list(tools or [])

            def add(self, *tools):
                self.tools.extend(tools)

            def to_list(self):
                return [
                    {"type": "function", "function": tool.get_schema()}
                    for tool in self.tools
                ]

        stub.ToolSet = ToolSet  # type: ignore[attr-defined]
        sys.modules["core.agent.tool"] = stub

    try:
        import core.provider  # noqa: F401
    except ImportError:
        sys.modules.pop("core.provider", None)
        from core.agent.message import OpenAIMessage
        from core.agent.tool import ToolSet

        stub = types.ModuleType("core.provider")

        class LLMRequest:
            def __init__(self, messages=None, tool_set=None, tool_choice=None):
                self.messages = [OpenAIMessage(**m) for m in (messages or [])]
                self.tool_set = tool_set
                self.tools = tool_set.to_list() if tool_set else None
                self.tool_choice = tool_choice or ("auto" if self.tools else "none")

        stub.LLMRequest = LLMRequest  # type: ignore[attr-defined]
        sys.modules["core.provider"] = stub


async def _dispatch_stream(plugin: Any, **kwargs: Any):
    router = getattr(plugin, "_test_router", None)
    if router is None:
        raise AssertionError("test router is not attached to the plugin")
    async for event in router.run_replyer_stream(plugin, **kwargs):
        yield event


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch) -> None:
    monkeypatch.setattr(host_llm, "run_replyer_stream", _dispatch_stream)


class RecordingRouter:
    """Records every request; replays a scripted list of steps."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def run_replyer_stream(
        self,
        plugin: Any,
        *,
        system_prompt: str,
        user_prompt: str,
        media: Any = None,
        tools: Any = None,
        extra_messages: Any = None,
        stream: bool = True,
        model_list: Any = None,
        reasoning_effort: str = "none",
    ):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": tools,
                "extra_messages": list(extra_messages or []),
                "model_list": list(model_list or []),
                "reasoning_effort": reasoning_effort,
            }
        )
        step = self.script.pop(0) if self.script else {"type": "text", "text": ""}
        text = str(step.get("text") or "")
        calls = None
        if step.get("move") is not None:
            calls = [
                {
                    "id": f"call-{len(self.calls)}",
                    "type": "function",
                    "function": {
                        "name": "play",
                        "arguments": json.dumps({"move": step["move"]}, ensure_ascii=False),
                    },
                }
            ]
        if text:
            yield {"type": "text", "delta": text}
        yield {"type": "tool_calls", "tool_calls": calls, "finish_reason": "stop"}


class FakeRepo:
    def __init__(self) -> None:
        self.snapshots: dict[str, Match] = {}
        self.statuses: dict[str, str] = {}

    async def save(self, match: Match, status: str) -> None:
        self.snapshots[match.id] = Match.from_dict(match.to_dict())
        self.statuses[match.id] = status

    async def load(self, match_id: str) -> Match | None:
        snapshot = self.snapshots.get(match_id)
        return Match.from_dict(snapshot.to_dict()) if snapshot is not None else None

    async def load_active(self) -> list[Match]:
        return [
            Match.from_dict(snapshot.to_dict())
            for match_id, snapshot in self.snapshots.items()
            if self.statuses.get(match_id) == "active"
        ]


class FakePlugin:
    def __init__(self, router: Any, cfg: Any, data_dir: Any) -> None:
        self.ctx = SimpleNamespace(task_router=router)
        self.settings = cfg
        self.data_dir = str(data_dir)
        self.stopping = False
        self._test_router = router


def _cfg(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "agent_name": "nori",
        "agent_persona": "调皮爱吐槽的猫娘，玩游戏时会认真+毒舌",
        "ai_mode": "agent",
        "ai_models": ["m1"],
        "ai_timeout": 2.0,
        "ai_max_concurrency": 1,
        "ai_agent_rounds": 5,
        "ai_agent_calls_per_round": 1,
        "ai_history_rounds": 10,
        "ai_min_think_ms": 0,
        "ai_think_jitter_ms": 0,
        "ai_agent_prompt": "",
        "ai_decision_log": False,
        "max_say_len": 280,
    }
    base.update(overrides)
    return Settings(base)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def _snapshot_messages(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        m
        for m in call["extra_messages"]
        if m.get("role") == "user" and "Current position:" in _message_text(m)
    ]


def _request_text(call: dict[str, Any]) -> str:
    parts = [call["system_prompt"], call["user_prompt"]]
    parts.extend(_message_text(m) for m in call["extra_messages"])
    return "\n".join(parts)


def _tool_def_text(tools: Any) -> str:
    parts: list[str] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        parts.append(str(function.get("description") or ""))
        params = function.get("parameters") or {}
        properties = params.get("properties") if isinstance(params, dict) else None
        if isinstance(properties, dict):
            for prop in properties.values():
                if isinstance(prop, dict):
                    parts.append(str(prop.get("description") or ""))
    return "\n".join(parts)


async def _gomoku_room() -> GameRoom:
    manager = RoomManager(repository=FakeRepo(), config=_cfg(), now_fn=lambda: 1000)
    room, _seat, _token = await manager.create(
        kind="gomoku",
        actor="alice",
        session_id="private-alice",
        name="玩家",
        now=1000,
        mode_options={"mode": "nori", "driver": "agent"},
    )
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    return room


async def _doudizhu_room() -> GameRoom:
    match = create_match(
        NewMatchOptions(
            id="dz-prompt-split",
            kind="doudizhu",
            seed=7,
            now=1,
            seats=[
                NewSeat("human", "yoru"),
                NewSeat("ai", "蕾依"),
                NewSeat("bot", "小白"),
            ],
            mode_options={"lineup": "agent_bot"},
        )
    )
    # Seat 0 bids 3 → seat 0 becomes landlord and leads; after its lead the
    # agent seat (1) is on turn.
    bid = apply_match_action(match, 0, {"type": "move", "move": "bid 3"}, 2)
    played = apply_match_action(
        bid.match,
        0,
        {"type": "move", "move": bid.match.state["cards"][0][0]},
        3,
    )
    assert played.ok
    return GameRoom(played.match, repository=FakeRepo(), now_fn=lambda: 1000)


# ----------------------------------------------------------------------
#  Stable prefix: rules / move syntax / identity exactly once
# ----------------------------------------------------------------------
async def test_rules_appear_once_in_stable_prefix_and_never_in_snapshot(
    tmp_path,
) -> None:
    router = RecordingRouter([{"type": "tool", "move": "J9", "text": "先占中路"}])
    room = await _gomoku_room()
    agent = GameAgent(FakePlugin(router, _cfg(), tmp_path))

    await agent.run(room, 1)

    call = router.calls[0]
    module = games.get("gomoku")
    stable_rules = _stable_rules_block(module)
    request = _request_text(call)
    # Raw rules embed their own "Moves:" clause; the stable block strips it so
    # the format is not stated twice inside rule text.
    assert str(module.rules) not in request
    assert request.count(stable_rules) == 1
    assert call["user_prompt"].count(stable_rules) == 1
    assert "Moves:" not in stable_rules
    assert str(module.move_help) not in stable_rules
    snapshot = _snapshot_messages(call)
    assert len(snapshot) == 1
    snapshot_text = _message_text(snapshot[0])
    assert "Rules:" not in snapshot_text
    assert stable_rules not in snapshot_text
    assert "Move syntax:" not in snapshot_text

    # Move syntax is declared once by the play tool schema; the stable init
    # only references it, and the dynamic snapshot never repeats it.
    tool_text = _tool_def_text(call["tools"])
    assert str(module.move_help) in tool_text
    assert str(module.move_help) not in snapshot_text
    assert str(module.move_help) not in call["user_prompt"]
    assert str(module.move_help) not in call["system_prompt"]
    assert "本局规则" in call["user_prompt"]
    assert "动作格式以 play 工具定义" in call["user_prompt"]


async def test_stable_prefix_has_single_identity_statement(tmp_path) -> None:
    router = RecordingRouter([{"type": "tool", "move": "J9"}])
    room = await _gomoku_room()
    agent = GameAgent(FakePlugin(router, _cfg(), tmp_path))

    await agent.run(room, 1)

    call = router.calls[0]
    system = call["system_prompt"]
    init = call["user_prompt"]
    assert "你是nori" in system
    assert "第1席" in system
    # Default system already states identity; init does not repeat it. Rules and
    # the move-format reference are the only long stable items in init.
    assert "你是" not in init
    assert "nori" not in init
    assert "在同一次回复的普通正文里" not in init
    assert "本局规则（整局有效）：" in init
    assert "动作格式" in init
    assert "这是本局连续的决策记录" in init
    # The latest snapshot sits after cross-hand history and before this-hand
    # tool interactions; "last in the input" would be inaccurate.
    assert "位于跨手历史之后、本手工具交互之前" in init
    assert "本轮输入的最后" not in init

    # The whole request states the identity once (system only).
    assert _request_text(call).count("你是nori") == 1


async def test_stable_prefix_speech_guidance_matches_user_wording(tmp_path) -> None:
    router = RecordingRouter([{"type": "tool", "move": "J9"}])
    room = await _gomoku_room()
    agent = GameAgent(FakePlugin(router, _cfg(), tmp_path))

    await agent.run(room, 1)

    system = router.calls[0]["system_prompt"]
    assert "根据局势和群聊信息" in system
    assert "说一两句符合你人设的玩家短话" in system
    assert "不要复述你的落子计划" in system
    # Text-only agent: the boundary is brief, no media capability offered.
    assert "本局发言只支持文本" in system
    assert "表情包" not in system and "语音" not in system
    assert "绝不透露或复述私有" in system
    # The play submission instruction is stated exactly once.
    assert system.count("play 工具") == 1
    # The dynamic snapshot no longer repeats speech/play/table-talk guidance.
    snapshot_text = _message_text(_snapshot_messages(router.calls[0])[0])
    assert "Call the `play` tool" not in snapshot_text
    assert "player-style lines" not in snapshot_text
    assert "do not reply with text only" not in snapshot_text
    assert "Table talk:" not in snapshot_text
    assert "Status: YOUR TURN." in snapshot_text


def test_stable_rules_block_strips_embedded_move_format() -> None:
    """Rules that embed their move format keep the rules but not the format."""
    for kind in ("gomoku", "doudizhu", "reversi"):
        module = games.get(kind)
        stable = _stable_rules_block(module)
        assert stable
        assert "Moves:" not in stable
        assert "Move syntax:" not in stable
        assert str(module.move_help) not in stable
        # The substantive rule text survives the strip.
        assert stable.splitlines()[0] in str(module.rules).splitlines()[0]

    # Doudizhu rules keep their non-format rules but drop the trailing Moves line.
    dz_rules = str(games.get("doudizhu").rules)
    dz_stable = _stable_rules_block(games.get("doudizhu"))
    assert "Bidding: in seat order" in dz_stable
    assert "Never claim to know hidden hands." in dz_stable
    assert dz_rules.count("Moves:") == 1
    assert "Moves:" not in dz_stable

    # Module without rules -> empty stable block (no stray blank section).
    assert _stable_rules_block(SimpleNamespace(rules="")) == ""


def test_play_tool_schema_declares_move_format_once() -> None:
    module = games.get("gomoku")
    tool = build_play_tool_def(module)
    text = _tool_def_text([tool])
    assert str(module.move_help) in text
    # The schema is the single stable home of the move format; prompt rule text
    # must not re-declare it (covered above). The schema may repeat it in the
    # function and parameter descriptions by design.
    assert text.count(str(module.move_help)) <= 2


# ----------------------------------------------------------------------
#  Dynamic snapshot: one per round, never accumulates, no replay
# ----------------------------------------------------------------------
async def test_snapshot_single_per_round_across_multi_round_turn(tmp_path) -> None:
    router = RecordingRouter(
        [
            {"type": "text", "text": "我想想"},
            {"type": "tool", "move": "Z99"},
            {"type": "tool", "move": "J9", "text": "这手稳了"},
        ]
    )
    room = await _gomoku_room()
    agent = GameAgent(FakePlugin(router, _cfg(ai_agent_rounds=5), tmp_path))

    await agent.run(room, 1)

    assert len(router.calls) == 3
    for call in router.calls:
        snapshots = _snapshot_messages(call)
        assert len(snapshots) == 1
        snapshot_text = _message_text(snapshots[0])
        assert snapshot_text.count("Current position:") == 1
        # No accumulation: the reminder/old transcripts are not new snapshots.
        users = [m for m in call["extra_messages"] if m.get("role") == "user"]
        position_users = [
            m for m in users if "Current position:" in _message_text(m)
        ]
        assert len(position_users) == 1
        assert "Rules:" not in snapshot_text
    assert room.match.log[-1].move == "J9"


async def test_old_user_snapshot_not_replayed_from_history(tmp_path) -> None:
    router = RecordingRouter([{"type": "tool", "move": "J9"}, {"type": "tool", "move": "K9"}])
    room = await _gomoku_room()
    agent = GameAgent(FakePlugin(router, _cfg(), tmp_path))

    await agent.run(room, 1)
    first_snapshot = _message_text(_snapshot_messages(router.calls[0])[0])
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)

    second = router.calls[-1]
    history = second["extra_messages"][: second["extra_messages"].index(_snapshot_messages(second)[0])]
    assert history, "history should carry the previous hand"
    assert all(m.get("role") != "user" for m in history)
    history_text = "\n".join(_message_text(m) for m in history)
    assert "Current position:" not in history_text
    assert "Rules:" not in history_text
    assert first_snapshot not in history_text
    # Exactly one fresh snapshot, which is newer than the replayed hand.
    snapshots = _snapshot_messages(second)
    assert len(snapshots) == 1
    assert "Moves so far: 3" in _message_text(snapshots[0])


async def test_agent_view_text_defaults_keep_stable_blocks() -> None:
    """Other callers keep the full default view (backward compatible)."""
    match = create_match(
        NewMatchOptions(
            id="default-view",
            kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t0", joined=True, actor="u0"),
                NewSeat(kind="ai", token=""),
            ],
            seed=1,
            now=1000,
        )
    )
    view = agent_view_text(match, 1)
    assert "Rules:" in view
    assert "Move syntax:" in view
    assert "You are" in view

    trimmed = agent_view_text(
        match,
        1,
        include_rules=False,
        include_move_syntax=False,
        include_instructions=False,
    )
    assert "Rules:" not in trimmed
    assert "Move syntax:" not in trimmed
    assert "You are 甲 in seat 1" not in trimmed
    # Seat 0 (black) opens in this fixture, so seat 1 is not on turn: the
    # status line is factual either way and carries no instruction text.
    assert "Call the `play` tool" not in trimmed
    assert "Status: waiting for" in trimmed or "Status: YOUR TURN." in trimmed
    assert "Current position:" in trimmed
    # Dynamic side labels stay in the seats block.
    assert "Seats:" in trimmed


# ----------------------------------------------------------------------
#  Doudizhu: authorised private hand only, dynamic side never in prefix
# ----------------------------------------------------------------------
async def test_doudizhu_private_hand_only_for_agent_seat(tmp_path) -> None:
    router = RecordingRouter([{"type": "tool", "move": "pass"}])
    room = await _doudizhu_room()
    assert room.match.state["turn"] == 1
    agent = GameAgent(FakePlugin(router, _cfg(), tmp_path))

    await agent.run(room, 1)

    call = router.calls[0]
    snapshot_text = _message_text(_snapshot_messages(call)[0])
    hand = room.match.state["cards"][1]
    assert "Your PRIVATE hand" in snapshot_text
    # The authorised seat sees its own hand (rank text, suits included).
    assert all(card in snapshot_text for card in hand)
    # Other seats' private hands never leak into the request: every card is a
    # unique "suit+rank" token, so a foreign token must be absent verbatim.
    # Revealed bottom cards are public and therefore allowed.
    request = _request_text(call)
    own = set(hand) | set(room.match.state.get("bottom") or ())
    # "BJ"/"RJ" are also rule vocabulary (rank order), so only suit-prefixed
    # tokens (S3/H3/...) are unambiguous private-hand evidence.
    for other in (0, 2):
        for card in room.match.state["cards"][other]:
            if card in own or len(card) < 3:
                continue
            assert card not in request.split(), f"foreign card leaked: {card}"
    # The stable prefix keeps fixed seat identity only (no dynamic side line).
    system = call["system_prompt"]
    assert "第1席" in system
    assert "座位名：对手" in system
    assert "你是地主" not in system
    assert "你是农民" not in system
    assert "playing alone" not in system
    assert "partner=" not in system
    # Dynamic side comes from the current snapshot; the private-hand safety
    # boundary is stated once in the stable system, not in the snapshot.
    assert "地主" in snapshot_text or "农民" in snapshot_text
    assert "Table talk:" not in snapshot_text
    assert "never reveal your private hand" not in snapshot_text
    assert "do not repeat in table speech" not in snapshot_text
    assert "绝不透露或复述私有" in system


async def test_doudizhu_rules_once_and_no_private_hand_in_history(tmp_path) -> None:
    router = RecordingRouter([{"type": "tool", "move": "pass"}])
    room = await _doudizhu_room()
    agent = GameAgent(FakePlugin(router, _cfg(), tmp_path))

    await agent.run(room, 1)
    hand = set(room.match.state["cards"][1])
    # Cross-hand history keeps actions only, never the private hand.
    history = agent._history.window(room.match.id, 1, -1)
    history_text = "\n".join(_message_text(m) for m in history)
    assert "PRIVATE hand" not in history_text
    assert all(m.get("role") != "user" for m in history)
    public = set(room.match.state.get("bottom") or ())
    for card in room.match.state["cards"][0] + room.match.state["cards"][2]:
        if card not in hand and card not in public:
            assert card not in history_text.split(), (
                f"foreign card leaked into history: {card}"
            )

    dz_module = games.get("doudizhu")
    stable_rules = _stable_rules_block(dz_module)
    assert _request_text(router.calls[0]).count(stable_rules) == 1
    assert str(dz_module.rules) not in _request_text(router.calls[0])
    assert "Moves:" not in stable_rules
    assert "Rules:" not in _message_text(_snapshot_messages(router.calls[0])[0])


async def test_custom_system_prompt_keeps_rules_and_identity_in_init(tmp_path) -> None:
    """Custom ``ai_agent_prompt`` replaces system; rules/seat identity stay in init."""
    router = RecordingRouter([{"type": "tool", "move": "J9"}])
    room = await _gomoku_room()
    agent = GameAgent(
        FakePlugin(router, _cfg(ai_agent_prompt="CUSTOM-PLAYER-PROMPT"), tmp_path)
    )

    await agent.run(room, 1)

    call = router.calls[0]
    assert call["system_prompt"] == "CUSTOM-PLAYER-PROMPT"
    init = call["user_prompt"]
    stable_rules = _stable_rules_block(games.get("gomoku"))
    assert stable_rules in init
    assert str(games.get("gomoku").rules) not in init
    assert "你是nori" in init
    assert "第1席" in init
    assert "绝不透露或复述私有" in init
    assert _request_text(call).count("绝不透露或复述私有") == 1
    assert _request_text(call).count(stable_rules) == 1
    assert "Rules:" not in _message_text(_snapshot_messages(call)[0])
