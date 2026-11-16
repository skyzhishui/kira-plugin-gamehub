"""game_agent 集成测试：插件自持循环 + 假 host.llm.run_replyer_stream。

覆盖：合法一步、非法/未知/坏参纠正、5 轮末轮工具保留、每轮调用上限、
标准 assistant/tool 配对、成功后不收回合、跨手历史窗口/隔离、当前局面
后置、plan 注入、reasoning 契约、超时/failover/取消/迟到/no_tool/bot 回归、
模型清单（ai_models）与 ai_mode=bot 的 KiraAI 配置口径。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from engine import (
    GameModule,
    MoveResultErr,
    MoveResultOk,
    NewMatchOptions,
    NewSeat,
    PlayerSpec,
    agent_view_text,
    create_match,
    games,
)
from engine.match import Match
from game_agent import (
    PLAY_TOOL_DEF,
    TEXT_ONLY_REMINDER,
    GameAgent,
    _stable_rules_block,
    build_play_tool_def,
)
from host import llm as host_llm
from host.settings import Settings
from room import GameRoom, RoomManager


@pytest.fixture(scope="session", autouse=True)
def _host_core_importable() -> None:
    """Make ``core.*`` importable from the repo root for the host adapter tests.

    The plugin suite runs with the standalone interpreter (not the full host
    dependency set), so the two heavy ``core`` modules ``host.llm`` needs are
    stubbed with the minimal surface it actually uses.
    """
    import types

    repo_root = Path(__file__).resolve().parents[4]
    if (repo_root / "core").is_dir() and str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        import core.agent.tool
    except ImportError:
        from core.agent.message import OpenAIMessage  # pydantic-only module

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
    """Route ``host.llm.run_replyer_stream`` to the plugin's scripted router."""
    router = getattr(plugin, "_test_router", None)
    if router is None:
        raise AssertionError("test router is not attached to the plugin")
    async for event in router.run_replyer_stream(plugin, **kwargs):
        yield event


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch) -> None:
    """Every test in this module drives the agent through a scripted router."""
    monkeypatch.setattr(host_llm, "run_replyer_stream", _dispatch_stream)


_REAL_RUN = host_llm.run_replyer_stream


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


class FakeRouter:
    """脚本化 host.llm.run_replyer_stream：按全局调用顺序消费 script。

    每个元素可以是：
    - ``{"type": "tool", "move": "J9"}``：一次 play 调用；
    - ``{"type": "calls", "calls": [{"name": ..., "arguments": ...}]}``：
      任意多个工具调用（用于上限/未知/坏参测试）；
    - ``{"type": "text", "text": "..."}``：纯文本轮（无工具）；
    - ``{"type": "hang"}``：挂起直到被取消/超时；
    - ``Exception`` 实例：抛异常；
    - 可调用对象：先 ``await step(router)`` 再继续（用于并发/迟到测试）。

    任意 step 可带 ``reasoning``（观察增量）与 ``contract_reasoning``
    （流末回传契约键）。
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()
        # Emulate a host with no default LLM client (empty model list).
        self.no_default = False

    def _make_calls(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        call_no = len(self.calls)
        for index, item in enumerate(items):
            arguments = item.get("arguments")
            if arguments is None:
                payload = {"move": item.get("move", "")}
                arguments = json.dumps(payload, ensure_ascii=False)
            elif not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            out.append(
                {
                    "id": str(item.get("id") or f"call-{call_no}-{index}"),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or "play"),
                        "arguments": str(arguments),
                    },
                }
            )
        return out

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
    ):
        if self.no_default and not any(str(m).strip() for m in (model_list or [])):
            # Mirrors host.llm._clients_for when no default client exists.
            raise RuntimeError("no default LLM client is configured")
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": tools,
                "media": media,
                "extra_messages": list(extra_messages or []),
                "model_list": list(model_list or []),
                "stream": stream,
            }
        )
        step = self.script.pop(0) if self.script else {"type": "text", "text": ""}
        if inspect.isawaitable(step):
            step = await step
        if callable(step):
            result = await step(self)
            step = (
                result
                if result is not None
                else (self.script.pop(0) if self.script else {"type": "text", "text": ""})
            )
        if isinstance(step, BaseException):
            raise step
        kind = step.get("type")
        if kind == "hang":
            await asyncio.Event().wait()
            return
        reasoning = step.get("reasoning")
        if reasoning:
            yield {"type": "reasoning", "delta": str(reasoning)}
        if kind == "tool":
            item = {"move": step.get("move", "")}
            calls = self._make_calls([item])
        elif kind == "calls":
            calls = self._make_calls(list(step.get("calls") or []))
        else:
            calls = None
        text = str(step.get("text") or "")
        if text:
            yield {"type": "text", "delta": text}
        final: dict[str, Any] = {
            "type": "tool_calls",
            "tool_calls": calls,
            "finish_reason": "tool_calls" if calls else "stop",
        }
        if step.get("contract_reasoning"):
            final["reasoning_content"] = str(step["contract_reasoning"])
        yield final


class FakePlugin:
    def __init__(self, router: Any, cfg: Any, data_dir: Any) -> None:
        self.ctx = SimpleNamespace(task_router=router)
        self.settings = cfg
        self.data_dir = str(data_dir)
        self.stopping = False
        self._test_router = router


class _TrioModule(GameModule):
    """非五子棋测试模块：3 座位、非坐标字符串动作、无可选威胁分析。"""

    kind = "trio_test"
    name: ClassVar[dict[str, str]] = {"zh": "三人测试", "en": "Trio Test"}
    family = "test"
    blurb = "three-seat non-coordinate test game"
    ready = True
    players = PlayerSpec(min=2, max=3, default=3)
    rules = "Choose an action token; first to two tokens wins."
    move_help = "one of red/blue/pass (action words, not coordinates)"
    tempo = "turn"

    def create(self, ctx: Any) -> Any:
        return {
            "turn": 0,
            "scores": [0] * int(ctx.players),
            "last": "",
        }

    def apply(self, state: Any, seat: int, move: str) -> Any:
        token = str(move).strip().lower()
        if token not in ("red", "blue", "pass"):
            return MoveResultErr("未知动作")
        scores = list(state["scores"])
        if token != "pass":
            scores[seat] += 1
        next_state = {
            "turn": (int(state["turn"]) + 1) % len(scores),
            "scores": scores,
            "last": token,
        }
        return MoveResultOk(next_state, token)

    def waiting_on(self, state: Any) -> list[int]:
        return [int(state["turn"])]

    def outcome(self, state: Any) -> Any:
        return None

    def seat_labels(self, state: Any) -> list[str]:
        return ["甲", "乙", "丙"][: len(state["scores"])]

    def view(self, state: Any, viewer: int | None) -> Any:
        return {**state, "you": viewer}

    def describe(self, state: Any, seat: int, names: list[str]) -> str:
        return (
            f"Seat {seat} ({names[seat]}). Last action: "
            f"{state['last'] or 'none'}. Scores: {state['scores']}."
        )

    def bot(self, state: Any, seat: int) -> str | None:
        return "pass"


def _cfg(**overrides: Any) -> Settings:
    """KiraAI Settings（nori 默认值）+ 测试覆盖。"""
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
        "turn_threat_analysis_enabled": True,
        "ai_min_think_ms": 0,
        "ai_think_jitter_ms": 0,
        "ai_agent_prompt": "",
        "ai_decision_log": True,
        "max_say_len": 280,
    }
    base.update(overrides)
    return Settings(base)


async def _ai_turn_room(manager: RoomManager) -> GameRoom:
    room, _seat, _token = await manager.create(
        kind="gomoku",
        actor="alice",
        session_id="private-alice",
        name="玩家",
        now=1000,
        # 这些测试直接覆盖 game_agent 驱动面：显式 agent（无选项 gomoku
        # ai 座位会按新规则迁移到 turn/director）。
        mode_options={"mode": "nori", "driver": "agent"},
    )
    # 人类先走 H8，轮到白棋 AI（seat 1）。
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    return room


async def _setup(
    tmp_path: Any, router: FakeRouter, cfg: Any
) -> tuple[FakePlugin, GameAgent, RoomManager, GameRoom]:
    plugin = FakePlugin(router, cfg, tmp_path)
    agent = GameAgent(plugin)
    manager = RoomManager(repository=FakeRepo(), config=cfg, now_fn=lambda: 1000)
    room = await _ai_turn_room(manager)
    return plugin, agent, manager, room


def _trio_match(match_id: str = "trio-match") -> Match:
    return create_match(
        NewMatchOptions(
            id=match_id,
            kind=_TrioModule.kind,
            seats=[
                NewSeat(kind="human", token="t0", joined=True, actor="u0"),
                NewSeat(kind="ai", token=""),
                NewSeat(kind="human", token="t2", joined=True, actor="u2"),
            ],
            seed=1,
            now=1000,
        )
    )


async def _trio_ai_turn_room() -> GameRoom:
    room = GameRoom(_trio_match(), repository=FakeRepo(), now_fn=lambda: 1000)
    await room.apply(
        0, {"type": "move", "move": "red"}, client_version=room.match.version
    )
    return room


def _log_entries(tmp_path: Any) -> list[dict[str, Any]]:
    path = tmp_path / "decision_log.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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


def _current_user_index(call: dict[str, Any]) -> int:
    """extra_messages 中最新的当前局面 user 消息下标（历史在其之前）。"""
    for index, message in enumerate(call["extra_messages"]):
        if message.get("role") == "user" and "Current position:" in _message_text(
            message
        ):
            return index
    raise AssertionError("未找到当前局面 user 消息")


def _history_messages(call: dict[str, Any]) -> list[dict[str, Any]]:
    return call["extra_messages"][: _current_user_index(call)]


def _current_user_raw(call: dict[str, Any]) -> dict[str, Any]:
    return call["extra_messages"][_current_user_index(call)]


def _current_user(call: dict[str, Any]) -> dict[str, Any]:
    message = _current_user_raw(call)
    return {"role": "user", "content": _message_text(message)}


def _tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") == "tool"]


def _history_entry(agent: GameAgent, match_id: str, seat: int = 1) -> list[dict[str, Any]]:
    return agent._history.window(match_id, seat, -1)


def _assert_pairs_closed(messages: list[dict[str, Any]]) -> None:
    """每个 assistant.tool_calls 都有对应 tool result，且没有多余 tool。"""
    declared: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                declared.add(str(call.get("id") or ""))
        elif message.get("role") == "tool":
            assert str(message.get("tool_call_id") or "") in declared
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = str(call.get("id") or "")
                assert any(
                    m.get("role") == "tool"
                    and str(m.get("tool_call_id") or "") == call_id
                    for m in messages
                ), f"悬挂 tool_call: {call_id}"


# ----------------------------------------------------------------------
#  合法一步 / 非法纠正 / 自定义 prompt
# ----------------------------------------------------------------------
async def test_legal_one_move_no_wasted_round(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9", "text": "开局我先占中路"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.play(room, 1)

    assert room.match.state["moves"] == 2
    assert room.match.log[-1].seat == 1
    assert room.match.log[-1].move == "J9"
    # 成功轮正文作为对手气泡落库（内部 say 传输）
    assert room.match.chat[-1].text == "开局我先占中路"
    assert len(router.calls) == 1  # 成功后立即收束，不再多花一轮
    assert router.calls[0]["media"] is None  # 无媒体输入时正常无图
    assert isinstance(_current_user_raw(router.calls[0])["content"], str)
    tool_names = [
        (tool.get("function") or {}).get("name")
        for tool in (router.calls[0]["tools"] or [])
    ]
    assert tool_names == ["play"]  # 只暴露内部 play，没有任何外部工具
    props = router.calls[0]["tools"][0]["function"]["parameters"]["properties"]
    assert set(props) == {"move"}  # 模型可见参数面没有 say
    entries = _log_entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "agent"
    assert entry["move"] == "J9"
    assert entry["model"] == "m1"
    assert entry["fallback"] == ""
    assert entry["retries"] == 0
    assert entry["seat"] == 1
    assert entry["move_count"] == 1
    assert entry["board_hash"]
    for key in (
        "t", "match_id", "seat", "move_count", "model", "plan_seq",
        "move", "source", "fallback", "retries", "elapsed_ms", "board_hash",
    ):
        assert key in entry
    # 不记录聊天全文/token
    assert "chat" not in entry and "token" not in entry

    # 标准 assistant.tool_calls + tool.tool_call_id 配对写入跨手历史。
    history = _history_entry(agent, room.match.id)
    assert [m["role"] for m in history] == ["assistant", "tool"]
    call = history[0]["tool_calls"][0]
    assert call["id"] == history[1]["tool_call_id"]
    assert call["function"] == {"name": "play", "arguments": json.dumps({"move": "J9"})}
    assert "已执行动作 J9" in history[1]["content"]
    _assert_pairs_closed(history)


def test_play_tool_def_exposes_only_move() -> None:
    """模型可见定义只有 move；描述只留动作格式与可选文本，无行为引导。"""
    # 无游戏模块（无 move_help）时的精确回退文案
    assert build_play_tool_def(None)["function"]["description"] == (
        "在当前对局执行一步。move 的格式由本局规则决定，可配合文本输出一起使用。"
    )
    gomoku = games.get("gomoku")
    assert build_play_tool_def(gomoku)["function"]["description"] == (
        "在当前对局执行一步。move 的格式由本局规则决定"
        f"（{str(gomoku.move_help).strip()}），可配合文本输出一起使用。"
    )
    for module in (None, games.get("gomoku")):
        definition = build_play_tool_def(module)
        function = definition["function"]
        props = function["parameters"]["properties"]
        assert set(props) == {"move"}
        assert "say" not in props
        assert function["parameters"]["required"] == ["move"]
        assert "say" not in json.dumps(definition, ensure_ascii=False)
        description = str(function["description"])
        assert "在当前对局执行一步" in description
        assert "可配合文本输出一起使用" in description
        for token in ("同一次回复里写一两句", "submit_reply", "人设", "对手"):
            assert token not in description
    assert set(PLAY_TOOL_DEF["function"]["parameters"]["properties"]) == {"move"}
    assert "可配合文本输出一起使用" in str(PLAY_TOOL_DEF["function"]["description"])


async def test_empty_round_text_still_applies_action(tmp_path) -> None:
    """提示词要求同轮文本，但运行时不因正文为空拒绝动作。"""
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    assert room.match.chat == []


async def test_success_round_text_becomes_opponent_bubble(tmp_path) -> None:
    """成功轮普通正文作为内部 say 传输写入 Match.chat（对手气泡）。"""
    router = FakeRouter(
        [{"type": "tool", "move": "J9", "text": "这一手我拿下了"}]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    assert room.match.chat
    assert room.match.chat[-1].seat == 1
    assert room.match.chat[-1].text == "这一手我拿下了"
    assert len(router.calls) == 1  # 说话不额外花一轮，也不会重试落子
    # 正文作为 assistant content 留在跨手历史，tool 配对完整
    history = _history_entry(agent, room.match.id)
    assert history[0]["content"] == "这一手我拿下了"
    _assert_pairs_closed(history)


async def test_success_round_text_is_capped_by_max_say_len(tmp_path) -> None:
    router = FakeRouter(
        [{"type": "tool", "move": "J9", "text": "一二三四五六七八九十一二三四五"}]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(max_say_len=5))

    await agent.run(room, 1)

    assert room.match.chat[-1].text == "一二三四五"


async def test_pure_text_round_never_persists_chat(tmp_path) -> None:
    """纯文字轮（无工具）绝不落 Match.chat；提醒后继续，最终正常落子。"""
    router = FakeRouter(
        [
            {"type": "text", "text": "这局我慢慢来"},
            {"type": "tool", "move": "J9"},
        ]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    assert all("慢慢来" not in c.text for c in room.match.chat)
    # 纯文字轮计一轮，第二轮拿到提醒，且提醒本身不要求纯文字
    second = router.calls[1]
    assert any(
        m.get("role") == "user" and TEXT_ONLY_REMINDER in _message_text(m)
        for m in second["extra_messages"]
    )


async def test_failed_round_text_never_persists_chat(tmp_path) -> None:
    """非法落子轮的正文绝不写入对局聊天；下一手纠正正文才生效。"""
    router = FakeRouter(
        [
            {"type": "tool", "move": "Z99", "text": "说早了"},
            {"type": "tool", "move": "J9", "text": "这手才是真的"},
        ]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    assert all("说早了" not in c.text for c in room.match.chat)
    assert room.match.chat[-1].text == "这手才是真的"


async def test_reasoning_never_persists_as_chat(tmp_path) -> None:
    """reasoning（观察增量/契约回传）绝不作为正文落 Match.chat。"""
    router = FakeRouter(
        [
            {
                "type": "tool",
                "move": "J9",
                "reasoning": "先算三连威胁",
                "contract_reasoning": "隐藏思考：这里要挡活三",
            }
        ]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    assert room.match.chat == []
    history = _history_entry(agent, room.match.id)
    assert "隐藏思考" not in json.dumps(history, ensure_ascii=False)
    assert "先算三连威胁" not in json.dumps(history, ensure_ascii=False)


async def test_stale_round_text_never_persists_chat(tmp_path) -> None:
    """stale（局面已推进）轮正文不落库，也不触发 bot 补手。"""
    router = FakeRouter([])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    async def push_then_late(_router: FakeRouter) -> dict[str, Any]:
        from engine.match import apply_match_action

        async with room.lock:
            result = apply_match_action(
                room.match, 1, {"type": "move", "move": "K9"}, 1000
            )
            assert result.ok
            room.match = result.match
        return {"type": "tool", "move": "J9", "text": "迟到的发言"}

    router.script = [push_then_late]
    await agent.run(room, 1)

    assert room.match.log[-1].move == "K9"
    assert all("迟到的发言" not in c.text for c in room.match.chat)
    entries = _log_entries(tmp_path)
    assert entries and entries[-1]["fallback"] == "stale"


async def test_illegal_move_gets_readable_error_and_correction(tmp_path) -> None:
    # Z99 越界/非法 → 第二轮 J9 纠正。
    router = FakeRouter(
        [{"type": "tool", "move": "Z99"}, {"type": "tool", "move": "J9"}]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2
    assert room.match.log[-1].move == "J9"
    assert len(router.calls) == 2
    second = router.calls[1]
    tool_results = _tool_messages(second["extra_messages"])
    assert tool_results
    assert "失败" in tool_results[-1]["content"]
    assert "Z99" in tool_results[-1]["content"]
    # 错误 tool result 不再附带旧棋盘/旧分析。
    assert "Gomoku" not in tool_results[-1]["content"]
    assert "Current plan" not in tool_results[-1]["content"]
    # 当前局面仍在历史之后作为最新 user 消息提供。
    assert "Gomoku" in _current_user(second)["content"]
    entries = _log_entries(tmp_path)
    assert entries[-1]["retries"] == 1
    history = _history_entry(agent, room.match.id)
    assert len([m for m in history if m["role"] == "assistant"]) == 2
    _assert_pairs_closed(history)


async def test_custom_system_prompt_is_used(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    cfg = _cfg(ai_agent_prompt="CUSTOM-PLAYER-PROMPT")
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    await agent.run(room, 1)

    assert router.calls[0]["system_prompt"] == "CUSTOM-PLAYER-PROMPT"


async def test_system_prompt_is_stable_across_turns(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}, {"type": "tool", "move": "K9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)
    first_system = router.calls[0]["system_prompt"]
    # 对手昵称变化不应影响 system（计划/局面都在 user 侧）。
    room.match.seats[0].name = "新昵称"
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)

    assert router.calls[-1]["system_prompt"] == first_system
    assert "新昵称" not in first_system


# ----------------------------------------------------------------------
#  失败路径：无工具 / 异常 / 超时 / 空模型 / bot 模式 → bot
# ----------------------------------------------------------------------
async def test_no_tool_call_reminds_until_budget_then_bot(tmp_path) -> None:
    router = FakeRouter([{"type": "text", "text": "我看看"}])
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_agent_rounds=5)
    )

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2
    assert room.match.log[-1].seat == 1
    assert len(router.calls) == 5  # 纯文本也计轮，提示后重试到预算
    reminders = [
        m
        for m in router.calls[1]["extra_messages"]
        if m.get("role") == "user" and "play 工具" in str(m.get("content"))
    ]
    assert reminders
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "no_tool_call"
    history = _history_entry(agent, room.match.id)
    assert len(history) == 2  # bot 兜底也留完整事实，不悬空
    assert "bot" in history[0]["content"] or "bot" in history[1]["content"]
    _assert_pairs_closed(history)


async def test_model_exception_falls_back_to_bot(tmp_path) -> None:
    router = FakeRouter([RuntimeError("provider down")])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "error"


async def test_timeout_falls_back_to_bot_within_budget(tmp_path) -> None:
    router = FakeRouter([{"type": "hang"}])
    cfg = _cfg(ai_timeout=0.15, ai_min_think_ms=0)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    started = time.monotonic()
    await agent.run(room, 1)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert room.match.state["moves"] == 2
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "timeout"


async def test_runtime_defaults_when_new_config_fields_absent(tmp_path) -> None:
    """配置里缺少新字段时，getattr 默认值仍可运行。"""
    cfg = _cfg()
    for key in (
        "ai_agent_rounds",
        "ai_agent_calls_per_round",
        "ai_history_rounds",
        "turn_threat_analysis_enabled",
    ):
        delattr(cfg, key)
    router = FakeRouter([{"type": "text", "text": ""}])
    _plugin, agent, _manager, room = await _setup(tmp_path, router, cfg)

    await agent.run(room, 1)

    assert len(router.calls) == 5  # 默认 rounds=5
    assert room.match.state["moves"] == 2
    assert _log_entries(tmp_path)[-1]["fallback"] == "no_tool_call"


async def test_empty_models_follow_the_host_default_llm(tmp_path) -> None:
    """Empty ``ai_models`` = host default LLM (locked decision), not the bot."""
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(ai_models=[]))

    await agent.run(room, 1)

    assert len(router.calls) == 1  # the model played on the first round
    # An empty entry tells host.llm to resolve the host default client.
    assert router.calls[0]["model_list"] == [""]
    assert room.match.state["moves"] == 2  # the model move landed
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "agent"
    assert entry["fallback"] == ""
    assert entry["model"] == "default"


async def test_empty_models_without_host_client_falls_back_to_bot(tmp_path) -> None:
    """No configured models *and* no host default client → rule bot."""
    router = FakeRouter([])
    router.no_default = True  # emulate a host without a default LLM
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(ai_models=[]))

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "no_model"


async def test_ai_mode_bot_skips_llm_and_uses_rule_bot(tmp_path) -> None:
    """ai_mode=bot：完全不调用 LLM，直接规则 bot 补手。"""
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_mode="bot", ai_models=["provider:model"])
    )

    await agent.run(room, 1)

    assert router.calls == []
    assert room.match.state["moves"] == 2
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "mode_bot"


async def test_ai_mode_bot_paces_the_rule_bot_move(tmp_path) -> None:
    """ai_mode=bot：规则 bot 也要先等拟人化延迟，再落子/出牌。"""
    router = FakeRouter([])
    cfg = _cfg(
        ai_mode="bot",
        ai_models=["provider:model"],
        bot_move_delay_min_ms=120,
        bot_move_delay_max_ms=120,
    )
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await agent.run(room, 1)

    assert loop.time() - started >= 0.12  # 不是瞬间应手
    assert router.calls == []
    assert room.match.state["moves"] == 2
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "mode_bot"


# ----------------------------------------------------------------------
#  5 轮末轮工具保留 / 每轮调用上限 / 坏参与未知工具
# ----------------------------------------------------------------------
async def test_fifth_round_still_has_play_and_can_succeed(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "tool", "move": "Z99"},
            {"type": "tool", "move": "Z98"},
            {"type": "tool", "move": "Z97"},
            {"type": "tool", "move": "Z96"},
            {"type": "tool", "move": "J9"},
        ]
    )
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_agent_rounds=5)
    )

    await agent.run(room, 1)

    assert len(router.calls) == 5
    for call in router.calls:
        names = [
            (tool.get("function") or {}).get("name")
            for tool in (call["tools"] or [])
        ]
        assert names == ["play"]  # 末轮也保留 play，不注入 final hint 收走
    assert room.match.log[-1].move == "J9"
    history = _history_entry(agent, room.match.id)
    assert len([m for m in history if m["role"] == "assistant"]) == 5
    _assert_pairs_closed(history)


async def test_per_round_call_limit_truncates_without_dangling(tmp_path) -> None:
    router = FakeRouter(
        [
            {
                "type": "calls",
                "calls": [
                    {"move": "Z99"},
                    {"move": "J9"},
                ],
            },
            {"type": "tool", "move": "J9"},
        ]
    )
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_agent_calls_per_round=1, ai_agent_rounds=5)
    )

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    assert len(router.calls) == 2
    # 第二次调用时，第一轮的 assistant 声明只保留被执行的 1 个 call。
    transcript = router.calls[1]["extra_messages"][
        _current_user_index(router.calls[1]) + 1:
    ]
    assistants = [m for m in transcript if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert len(assistants[0]["tool_calls"]) == 1
    assert json.loads(assistants[0]["tool_calls"][0]["function"]["arguments"])["move"] == "Z99"
    _assert_pairs_closed(transcript)
    history = _history_entry(agent, room.match.id)
    assert all(len(m.get("tool_calls") or []) <= 1 for m in history if m["role"] == "assistant")
    _assert_pairs_closed(history)


async def test_multiple_standard_tool_call_ids_are_preserved(tmp_path) -> None:
    """fake host.llm 产出完整标准 event；多调用 id/配对原样保留。"""
    router = FakeRouter(
        [
            {
                "type": "calls",
                "calls": [
                    {
                        "id": "call-alpha",
                        "name": "not_a_tool",
                        "arguments": {"x": 1},
                    },
                    {
                        "id": "call-beta",
                        "name": "play",
                        "arguments": {"move": "J9"},
                    },
                ],
            }
        ]
    )
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_agent_calls_per_round=2)
    )

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    history = _history_entry(agent, room.match.id)
    assert [m["role"] for m in history] == ["assistant", "tool", "tool"]
    assert [tc["id"] for tc in history[0]["tool_calls"]] == [
        "call-alpha",
        "call-beta",
    ]
    assert [m["tool_call_id"] for m in history[1:]] == [
        "call-alpha",
        "call-beta",
    ]
    assert "未知工具" in history[1]["content"]
    assert "已执行动作 J9" in history[2]["content"]
    _assert_pairs_closed(history)


async def test_unknown_malformed_empty_and_illegal_then_success(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "calls", "calls": [{"name": "not_a_tool", "arguments": {"x": 1}}]},
            {"type": "calls", "calls": [{"name": "play", "arguments": "not-json"}]},
            {"type": "calls", "calls": [{"name": "play", "arguments": {"foo": 1}}]},
            {"type": "tool", "move": "Z99"},
            {"type": "tool", "move": "J9"},
        ]
    )
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_agent_rounds=5)
    )

    await agent.run(room, 1)

    assert len(router.calls) == 5
    assert room.match.log[-1].move == "J9"
    history = _history_entry(agent, room.match.id)
    texts = "\n".join(str(m.get("content") or "") for m in history)
    assert "未知工具" in texts
    assert "无法解析" in texts
    assert "缺少 move 参数" in texts
    assert "失败" in texts
    assert len([m for m in history if m["role"] == "assistant"]) == 5
    _assert_pairs_closed(history)


async def test_reasoning_contract_only_and_history_strips_it(tmp_path) -> None:
    router = FakeRouter(
        [
            {
                "type": "tool",
                "move": "Z99",
                "reasoning": "hidden-delta",
                "contract_reasoning": "contract-thought",
            },
            {"type": "tool", "move": "J9"},
        ]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    second = router.calls[1]["extra_messages"]
    assistant = next(
        m for m in second if m.get("role") == "assistant" and m.get("tool_calls")
    )
    # 只回传流末契约键，不回放观察 delta。
    assert assistant.get("reasoning_content") == "contract-thought"
    assert "hidden-delta" not in json.dumps(second, ensure_ascii=False)
    # 跨手历史不携带隐藏推理。
    history = _history_entry(agent, room.match.id)
    assert all("reasoning_content" not in m for m in history)
    assert "hidden-delta" not in json.dumps(history, ensure_ascii=False)
    assert "contract-thought" not in json.dumps(history, ensure_ascii=False)


# ----------------------------------------------------------------------
#  failover / 成功后不二次 bot
# ----------------------------------------------------------------------
async def test_multi_model_failover(tmp_path) -> None:
    router = FakeRouter([RuntimeError("bad model"), {"type": "tool", "move": "J9"}])
    cfg = _cfg(ai_models=["bad", "good"], ai_max_concurrency=1)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2
    assert [c["model_list"] for c in router.calls] == [["bad"], ["good"]]
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "agent"
    assert entry["model"] == "good"


async def test_failover_discards_failed_model_transcript(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "tool", "move": "Z99"},
            RuntimeError("bad model"),
            {"type": "tool", "move": "J9"},
        ]
    )
    cfg = _cfg(ai_models=["bad", "good"], ai_agent_rounds=5)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    await agent.run(room, 1)

    assert room.match.log[-1].move == "J9"
    history = _history_entry(agent, room.match.id)
    # 失败模型的半截 transcript 不落历史；只保留成功模型的完整配对。
    assert len([m for m in history if m["role"] == "assistant"]) == 1
    assert "Z99" not in json.dumps(history, ensure_ascii=False)
    _assert_pairs_closed(history)


async def test_success_prevents_later_error_second_bot(tmp_path) -> None:
    # 落子成功后不会再有第二次模型调用，因此也不会因“后续轮错误”二次 bot。
    router = FakeRouter([{"type": "tool", "move": "J9"}, RuntimeError("late")])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)

    assert len(router.calls) == 1
    assert room.match.state["moves"] == 2
    assert room.match.log[-1].move == "J9"
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "agent"


# ----------------------------------------------------------------------
#  取消 / 终局 / 迟到
# ----------------------------------------------------------------------
async def test_stopping_skips_llm_and_bot(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    plugin, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_timeout=5.0)
    )
    before = room.match.state["moves"]

    plugin.stopping = True
    await agent.run(room, 1)
    assert router.calls == []
    assert room.match.state["moves"] == before

    # 模型调用进行中停机：失败兜底也必须在提交前被 stopping 拦住。
    plugin.stopping = False

    async def mark(inner: FakeRouter) -> dict[str, Any]:
        plugin.stopping = True
        return {"type": "text", "text": "思考中"}

    router.script = [mark]
    await agent.run(room, 1)
    assert len(router.calls) == 1
    assert room.match.state["moves"] == before


async def test_stopping_during_min_think_does_not_commit(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    cfg = _cfg(ai_timeout=5.0, ai_min_think_ms=300, ai_think_jitter_ms=0)
    plugin, agent, _manager, room = await _setup(tmp_path, router, cfg)
    before = room.match.state["moves"]

    task = asyncio.create_task(agent.run(room, 1))
    deadline = asyncio.get_running_loop().time() + 1.0
    while not router.calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert router.calls
    # 此时 play 已在 _wait_min_think 中；停机后醒来必须再次检查并放弃提交。
    await asyncio.sleep(0.05)
    plugin.stopping = True
    with pytest.raises(asyncio.CancelledError):
        await task

    assert room.match.state["moves"] == before
    entry = _log_entries(tmp_path)[-1]
    assert entry["fallback"] == "cancelled"
    assert entry["source"] == "none"


async def test_drive_ai_exception_does_not_cross_turn_bot(tmp_path) -> None:
    """驱动异常时不得用无快照 ai_step 补到已推进的新回合。

    KiraAI 的驱动入口是 ``host.runtime._drive_ai`` → ``agent.play``；这里用
    等价的 room ``drive_ai_hook`` 包装验证同一语义。
    """
    manager = RoomManager(repository=FakeRepo(), config=_cfg(), now_fn=lambda: 1000)
    room = await _ai_turn_room(manager)
    started = asyncio.Event()
    release = asyncio.Event()

    class _BoomAgent:
        async def play(self, room: Any, seat: int) -> None:
            started.set()
            await release.wait()
            raise RuntimeError("boom")

    boom = _BoomAgent()

    async def drive_hook(target_room: Any, seat: int) -> None:
        await boom.play(target_room, seat)

    task = asyncio.create_task(drive_hook(room, 1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # 异常等待期间局面跨回合推进：同座位先落一子 → 人类应手 → 又轮到 AI。
    from engine.match import apply_match_action

    async with room.lock:
        result = apply_match_action(room.match, 1, {"type": "move", "move": "J9"}, 1000)
        assert result.ok
        room.match = result.match
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    assert room.match.state["moves"] == 3
    assert room.match.log[-1].move == "K8"

    release.set()
    with pytest.raises(RuntimeError):
        await task

    # 没有无快照 ai_step 兜底：不会在“新回合”上再落第四手。
    assert room.match.state["moves"] == 3
    assert room.match.log[-1].move == "K8"


async def test_external_cancel_does_not_bot(tmp_path) -> None:
    started = asyncio.Event()

    async def wait_for_cancel(router: FakeRouter) -> None:
        started.set()
        await asyncio.Event().wait()

    router = FakeRouter([wait_for_cancel])
    cfg = _cfg(ai_timeout=5.0)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)
    before = room.match.state["moves"]

    task = asyncio.create_task(agent.run(room, 1))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.05)
    assert room.match.state["moves"] == before
    assert agent._history.entry_count(room.match.id, 1) == 0
    entry = _log_entries(tmp_path)[-1]
    assert entry["fallback"] == "cancelled"
    assert entry["source"] == "none"


async def test_cancel_after_illegal_pair_does_not_write_partial_history(tmp_path) -> None:
    """取消时只丢弃当轮本地 transcript；完整非法配对也不能半截落历史。"""
    entered = asyncio.Event()

    async def hang(router: FakeRouter) -> None:
        entered.set()
        await asyncio.Event().wait()

    router = FakeRouter([{"type": "tool", "move": "Z99"}, hang])
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_timeout=5.0)
    )
    before = room.match.state["moves"]

    task = asyncio.create_task(agent.run(room, 1))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert room.match.state["moves"] == before
    assert agent._history.entry_count(room.match.id, 1) == 0
    assert _log_entries(tmp_path)[-1]["fallback"] == "cancelled"


async def test_cancel_does_not_pollute_later_or_other_history(tmp_path) -> None:
    started = asyncio.Event()

    async def hang(router: FakeRouter) -> None:
        started.set()
        await asyncio.Event().wait()

    router = FakeRouter([hang, {"type": "tool", "move": "J9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(ai_timeout=5.0))
    task = asyncio.create_task(agent.run(room, 1))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent._history.entry_count(room.match.id, 1) == 0

    # 同一局下一次决策仍能成功，历史只有成功这一手。
    await agent.run(room, 1)
    history = _history_entry(agent, room.match.id)
    assert len([m for m in history if m["role"] == "assistant"]) == 1
    assert room.match.log[-1].move == "J9"
    _assert_pairs_closed(history)


async def test_queue_wait_cancel_does_not_bot(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(ai_timeout=5.0))
    before = room.match.state["moves"]
    await agent._semaphore.acquire()  # 模拟并发已满，第二手在排队
    try:
        task = asyncio.create_task(agent.run(room, 1))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        agent._semaphore.release()

    await asyncio.sleep(0.05)
    assert room.match.state["moves"] == before
    assert router.calls == []


async def test_semaphore_queue_timeout_falls_back_to_bot(tmp_path) -> None:
    router = FakeRouter([])
    cfg = _cfg(ai_timeout=0.1, ai_min_think_ms=0)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)
    await agent._semaphore.acquire()
    try:
        started = time.monotonic()
        await agent.run(room, 1)
        assert time.monotonic() - started < 1.0
    finally:
        agent._semaphore.release()

    assert room.match.state["moves"] == 2
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "bot"
    assert entry["fallback"] == "timeout"


async def test_cancel_after_game_over_does_not_bot(tmp_path) -> None:
    started = asyncio.Event()

    async def wait_then_end(router: FakeRouter) -> None:
        started.set()
        await asyncio.Event().wait()

    router = FakeRouter([wait_then_end])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(ai_timeout=5.0))
    task = asyncio.create_task(agent.run(room, 1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await room.apply(0, {"type": "resign"}, client_version=room.match.version)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.05)
    assert room.status == "over"
    assert room.match.log[-1].move == "resign"


async def test_late_decision_does_not_commit_stale_turn(tmp_path) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def wait_then_play(router: FakeRouter) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"type": "tool", "move": "J8"}

    router = FakeRouter([wait_then_play])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg(ai_timeout=5.0))
    task = asyncio.create_task(agent.run(room, 1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # 迟到场景：等待期间同座位已经落过一子（例如另一个驱动已提交）。
    from engine.match import apply_match_action

    async with room.lock:
        result = apply_match_action(
            room.match, 1, {"type": "move", "move": "K9"}, 1000
        )
        assert result.ok
        room.match = result.match
    release.set()
    await task

    moves = [entry.move for entry in room.match.log]
    assert moves == ["H8", "K9"]  # 迟到 J8 被锁内拒绝，没有补第三手
    assert not any(entry["source"] == "bot" for entry in _log_entries(tmp_path))
    assert agent._history.entry_count(room.match.id, 1) == 0


# ----------------------------------------------------------------------
#  并发 / 最小思考时间
# ----------------------------------------------------------------------
async def test_concurrency_semaphore_limits_parallel_agents(tmp_path) -> None:
    router = FakeRouter([])

    async def blocking(inner: FakeRouter) -> dict[str, Any]:
        inner.active += 1
        inner.max_active = max(inner.max_active, inner.active)
        try:
            await inner.release.wait()
        finally:
            inner.active -= 1
        return {"type": "tool", "move": "J9"}

    router.script = [blocking, blocking, blocking]
    cfg = _cfg(ai_max_concurrency=2, ai_timeout=5.0)
    plugin = FakePlugin(router, cfg, tmp_path)
    agent = GameAgent(plugin)
    manager = RoomManager(repository=FakeRepo(), config=cfg, now_fn=lambda: 1000)
    rooms = []
    for index in range(3):
        room, _seat, _token = await manager.create(
            kind="gomoku",
            actor=f"u{index}",
            session_id=f"private-u{index}",
            name=f"u{index}",
            now=1000,
        )
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
        rooms.append(room)

    tasks = [asyncio.create_task(agent.run(room, 1)) for room in rooms]
    await asyncio.sleep(0.1)
    assert router.max_active == 2
    router.release.set()
    await asyncio.gather(*tasks)
    assert router.max_active == 2
    assert all(room.match.state["moves"] == 2 for room in rooms)


async def test_min_think_delays_commit_not_after(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    cfg = _cfg(ai_min_think_ms=120, ai_think_jitter_ms=0)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)
    original = room.apply_ai_move
    committed_at: list[float] = []

    async def wrapped(seat, move, **kwargs):
        committed_at.append(time.monotonic())
        return await original(seat, move, **kwargs)

    room.apply_ai_move = wrapped  # type: ignore[method-assign]
    started = time.monotonic()
    await agent.run(room, 1)
    finished = time.monotonic()

    assert committed_at
    assert committed_at[0] - started >= 0.11
    # 落子后没有再补睡一整段思考时间。
    assert finished - committed_at[0] < 0.12
    assert room.match.state["moves"] == 2


async def test_threat_analysis_flag_is_forwarded_to_current_view(tmp_path) -> None:
    enabled_router = FakeRouter([{"type": "tool", "move": "J9"}])
    _, enabled_agent, _manager, enabled_room = await _setup(
        tmp_path, enabled_router, _cfg(turn_threat_analysis_enabled=True)
    )
    await enabled_agent.run(enabled_room, 1)
    assert "Threat analysis" in _current_user(enabled_router.calls[-1])["content"]

    disabled_router = FakeRouter([{"type": "tool", "move": "J9"}])
    _, disabled_agent, _manager2, disabled_room = await _setup(
        tmp_path, disabled_router, _cfg(turn_threat_analysis_enabled=False)
    )
    await disabled_room.set_mode_options(level="beginner")
    await disabled_agent.run(disabled_room, 1)
    disabled_view = _current_user(disabled_router.calls[-1])["content"]
    assert "Threat analysis" not in disabled_view
    # Rules live in the stable prefix (init user); the snapshot stays dynamic.
    assert "本局规则" in disabled_router.calls[-1]["user_prompt"]
    assert "Rules:" not in disabled_view
    assert "Current position:" in disabled_view


# ----------------------------------------------------------------------
#  plan 恒注入 / 过期移除 / 黑白座位
# ----------------------------------------------------------------------
async def test_plan_injected_every_turn_then_removed_after_expiry(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "tool", "move": "J9"},
            {"type": "tool", "move": "K9"},
            {"type": "tool", "move": "L9"},
        ]
    )
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())
    plan_text = "这局放水，别把活四堵死"
    await room.set_plan(1, text=plan_text, ttl_moves=10, ttl_s=120.0)

    await agent.run(room, 1)  # 第一手
    assert plan_text in _current_user(router.calls[-1])["content"]
    assert "即使偏离默认目标" in _current_user(router.calls[-1])["content"]

    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)  # 第二手仍注入
    assert plan_text in _current_user(router.calls[-1])["content"]

    # 换成 1 手 TTL，下一步（人类落子后）即过期，不再注入。
    await room.set_plan(1, text=plan_text, ttl_moves=1, ttl_s=120.0)
    await room.apply(0, {"type": "move", "move": "L8"}, client_version=room.match.version)
    await agent.run(room, 1)
    assert plan_text not in _current_user(router.calls[-1])["content"]


def _ai_black_match() -> Match:
    return create_match(
        NewMatchOptions(
            id="black-ai",
            kind="gomoku",
            seats=[
                NewSeat(kind="ai", token=""),
                NewSeat(kind="human", token="tok", joined=True, actor="alice"),
            ],
            seed=1,
            now=1000,
        )
    )


def test_system_prompt_uses_configured_identity_not_model_alias(tmp_path) -> None:
    """内部 player 身份来自插件配置 agent_name/persona；ai_models 只是模型别名。"""
    cfg = _cfg(agent_name="蕾依", agent_persona="机敏猫娘，表达带口癖。")
    plugin = FakePlugin(FakeRouter([]), cfg, tmp_path)
    agent = GameAgent(plugin)
    teammate = create_match(
        NewMatchOptions(
            id="t-identity",
            kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t0", joined=True, actor="u0"),
                NewSeat(kind="ai", token=""),
            ],
            seed=1,
            now=1000,
            mode_options={"mode": "bot", "difficulty": "advanced"},
        )
    )
    prompt = agent._system_prompt(teammate, 1)
    assert "蕾依" in prompt and "机敏猫娘" in prompt
    assert "game_agent" not in prompt  # 模型清单别名不当成人名
    init = agent._init_prompt(teammate, 1)
    # Identity appears once in the stable prefix (system); init does not repeat.
    assert "蕾依" not in init and "诺里" not in init


def test_agent_identity_falls_back_to_generic_agent_without_persona(tmp_path) -> None:
    cfg = _cfg(agent_name="", agent_persona="")
    plugin = FakePlugin(FakeRouter([]), cfg, tmp_path)
    agent = GameAgent(plugin)
    teammate = create_match(
        NewMatchOptions(
            id="t-agent-fallback",
            kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t0", joined=True, actor="u0"),
                NewSeat(kind="ai", token=""),
            ],
            seed=1,
            now=1000,
            mode_options={"mode": "bot", "difficulty": "advanced"},
        )
    )
    prompt = agent._system_prompt(teammate, 1)
    assert "你是agent" in prompt
    assert "原本的身份/人设" not in prompt  # 无配置人格时不编造


def test_system_prompt_keeps_configured_persona_for_nori_seat(tmp_path) -> None:
    cfg = _cfg(agent_name="Nori", agent_persona="ORIGINAL-PERSONA")
    plugin = FakePlugin(FakeRouter([]), cfg, tmp_path)
    agent = GameAgent(plugin)
    match = create_match(
        NewMatchOptions(
            id="n-identity",
            kind="gomoku",
            seats=[
                NewSeat(kind="human", token="t0", joined=True, actor="u0"),
                NewSeat(kind="ai", token=""),
            ],
            seed=1,
            now=1000,
            mode_options={"mode": "nori", "driver": "agent"},
        )
    )
    prompt = agent._system_prompt(match, 1)
    assert "Nori" in prompt and "ORIGINAL-PERSONA" in prompt
    assert "保持原样" in prompt
    # Stable system: identity/seat line + optional persona line + four
    # behaviour paragraphs (speech, text-only, hidden-info safety, priority).
    # Rule text and move format live once in the stable init user prompt
    # (move format is also declared by the play tool schema).
    paragraphs = prompt.split("\n")
    assert paragraphs[0] == (
        "你是Nori，正在参与一局五子棋；你在本局固定坐在第1席（座位名：对手）。"
        "具体局面、身份阵营与行动权以最新局面为准。"
    )
    assert paragraphs[1] == (
        "以下是你原本的身份/人设，保持原样，不要因为游戏改写或覆盖："
        "ORIGINAL-PERSONA"
    )
    assert paragraphs[2:] == [
        (
            "轮到你时调用 play 工具执行本局动作，提交本手动作时需根据局势"
            "和群聊信息在同一次回复的普通正文里说一两句符合你人设的玩家"
            "短话；不要复述你的落子计划，不要解说，也不要只发一条没有"
            "工具调用的纯文字消息。动作成功后结束，不续模型轮。"
        ),
        (
            "短话写在同一回复的普通正文里，和工具调用一起提交，不通过"
            "工具参数传递，也不要只写在 reasoning/思考里；只有本手合法"
            "动作成功时才会作为你的发言被对手看到，失败时不会发出，也"
            "不需要为说话重试或改写动作。本局发言只支持文本。"
        ),
        (
            "有隐藏信息的对局只依据公开信息发言，绝不透露或复述私有"
            "手牌/策略。"
        ),
        (
            "主 agent 的总体指令优先；本局策略与 plan 是战术提示，"
            "即使偏离默认目标也优先遵从，但必须符合规则。"
        ),
    ]

    init = agent._init_prompt(match, 1)
    init_lines = init.split("\n")
    assert init_lines[0] == "本局是五子棋（Gomoku）。"
    assert init_lines[1] == "本局规则（整局有效）："
    # The static rule text keeps the rules but drops the embedded "Moves:"
    # clause; the move format is declared once by the play tool schema.
    stable_rules = _stable_rules_block(games.get("gomoku"))
    assert init_lines[2] == stable_rules
    assert "Moves:" not in stable_rules
    assert games.get("gomoku").move_help not in stable_rules
    assert "Freestyle gomoku" in stable_rules
    assert init_lines[3] == (
        "动作格式以 play 工具定义中的 move 说明为准"
        "（即本局 move_help），正文不要重复抄写。"
    )
    assert games.get("gomoku").move_help not in init
    assert "位于跨手历史之后、本手工具交互之前" in init


def test_default_settings_identity_is_nori(tmp_path) -> None:
    """Settings 默认身份：nori + 调皮爱吐槽的猫娘（KiraAI 配置默认值）。"""
    plugin = FakePlugin(FakeRouter([]), Settings({}), tmp_path)
    agent = GameAgent(plugin)
    match = _ai_black_match()
    prompt = agent._system_prompt(match, 0)
    assert "你是nori" in prompt
    assert "调皮爱吐槽的猫娘，玩游戏时会认真+毒舌" in prompt


def test_prompts_request_text_in_same_response_without_say_param(tmp_path) -> None:
    """提示词要求短话与工具调用同轮出现；不再出现 say 参数/先发言后落子。"""
    plugin = FakePlugin(FakeRouter([]), _cfg(), tmp_path)
    agent = GameAgent(plugin)
    match = _ai_black_match()

    system = agent._system_prompt(match, 0)
    init = agent._init_prompt(match, 0)
    # Behaviour instructions are stated once in the stable system; init only
    # explains the conversation structure.
    assert "在同一次回复的普通正文里说一两句" in system
    assert "在同一次回复的普通正文里" not in init
    for text in (system, init):
        assert "say" not in text
        # Removed tactical explanations / extra plan bans must not come back.
        assert "不要设置或修改 plan" not in text
        assert "判断要点" not in text
        assert "如需简短说明" not in text
        # Generic prompt: no hardcoded chess action vocabulary.
        assert "棋手" not in text
    # Only the user's requested phrase may contain "落子".
    assert system.replace("不要复述你的落子计划", "").count("落子") == 0
    assert "纯文字" in system  # explicitly forbids text-only replies
    assert "和工具调用一起提交，不通过工具参数传递" in system
    assert "不要解说" in system
    assert "不要复述你的落子计划" in system
    # Text-only agent: the boundary is stated briefly, without a long list.
    assert "本局发言只支持文本" in system
    assert "表情包" not in system and "语音" not in system
    assert "绝不透露或复述私有" in system
    assert system.count("play 工具") == 1  # submit instruction stated once


def test_system_prompt_has_no_gomoku_assumptions(tmp_path) -> None:
    cfg = _cfg()
    plugin = FakePlugin(FakeRouter([]), cfg, tmp_path)
    agent = GameAgent(plugin)
    black = _ai_black_match()  # AI seat 0
    assert black.seats[0].kind == "ai"
    prompt = agent._system_prompt(black, 0)
    assert "play 工具" in prompt
    # Rule text and move format come only from this game module (stable init
    # user + play tool schema), never from shared behaviour instructions or
    # hardcoded board symbols.
    assert _stable_rules_block(games.get("gomoku")) not in prompt

    init_prompt = agent._init_prompt(black, 0)
    assert "第0席" not in init_prompt  # identity appears once in default system
    assert _stable_rules_block(games.get("gomoku")) in init_prompt
    assert games.get("gomoku").move_help not in init_prompt
    for text in (prompt,):
        for token in (
            "15x15",
            "GTP",
            "A-P",
            "X",
            "O",
            "黑",
            "白",
            "坐标",
            "放水",
            "降低胜率",
            "对手下一局",
        ):
            assert token not in text
        assert "判断要点" not in text
        assert "不要设置或修改 plan" not in text

    # 棋种专属符号说明由模块 describe/agent_view_text 提供，不在 system 里写死。
    view = agent_view_text(black, 0)
    assert "X (black)" in view
    assert "O (white)" in view
    header = "A B C D E F G H J K L M N O P"
    assert header in view
    assert " Q " not in view
    assert "MOVE:" not in view


# ----------------------------------------------------------------------
#  跨手历史：窗口 / 隔离 / 动态局面剥离 / 最新状态后置
# ----------------------------------------------------------------------
async def test_history_has_no_old_board_plan_or_analysis(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}, {"type": "tool", "move": "K9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())
    plan_text = "这局放水"
    await room.set_plan(1, text=plan_text, ttl_moves=10, ttl_s=120.0)

    await agent.run(room, 1)
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)

    second = router.calls[-1]
    history = _history_messages(second)
    assert history  # 上一手历史确实存在
    dumped = json.dumps(history, ensure_ascii=False)
    assert "Gomoku" not in dumped
    assert "Current plan" not in dumped
    assert plan_text not in dumped
    assert "Opponent threatens" not in dumped
    assert "You can win now" not in dumped
    # 最新局面在历史之后，且包含人类最新 K8 与 plan。
    current = _current_user(second)["content"]
    assert "K8" in current
    assert plan_text in current


async def test_new_game_and_different_user_history_is_isolated(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "tool", "move": "J9"},
            {"type": "tool", "move": "K9"},
            {"type": "tool", "move": "L9"},
            {"type": "tool", "move": "M9"},
        ]
    )
    plugin = FakePlugin(router, _cfg(), tmp_path)
    agent = GameAgent(plugin)

    def match_for(match_id: str, actor: str) -> Match:
        return create_match(
            NewMatchOptions(
                id=match_id,
                kind="gomoku",
                seats=[
                    NewSeat(
                        kind="human",
                        token="tok",
                        joined=True,
                        actor=actor,
                        session_id=f"private-{actor}",
                    ),
                    NewSeat(kind="ai", token=""),
                ],
                seed=1,
                now=1000,
            )
        )

    async def room_for(match_id: str, actor: str) -> GameRoom:
        room = GameRoom(match_for(match_id, actor), repository=FakeRepo(), now_fn=lambda: 1000)
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=room.match.version)
        return room

    # 同一用户开新局：match_id 不同 → 历史不串。
    room_a = await room_for("user-a-game-1", "alice")
    await agent.run(room_a, 1)
    assert agent._history.entry_count("user-a-game-1", 1) == 1

    room_a2 = await room_for("user-a-game-2", "alice")
    await agent.run(room_a2, 1)
    assert agent._history.entry_count("user-a-game-2", 1) == 1
    # 新局第一次调用看不到上一局历史。
    assert _history_messages(router.calls[-1]) == []
    assert agent._history.entry_count("user-a-game-1", 1) == 1

    # 不同用户：同样按 match_id/seat 隔离；新局第一次调用看不到别人历史。
    room_b = await room_for("user-b-game-1", "bob")
    await agent.run(room_b, 1)
    assert agent._history.entry_count("user-b-game-1", 1) == 1
    first_b_call = router.calls[-1]
    assert _history_messages(first_b_call) == []
    assert agent._history.entry_count("user-a-game-1", 1) == 1

    # 另一局第二次决策只读取自己的历史。
    await room_a.apply(0, {"type": "move", "move": "K8"}, client_version=room_a.match.version)
    await agent.run(room_a, 1)
    history_a = _history_messages(router.calls[-1])
    dumped = json.dumps(history_a, ensure_ascii=False)
    assert "J9" in dumped
    assert "K9" not in dumped  # 同用户新局
    assert "L9" not in dumped  # 不同用户


async def test_configured_history_window_is_used_atomically(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "tool", "move": "J9"},
            {"type": "tool", "move": "Z99"},
            {"type": "tool", "move": "K9"},
            {"type": "tool", "move": "M9"},
        ]
    )
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_history_rounds=1)
    )

    await agent.run(room, 1)  # 第 1 手 J9
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)  # 第 2 手：非法纠正后 K9
    await room.apply(0, {"type": "move", "move": "L8"}, client_version=room.match.version)
    await agent.run(room, 1)  # 第 3 手：M9

    third = router.calls[-1]
    history = _history_messages(third)
    dumped = json.dumps(history, ensure_ascii=False)
    assert "K9" in dumped
    assert "J9" not in dumped  # 窗口=1，只取上一手
    # 第 2 手内部有两条完整配对，窗口不能只截到后一条。
    assistant_calls = [
        call
        for message in history
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    assert len(assistant_calls) == 2
    assert agent._history.entry_count(room.match.id, 1) == 1
    _assert_pairs_closed(history)


async def test_hot_shrink_history_rounds_trims_storage(tmp_path) -> None:
    router = FakeRouter(
        [
            {"type": "tool", "move": "J9"},
            {"type": "tool", "move": "K9"},
            {"type": "tool", "move": "L9"},
        ]
    )
    cfg = _cfg(ai_history_rounds=-1)
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    await agent.run(room, 1)
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)
    assert agent._history.entry_count(room.match.id, 1) == 2

    # 热更缩小窗口：下一次决策入口即回收旧 entry。
    cfg.ai_history_rounds = 1
    await room.apply(0, {"type": "move", "move": "L8"}, client_version=room.match.version)
    await agent.run(room, 1)

    assert agent._history.entry_count(room.match.id, 1) == 1
    remaining = json.dumps(_history_entry(agent, room.match.id), ensure_ascii=False)
    assert "L9" in remaining
    assert "J9" not in remaining and "K9" not in remaining


async def test_history_rounds_zero_disables_cross_turn_history(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}, {"type": "tool", "move": "K9"}])
    _, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_history_rounds=0)
    )

    await agent.run(room, 1)
    assert agent._history.entry_count(room.match.id, 1) == 0
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)

    # 关闭历史后，第二次决策的 extra_messages 直接从最新 user 开始。
    second = router.calls[-1]
    assert _current_user_index(second) == 0
    assert _history_messages(second) == []


async def test_context_overflow_uses_existing_failover(tmp_path) -> None:
    """超上下文不私自硬裁剪，按现有 failover 换模型（失败则 bot）。"""
    router = FakeRouter(
        [
            RuntimeError("maximum context length exceeded"),
            {"type": "tool", "move": "J9"},
        ]
    )
    cfg = _cfg(ai_models=["small", "big"])
    _, agent, _manager, room = await _setup(tmp_path, router, cfg)

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2
    assert room.match.log[-1].move == "J9"
    assert [c["model_list"] for c in router.calls] == [["small"], ["big"]]
    entry = _log_entries(tmp_path)[-1]
    assert entry["source"] == "agent"
    assert entry["model"] == "big"


async def test_run_prunes_finished_match_history(tmp_path) -> None:
    """run 入口回收已不在 active_rooms 的对局历史，当前局保留。"""
    router = FakeRouter([{"type": "tool", "move": "J9"}, {"type": "tool", "move": "K9"}])
    cfg = _cfg()
    plugin = FakePlugin(router, cfg, tmp_path)
    agent = GameAgent(plugin)

    def match_for(match_id: str) -> Match:
        return create_match(
            NewMatchOptions(
                id=match_id,
                kind="gomoku",
                seats=[
                    NewSeat(kind="human", token="tok", joined=True, actor="alice"),
                    NewSeat(kind="ai", token=""),
                ],
                seed=1,
                now=1000,
            )
        )

    async def room_for(match_id: str) -> GameRoom:
        room = GameRoom(match_for(match_id), repository=FakeRepo(), now_fn=lambda: 1000)
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=room.match.version)
        return room

    room_a = await room_for("active-a")
    room_b = await room_for("finished-b")
    active = [room_a, room_b]
    plugin.rooms = SimpleNamespace(active_rooms=lambda: list(active))

    await agent.run(room_b, 1)
    assert agent._history.entry_count("finished-b", 1) == 1

    # b 终局/卸载后从 active_rooms 消失；下一次 run 入口清理其历史。
    active.remove(room_b)
    await agent.run(room_a, 1)

    assert agent._history.entry_count("finished-b", 1) == 0
    assert agent._history.entry_count("active-a", 1) == 1


async def test_current_state_is_after_history_and_uses_latest_board(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}, {"type": "tool", "move": "K9"}])
    _, agent, _manager, room = await _setup(tmp_path, router, _cfg())

    await agent.run(room, 1)
    await room.apply(0, {"type": "move", "move": "K8"}, client_version=room.match.version)
    await agent.run(room, 1)

    second = router.calls[-1]
    history = _history_messages(second)
    current_index = _current_user_index(second)
    assert current_index == len(history)
    current = _current_user(second)["content"]
    # 最新人类落子出现在当前局面；旧历史里没有棋盘文本。
    assert "K8" in current
    assert "Last move: K8" in current
    assert "Gomoku" not in json.dumps(history, ensure_ascii=False)


async def test_non_gomoku_three_seat_non_coordinate_module(
    tmp_path, monkeypatch
) -> None:
    """非五子棋 3 座位/非坐标动作：tool/system 无棋种假设，loop/历史照常。"""
    module = _TrioModule()
    monkeypatch.setitem(games._MODULES, module.kind, module)
    router = FakeRouter(
        [{"type": "tool", "move": "blue"}, {"type": "tool", "move": "blue"}]
    )
    plugin = FakePlugin(router, _cfg(), tmp_path)
    agent = GameAgent(plugin)
    room = await _trio_ai_turn_room()
    await room.set_plan(1, text="测试计划", ttl_moves=10, ttl_s=120.0)

    await agent.run(room, 1)
    assert room.match.state["scores"] == [1, 1, 0]

    # 推进到同一 AI 的下一次决策，确保最新 user 在 history 之后。
    await room.apply(2, {"type": "move", "move": "red"}, client_version=room.match.version)
    await room.apply(0, {"type": "move", "move": "red"}, client_version=room.match.version)
    await agent.run(room, 1)
    assert room.match.state["scores"] == [2, 2, 1]

    call = router.calls[-1]
    tool_dump = json.dumps(call["tools"], ensure_ascii=False)
    assert module.move_help in tool_dump
    for token in ("15x15", "GTP", "A-P", "X (black)", "O (white)", "黑", "白"):
        assert token not in tool_dump

    system = call["system_prompt"]
    for token in ("15x15", "GTP", "A-P", "X", "O", "黑", "白", "坐标"):
        assert token not in system

    # 历史确实在最新 user 之前；媒体输入恒为 None，也不进 history。
    history = _history_messages(call)
    assert history
    current_index = _current_user_index(call)
    assert current_index == len(history)
    latest = _current_user_raw(call)
    assert isinstance(latest["content"], str)
    assert "Game trio-match" in latest["content"]
    assert "image_url" not in json.dumps(history, ensure_ascii=False)

    # 基座 user 仍是纯文本稳定初始化；media 参数必须为空。
    assert isinstance(call["user_prompt"], str)
    assert call["media"] is None

    current = _current_user(call)["content"]
    assert "测试计划" in current
    # Move format is declared by the play tool schema; the stable prompt only
    # references it and the dynamic snapshot never repeats it.
    assert module.move_help not in current
    assert module.move_help not in call["user_prompt"]
    assert module.move_help in tool_dump
    assert "Threat analysis" not in current  # 模块无 threat_summary 时正常跳过

    entries = _history_entry(agent, room.match.id, 1)
    assert len(entries) == 4  # 两手，各自 assistant/tool 配对
    assert all(
        call["function"]["name"] == "play"
        for message in entries
        if message["role"] == "assistant"
        for call in (message.get("tool_calls") or [])
    )
    _assert_pairs_closed(entries)


async def test_module_media_hook_is_disabled(tmp_path, monkeypatch) -> None:
    """视觉扩展点下线：即使模块提供 agent_media 也不读、不发图。"""
    module = _TrioModule()
    monkeypatch.setitem(games._MODULES, module.kind, module)
    router = FakeRouter([{"type": "tool", "move": "blue"}])
    plugin = FakePlugin(router, _cfg(), tmp_path)
    agent = GameAgent(plugin)
    room = await _trio_ai_turn_room()

    assert await agent._module_media(room.match, 1) is None
    await agent.run(room, 1)

    assert room.match.log[-1].move == "blue"
    assert router.calls[0]["media"] is None
    latest = _current_user_raw(router.calls[0])
    assert isinstance(latest["content"], str)


# ----------------------------------------------------------------------
#  日志失败不影响游戏 / 工具面仍是 4 个
# ----------------------------------------------------------------------
async def test_decision_log_failure_does_not_break_game(tmp_path) -> None:
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    bad_dir = tmp_path / "not-a-dir"
    bad_dir.write_text("file", encoding="utf-8")
    _, agent, _manager, room = await _setup(bad_dir, router, _cfg())

    await agent.run(room, 1)

    assert room.match.state["moves"] == 2


def test_host_tool_surface_four_without_play_or_plan(tmp_path) -> None:
    from tools import game_list, game_new, game_resign, game_state

    assert all(callable(fn) for fn in (game_list, game_new, game_state, game_resign))
    assert "play" not in {"game_list", "game_new", "game_state", "game_resign"}
    assert "game_move" not in {"game_list", "game_new", "game_state", "game_resign"}
    # game_plan 已下线：新计划行为延期，绝不把策略写进队友局的对手座位。
    assert "game_plan" not in {"game_list", "game_new", "game_state", "game_resign"}


def test_default_play_tool_definition_is_generic() -> None:
    function = PLAY_TOOL_DEF["function"]
    assert function["name"] == "play"
    description = (
        str(function["description"])
        + str(function["parameters"]["properties"]["move"]["description"])
    )
    for token in ("15x15", "GTP", "A-P", "H8", "X", "O", "黑", "白", "坐标"):
        assert token not in description
    assert "动作" in description or "move" in description


def test_play_tool_definition_uses_module_move_help() -> None:
    gomoku = games.get("gomoku")
    tool = build_play_tool_def(gomoku)
    function = tool["function"]
    assert gomoku.move_help in str(function["description"])
    assert gomoku.move_help in str(function["parameters"]["properties"]["move"]["description"])


# ----------------------------------------------------------------------
#  host.llm 适配层：流式聚合 / chat 回退 / 模型 failover / 工具 schema
# ----------------------------------------------------------------------
def _chunk(
    *,
    text: str = "",
    reasoning: str = "",
    calls: list[dict[str, Any]] | None = None,
    final: bool = False,
    finish: str = "",
) -> Any:
    return SimpleNamespace(
        delta_text=text,
        delta_reasoning=reasoning,
        tool_calls_delta=list(calls or []),
        is_final=final,
        finish_reason=finish,
    )


class _FakeLLMClient:
    """Minimal KiraAI LLM client: records requests, streams scripted chunks."""

    def __init__(
        self,
        chunks: list[Any] | None = None,
        *,
        stream_error: BaseException | None = None,
        response: Any = None,
        chat_error: BaseException | None = None,
    ) -> None:
        self.chunks = list(chunks or [])
        self.stream_error = stream_error
        self.response = response
        self.chat_error = chat_error
        self.requests: list[Any] = []

    async def chat_stream(self, request):
        self.requests.append(request)
        if self.stream_error is not None:
            raise self.stream_error
        for chunk in self.chunks:
            yield chunk

    async def chat(self, request):
        self.requests.append(request)
        if self.chat_error is not None:
            raise self.chat_error
        return self.response


def _fake_ctx(clients: dict[str, Any], default: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_llm_client=lambda model: clients.get(model),
        get_default_llm_client=lambda: default,
    )


async def _collect(agen) -> list[dict[str, Any]]:
    return [event async for event in agen]


async def test_host_llm_stream_aggregates_text_reasoning_and_tool_calls() -> None:
    client = _FakeLLMClient(
        [
            _chunk(reasoning="先算", text="我要下"),
            _chunk(text="中路", final=True, finish="tool_calls"),
            _chunk(
                calls=[
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "play", "arguments": '{"mo'},
                    }
                ]
            ),
            _chunk(
                calls=[
                    {
                        "index": 0,
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": 've": "J9"}'},
                    }
                ]
            ),
        ]
    )
    plugin = SimpleNamespace(ctx=_fake_ctx({"provider:m": client}))
    play_def = build_play_tool_def(games.get("gomoku"))

    events = await _collect(
        _REAL_RUN(
            plugin,
            system_prompt="SYS",
            user_prompt="INIT",
            tools=[play_def],
            extra_messages=[{"role": "user", "content": "BOARD"}],
            model_list=["provider:m"],
        )
    )

    assert [e["type"] for e in events] == [
        "text", "reasoning", "text", "tool_calls",
    ]
    assert events[0]["delta"] == "我要下"
    assert events[1]["delta"] == "先算"
    final = events[-1]
    assert final["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "play", "arguments": '{"move": "J9"}'},
        }
    ]
    assert final["reasoning_content"] == "先算"

    request = client.requests[0]
    assert [m.role for m in request.messages] == ["system", "user", "user"]
    assert request.messages[0].content == "SYS"
    assert request.messages[1].content == "INIT"
    assert request.messages[2].content == "BOARD"
    # 工具 schema 被包成 BaseTool 进入 tool_set，参数面与 nori 完全一致。
    tools = request.tool_set.to_list()
    assert [tool["function"]["name"] for tool in tools] == ["play"]
    assert set(tools[0]["function"]["parameters"]["properties"]) == {"move"}
    assert request.tool_choice == "auto"
    # 这些是 schema-only 包装：从不由宿主 agent loop 执行。
    with pytest.raises(RuntimeError):
        await request.tool_set.tools[0].execute()


async def test_host_llm_falls_back_to_chat_when_stream_fails() -> None:
    response = SimpleNamespace(
        text_response="我下这里",
        reasoning_content="",
        tool_calls=[
            {
                "id": "call-chat",
                "type": "function",
                "function": {"name": "play", "arguments": '{"move": "J9"}'},
            }
        ],
    )
    client = _FakeLLMClient(stream_error=TypeError("stream unsupported"), response=response)
    plugin = SimpleNamespace(ctx=_fake_ctx({"p:m": client}))

    events = await _collect(
        _REAL_RUN(
            plugin,
            system_prompt="SYS",
            user_prompt="INIT",
            tools=[build_play_tool_def(None)],
            model_list=["p:m"],
        )
    )

    assert events[0] == {"type": "text", "delta": "我下这里"}
    assert events[-1]["type"] == "tool_calls"
    assert events[-1]["tool_calls"][0]["id"] == "call-chat"
    assert len(client.requests) == 2  # 一次流式失败 + 一次 chat 回退


async def test_host_llm_ordered_failover_over_models() -> None:
    bad = _FakeLLMClient(stream_error=RuntimeError("bad model"), chat_error=RuntimeError("bad"))
    good = _FakeLLMClient(
        [_chunk(text="ok", final=True, finish="stop")],
    )
    plugin = SimpleNamespace(ctx=_fake_ctx({"p:bad": bad, "p:good": good}))

    events = await _collect(
        _REAL_RUN(
            plugin,
            system_prompt="SYS",
            user_prompt="INIT",
            model_list=["p:bad", "p:good"],
        )
    )

    assert events[0] == {"type": "text", "delta": "ok"}
    assert events[-1]["tool_calls"] == []
    assert len(bad.requests) == 2  # 流的失败 + 同模型 chat 回退
    assert len(good.requests) == 1
    # 没有工具时 tool_choice=none，避免向提供方要求必选工具。
    assert good.requests[0].tool_choice == "none"


async def test_host_llm_uses_default_client_when_model_list_empty() -> None:
    default = _FakeLLMClient([_chunk(text="hi", final=True, finish="stop")])
    plugin = SimpleNamespace(ctx=_fake_ctx({}, default=default))

    events = await _collect(
        _REAL_RUN(plugin, system_prompt="SYS", user_prompt="INIT")
    )

    assert events == [
        {"type": "text", "delta": "hi"},
        {
            "type": "tool_calls",
            "tool_calls": [],
            "reasoning_content": "",
            "finish_reason": "stop",
        },
    ]
    assert len(default.requests) == 1


async def test_host_llm_raises_last_error_when_all_models_fail() -> None:
    bad = _FakeLLMClient(stream_error=RuntimeError("boom"), chat_error=RuntimeError("boom2"))
    plugin = SimpleNamespace(ctx=_fake_ctx({"p:only": bad}))

    with pytest.raises(RuntimeError):
        await _collect(
            _REAL_RUN(
                plugin,
                system_prompt="SYS",
                user_prompt="INIT",
                model_list=["p:only"],
            )
        )


async def test_host_llm_skips_unknown_models_and_ignores_media() -> None:
    good = _FakeLLMClient([_chunk(text="x", final=True, finish="stop")])
    plugin = SimpleNamespace(ctx=_fake_ctx({"p:good": good}))

    events = await _collect(
        _REAL_RUN(
            plugin,
            system_prompt="SYS",
            user_prompt="INIT",
            model_list=["p:missing", "p:good"],
            media=SimpleNamespace(images_base64=["abc"]),
            stream=True,
        )
    )

    assert events[0] == {"type": "text", "delta": "x"}
    assert len(good.requests) == 1


# ----------------------------------------------------------------------
#  2026-11-16 remediation regressions.
# ----------------------------------------------------------------------
async def test_broken_ai_timeout_falls_back_to_a_default_budget(tmp_path) -> None:
    """P2: ai_timeout=null or a broken value must not crash the run() entry."""
    router = FakeRouter([{"type": "tool", "move": "J9"}])
    plugin, agent, _manager, room = await _setup(
        tmp_path, router, _cfg(ai_timeout=None)
    )
    before = room.match.state["moves"]
    await agent.run(room, 1)  # must not raise
    assert room.match.state["moves"] == before + 1  # the move still applied


def test_decision_log_rotates_and_stays_serialized(tmp_path) -> None:
    """P2: decision_log.jsonl rotates once and writes are serialized."""
    import game_agent as module

    path = Path(str(tmp_path)) / "decision_log.jsonl"
    old_cap = module._JSONL_MAX_BYTES
    try:
        module._JSONL_MAX_BYTES = 300
        for index in range(10):
            module._append_line(path, json.dumps({"n": index}) * 15 + "\n")
    finally:
        module._JSONL_MAX_BYTES = old_cap
    assert (tmp_path / "decision_log.jsonl.1").exists()
