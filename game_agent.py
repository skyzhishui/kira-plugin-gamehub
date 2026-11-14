"""game_agent：插件内的 LLM 对局决策者（自持循环 + 原生 transcript）。

移植自 nori ``nori_plugin_gamehub/game_agent.py``，决策循环语义原样保留，
只把宿主适配层（原 ``ctx.task_router.run_replyer_stream``）换成
``host.llm.run_replyer_stream``。

架构口径（棋牌/解谜/视觉通用，不写死五子棋）：

- 决策循环由插件自持，直接消费 ``host.llm.run_replyer_stream``；
  工具面每轮恒为内部 ``play(move)``，**末轮也不收走**，也不注入 final
  hint——第 N 轮仍可合法执行动作/纠错。``move`` 是字符串动作协议，格式由
  游戏模块的 ``move_help`` 决定，不假设坐标。模型被要求在同一次回复里
  附带一两句保持人设的玩家短话（与工具调用同行，不是独立参数）；该轮正文
  只在**合法落子成功**后作为内部 ``say`` 传输写入 ``Match.chat``（对手
  气泡），绝不发会话 notice、绝不把纯文字轮/失败轮/过期轮正文落库。
- 模型输入顺序固定为：
  ``[system(稳定), user(稳定初始化/说明), 跨手历史 native assistant/tool,
  user(最新局面), 本手 transcript...]``。最新 user 消息由
  ``agent_view_text`` 统一输出 rules → 当前 board/chat → plan →
  threat_summary。
- 跨手历史按 ``(match_id, seat)`` 隔离，默认最近 10 个完成决策回合
  （``ai_history_rounds``：-1 不限、0 关闭）。历史是内存态，重启清空，
  不进入宿主聊天历史；只存游戏动作与模型/工具短说明。
- 每轮工具调用数由 ``ai_agent_calls_per_round``（默认 1）限制；截断时
  assistant.tool_calls 只声明真正执行的子集，每个声明都有配对的
  ``tool.tool_call_id/content``，绝不悬挂。
- 动作成功立即结束，但历史写入前一定先补齐 assistant/tool 配对。
- 超时/模型 failover/并发/取消/迟到防护/最终 bot 兜底沿用原有口径：
  所有模型共享 ``ai_timeout``；只有仍有效的当前回合才允许 bot 补手；
  取消/停机绝不补手，迟到提交绝不跨手。
- 身份不写死：``agent_name`` / ``agent_persona`` 来自插件配置
  （默认 nori / 调皮爱吐槽的猫娘）。``ai_models`` 里的名字是模型清单
  别名，绝不当人设名。
- 视觉扩展点已下线：``_module_media`` 恒返回 ``None``（本宿主对局无图片
  输入），但仍保留该钩子位置，便于将来接回。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # production: plugin package (``plugins.<plugin>.game_agent``)
    from . import modes
    from .agent_history import AgentHistory
    from .engine import games
    from .engine.match import agent_view_text
    from .engine.types import GameModule
    from .host import llm as host_llm
    from .plan import normalize_plan_text, plan_remaining
except ImportError:  # standalone tests import modules as top-level
    import modes
    from agent_history import AgentHistory
    from engine import games
    from engine.match import agent_view_text
    from engine.types import GameModule
    from plan import normalize_plan_text, plan_remaining

    try:
        from host import llm as host_llm
    except ImportError:  # pragma: no cover - host adapter absent in tests
        host_llm = None  # type: ignore[assignment]

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

# 配置缺省（settings.py 声明；这里用 getattr 兼容运行）。
DEFAULT_ROUNDS = 5
DEFAULT_CALLS_PER_ROUND = 1
DEFAULT_HISTORY_ROUNDS = 10
DEFAULT_AGENT_NAME = "nori"
DEFAULT_AGENT_PERSONA = "调皮爱吐槽的猫娘，玩游戏时会认真+毒舌"


def build_play_tool_def(module: Any = None) -> dict[str, object]:
    """通用 play 工具定义：只有必填 ``move``，不暴露任何台词参数。

    ``move`` 是字符串动作，格式由游戏模块 move_help 决定；不假设坐标/
    棋盘/执色。定义描述只说明动作与可选同轮文本输出，不携带人设/对手/
    行为引导（这些由调用方的 system/notice 承载）。表达不通过工具参数
    传递：内部只在合法落子成功时把该轮正文作为传输层 ``say`` 写入对手
    气泡。说话失败不得触发重试落子。
    """
    move_help = str(getattr(module, "move_help", "") or "").strip()
    if move_help:
        tool_description = (
            "在当前对局执行一步。move 的格式由本局规则决定"
            f"（{move_help}），可配合文本输出一起使用。"
        )
        move_description = f"游戏动作字符串；本局格式：{move_help}。"
    else:
        tool_description = (
            "在当前对局执行一步。move 的格式由本局规则决定，可配合文本输出一起使用。"
        )
        move_description = "游戏动作字符串；具体格式以本局规则/move_help 为准。"
    return {
        "type": "function",
        "function": {
            "name": "play",
            "description": tool_description,
            "parameters": {
                "type": "object",
                "properties": {
                    "move": {
                        "type": "string",
                        "description": move_description,
                    },
                },
                "required": ["move"],
                "additionalProperties": False,
            },
        },
    }


# 兼容导出：无模块上下文时的通用定义；实际调用按当前游戏模块动态生成。
PLAY_TOOL_DEF: dict[str, object] = build_play_tool_def(None)

# 纯文本轮提醒：本轮计一轮，提示后继续到预算耗尽（末轮不再提醒）。
TEXT_ONLY_REMINDER = (
    "你没有调用 play 工具执行本局动作；只回复文字不会推进对局。"
    "请在同一次回复里调用 play，短话可以和工具调用写在一起。"
)

# 这些错误表示「本手已经不属于当前回合」，继续纠错/换模型/补 bot 都无意义。
_STALE_ERROR_MARKERS = (
    "局面已经变化",
    "现在还没轮到你",
    "这手已经过期",
    "对局已经结束",
    "没有这个座位",
)


def _is_stale_error(error: str) -> bool:
    text = str(error or "")
    if any(marker in text for marker in _STALE_ERROR_MARKERS):
        return True
    # 座位归属错误（不同模式文案可能引用不同显示名，不写死具体人设名）。
    return "这不是" in text and "座位" in text


@dataclass
class _RunState:
    """单个模型一次完整循环的结果（局部可变状态，不跨局共享）。"""

    model: str = ""
    applied: bool = False
    applied_move: str = ""
    illegal: int = 0
    executed: int = 0
    rounds: int = 0
    stale: bool = False
    reason: str = "no_tool_call"


@dataclass
class _Snapshot:
    """LLM 调用前的锁内快照（锁外只读使用）。"""

    match: Any
    active: bool
    move_count: int
    version: int
    plan: dict[str, Any] | None = None
    plan_text: str = ""
    plan_seq: int = 0
    plan_review: bool = False
    board_hash: str = ""
    seat_label: str = ""
    seat: int = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _board_hash(state: Any) -> str:
    try:
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = repr(state)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _active_plan(match: Any, seat: int) -> tuple[dict[str, Any] | None, str, int, bool]:
    """读取 ``seat.plan`` 并只返回**未过期**的文本（TTL 纯函数复用 plan.py）。"""
    plan = None
    seats = getattr(match, "seats", ()) or ()
    if 0 <= seat < len(seats):
        candidate = getattr(seats[seat], "plan", None)
        if isinstance(candidate, dict):
            plan = candidate
    if plan is None:
        return None, "", 0, False
    text = normalize_plan_text(plan.get("text"))
    if not text:
        return plan, "", 0, False
    try:
        tempo = str(games.get(match.kind).tempo or "turn")
    except KeyError:
        tempo = "turn"
    ttl = plan_remaining(
        plan,
        tempo=tempo,
        move_count=len(getattr(match, "log", ()) or ()),
        now=_now_ms(),
    )
    if not ttl["active"]:
        return plan, "", 0, False
    try:
        seq = int(plan.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return plan, text, seq, bool(plan.get("review"))


def _seat_labels(match: Any) -> list[str]:
    try:
        return list(games.get(match.kind).seat_labels(match.state) or [])
    except KeyError:
        return []


def _seat_name(match: Any, seat: int) -> str:
    seats = getattr(match, "seats", ()) or ()
    if 0 <= seat < len(seats):
        return str(getattr(seats[seat], "name", "") or "").strip()
    return ""


def _settings_of(plugin: Any) -> Any:
    return getattr(plugin, "settings", None)


def _configured_identity(plugin: Any) -> tuple[str, str]:
    """插件配置里的 AI 身份（KiraAI：``agent_name`` / ``agent_persona``）。"""
    settings = _settings_of(plugin)
    name = str(getattr(settings, "agent_name", "") or "").strip()
    persona = str(getattr(settings, "agent_persona", "") or "").strip()
    return name, persona


def _bot_identity(plugin: Any) -> tuple[str, str]:
    """宿主 bot（=插件 AI）的原始身份：昵称 + 人格，不写死具体人设名。"""
    return _configured_identity(plugin)


def _agent_identity(plugin: Any) -> tuple[str, str]:
    """当前选中 agent 执行器的配置身份（NAME + PERSONALITY）。

    KiraAI 没有 ``ctx.subagent_runner``：身份统一来自插件配置
    ``agent_name`` / ``agent_persona``。缺席时退回通用 ``"agent"`` 且不带
    人格；**不读 ``ai_models``**（那是模型清单别名，不是 agent 身份）。
    """
    name, persona = _configured_identity(plugin)
    return name or "agent", persona


def _seat_identity(plugin: Any, match: Any, seat: int) -> tuple[str, str]:
    """本局该 AI 座位的说话身份：优先配置身份，座位名兜底。

    - 对手局（诺里本人座位）：用插件配置的昵称/人格，绝不改写原有人设；
    - 队友局 advanced 对手座位：同样用配置身份；未配置时用通用
      ``"agent"``，不写给具体人设名；
    - 配置缺席时用座位落库显示名，再退中性「玩家」。
    """
    try:
        role = modes.role_of(match)
    except Exception:
        role = modes.ROLE_OPPONENT
    if role == modes.ROLE_OPPONENT and not (
        getattr(match, "kind", "") == "doudizhu" and modes.seat_driver_of(match, seat) == "agent"
    ):
        bot_name, persona = _bot_identity(plugin)
        return bot_name or _seat_name(match, seat) or "玩家", persona
    agent_name, persona = _agent_identity(plugin)
    return agent_name, persona


def _parse_tool_args(raw: object) -> dict[str, Any] | None:
    """严格解析 tool_call.arguments（空/非对象/坏 JSON 返回 None）。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_utterance(value: Any, limit: int = 280) -> str:
    """把一轮正文清洗成可落库的短对白：折叠空白、截断；空/非字符串为空。

    只处理模型普通正文（reasoning/契约 reasoning 不在 ``round_text`` 里），
    因此不会把思考内容写进对手气泡。
    """
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[: max(1, int(limit))]


def _history_rounds(cfg: Any) -> int:
    try:
        return int(getattr(cfg, "ai_history_rounds", DEFAULT_HISTORY_ROUNDS))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_ROUNDS


def _normalize_tool_calls(
    raw: Any, round_idx: int
) -> list[dict[str, Any]]:
    """把流末 tool_calls 归一为 OpenAI 标准声明，保留完整 id/args。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            function = {}
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        if arguments is None:
            arguments = ""
        elif not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = str(arguments)
        call_id = str(item.get("id") or "") or f"call_{round_idx}_{index}"
        if not name:
            # 空工具名也归一为合法占位名，执行层会给出可读的未知工具错误，
            # 同时避免把空 name 的 assistant 消息回传给提供方。
            name = "unknown_tool"
        out.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": str(arguments)},
            }
        )
    return out


def _clip_tool_calls(
    tool_calls: list[dict[str, Any]], per_round_limit: int
) -> list[dict[str, Any]]:
    """单轮调用上限：只声明前 N 个（被截断的调用不声明、不产生悬挂）。"""
    limit = max(1, int(per_round_limit or 1))
    if len(tool_calls) <= limit:
        return tool_calls
    logger.warning(
        "game_agent：单轮工具调用 %d 个超出上限 %d，截断保留前 %d 个",
        len(tool_calls),
        limit,
        limit,
    )
    return tool_calls[:limit]


class GameAgent:
    """单个插件实例共享的走子 agent（含并发信号量与跨手内存历史）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        cfg = plugin.settings
        self._semaphore = asyncio.Semaphore(
            max(1, int(getattr(cfg, "ai_max_concurrency", 2) or 1))
        )
        self._history = AgentHistory()

    # ------------------------------------------------------------------
    #  对外入口（host.runtime._drive_ai 调 ``play``）
    # ------------------------------------------------------------------
    async def play(self, room: Any, seat: int) -> None:
        """KiraAI 入口名：等价于 nori 的 ``run``。"""
        await self.run(room, seat)

    async def run(self, room: Any, seat: int) -> None:
        if getattr(self._plugin, "stopping", False):
            return
        # 资源生命周期：GameAgent 是插件级共享实例，历史按局/seat 隔离；
        # 每次决策入口回收已不在 active_rooms 的对局历史，避免终局局
        # 长期堆积。当前局显式保留（刚结束的成功手可留到下一次清理）。
        self._prune_inactive_history(room)
        cfg = self._plugin.settings
        started = time.monotonic()
        deadline = started + float(cfg.ai_timeout)
        snap = await self._snapshot(room, seat)
        if not snap.active:
            return

        models = [
            str(name).strip()
            for name in (getattr(cfg, "ai_models", None) or [])
            if str(name).strip()
        ]
        if getattr(cfg, "ai_mode", "agent") == "bot":
            await self._fallback(room, seat, snap, "mode_bot", started)
            return
        if not models:
            await self._fallback(room, seat, snap, "no_model", started)
            return

        acquired = False
        retries = 0
        last_reason = "no_model"
        used_model = models[0]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
            acquired = True
            try:
                for model in models:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        last_reason = "timeout"
                        break
                    used_model = model
                    try:
                        outcome = await asyncio.wait_for(
                            self._run_model(room, seat, snap, model, started),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        last_reason = "timeout"
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "game_agent 模型 %s 决策异常（继续 failover）: %s",
                            model,
                            room.match.id,
                            exc_info=True,
                        )
                        last_reason = "error"
                        continue
                    retries += outcome.illegal
                    if outcome.applied:
                        await self._log(
                            snap,
                            source="agent",
                            move=outcome.applied_move,
                            model=model,
                            fallback="",
                            retries=retries,
                            started=started,
                        )
                        return
                    if outcome.stale:
                        # 当前回合已消失：不 failover、不 bot、不跨手提交。
                        await self._log(
                            snap,
                            source="none",
                            move="",
                            model=model,
                            fallback="stale",
                            retries=retries,
                            started=started,
                        )
                        return
                    last_reason = outcome.reason
            finally:
                if acquired:
                    self._semaphore.release()
            await self._fallback(
                room,
                seat,
                snap,
                last_reason,
                started,
                retries=retries,
                model=used_model,
            )
        except asyncio.TimeoutError:
            # 信号量排队已吃掉全部总预算：仍有效的当前回合用 bot 兜底。
            await self._fallback(
                room,
                seat,
                snap,
                "timeout",
                started,
                retries=retries,
                model=used_model,
            )
        except asyncio.CancelledError:
            # 外部取消/停机：明确记一条取消日志，但绝不 bot 补手。
            await self._log_cancel(snap, started)
            raise

    # ------------------------------------------------------------------
    #  单模型循环
    # ------------------------------------------------------------------
    async def _run_model(
        self,
        room: Any,
        seat: int,
        snap: _Snapshot,
        model: str,
        started: float,
    ) -> _RunState:
        state = _RunState(model=model)
        if host_llm is None or getattr(self._plugin, "ctx", None) is None:
            state.reason = "no_model"
            return state

        cfg = self._plugin.settings
        try:
            rounds = max(1, int(getattr(cfg, "ai_agent_rounds", DEFAULT_ROUNDS)))
        except (TypeError, ValueError):
            rounds = DEFAULT_ROUNDS
        try:
            per_round = max(
                1, int(getattr(cfg, "ai_agent_calls_per_round", DEFAULT_CALLS_PER_ROUND))
            )
        except (TypeError, ValueError):
            per_round = DEFAULT_CALLS_PER_ROUND
        history_rounds = _history_rounds(cfg)
        # 正值窗口在存储层也按 entry 原子裁剪（-1 不限、0 清空），
        # 热更缩小 ai_history_rounds 后下一次决策即回收旧历史。
        self._history.trim(snap.match.id, seat, history_rounds)

        system_prompt = self._system_prompt(snap.match, seat)
        init_prompt = self._init_prompt(snap.match, seat)
        play_def = build_play_tool_def(games.get(snap.match.kind))
        # 每个模型从同一份跨手历史起步；失败模型的回合内 transcript 不落历史。
        history_messages = self._history.window(snap.match.id, seat, history_rounds)
        transcript: list[dict[str, Any]] = []

        for round_idx in range(rounds):
            if getattr(self._plugin, "stopping", False):
                break
            # 当前局面每轮重新读取：局面若已经跨回合变化，直接判定本手过期。
            current_view, current_match, fresh = await self._current_view(
                room, seat, snap.move_count
            )
            if not fresh:
                state.stale = True
                state.reason = "stale"
                return state
            # 可选视觉扩展点：本宿主对局无图片输入，恒为 None。
            media = await self._module_media(current_match, seat)
            latest_user = await self._latest_user_message(current_view, media)
            extra_messages = history_messages + [latest_user] + transcript

            round_text: list[str] = []
            tool_calls_raw: Any = None
            contract_reasoning = ""
            try:
                async for event in host_llm.run_replyer_stream(
                    self._plugin,
                    system_prompt=system_prompt,
                    user_prompt=init_prompt,
                    media=None,
                    tools=[play_def],
                    extra_messages=extra_messages,
                    stream=True,
                    model_list=[model],
                ):
                    if isinstance(event, str):
                        if event:
                            round_text.append(event)
                    elif isinstance(event, dict):
                        kind = event.get("type")
                        if kind == "text":
                            delta = str(event.get("delta") or "")
                            if delta:
                                round_text.append(delta)
                        elif kind == "reasoning":
                            # 观察通道：不回放、不进 transcript、不当普通文本。
                            continue
                        elif kind == "tool_calls":
                            tool_calls_raw = event.get("tool_calls")
                            # 唯一回传来源：流末契约键（门控关闭时不存在）。
                            contract_reasoning = str(
                                event.get("reasoning_content") or ""
                            )
            finally:
                state.rounds += 1

            full_text = "".join(round_text)
            normalized = _normalize_tool_calls(tool_calls_raw, round_idx)
            calls = _clip_tool_calls(normalized, per_round)

            if not calls:
                # 纯文本轮也计一轮：留短判断文本，提醒后继续（末轮不再提醒）。
                if full_text:
                    transcript.append({"role": "assistant", "content": full_text})
                if round_idx < rounds - 1:
                    transcript.append({"role": "user", "content": TEXT_ONLY_REMINDER})
                    continue
                break

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": full_text or None,
                "tool_calls": calls,
            }
            if contract_reasoning:
                assistant_message["reasoning_content"] = contract_reasoning
            transcript.append(assistant_message)

            stale = await self._execute_tool_calls(
                room=room,
                seat=seat,
                snap=snap,
                calls=calls,
                transcript=transcript,
                state=state,
                started=started,
                round_text=full_text,
            )
            if stale:
                state.stale = True
                state.reason = "stale"
                return state
            if state.applied:
                break

        if state.applied:
            # 成功：transcript 已完整配对；写入跨手历史后立即结束。
            self._record_history(snap.match.id, seat, transcript, history_rounds)
        elif state.illegal:
            state.reason = "illegal"
        elif state.executed == 0:
            state.reason = "no_tool_call"
        else:
            state.reason = "no_move"
        return state

    async def _execute_tool_calls(
        self,
        *,
        room: Any,
        seat: int,
        snap: _Snapshot,
        calls: list[dict[str, Any]],
        transcript: list[dict[str, Any]],
        state: _RunState,
        started: float,
        round_text: str = "",
    ) -> bool:
        """执行本轮声明的调用并补齐 tool result；返回是否已过期。

        ``round_text`` 是本轮普通正文（reasoning 已排除）。它只在**合法
        落子成功**时作为内部 ``say`` 传输写入 ``Match.chat``：房间层保证
        非法/迟到/纯文字轮绝不落库；``max_say_len`` 截断，失败只丢台词，
        绝不把已成功落子改成失败或触发重试。
        """
        for call in calls:
            tool_call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            args_raw = function.get("arguments")

            if state.stale:
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": "本手已经过期，本次调用未执行。",
                    }
                )
                continue
            if state.applied:
                # 同一轮多个声明：成功后其余调用不执行，但必须配对收尾。
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": "本回合已经执行动作，本次调用未执行。",
                    }
                )
                continue

            state.executed += 1
            args = _parse_tool_args(args_raw)
            if args is None:
                result = (
                    f"工具 {name or '(未命名)'} 的参数为空或无法解析（可能被截断），"
                    "请重发完整的调用参数。"
                )
            elif name != "play":
                result = f"错误：未知工具 {name or '(未命名)'}。本轮只有 play 可用。"
            else:
                move = str(args.get("move") or "").strip()
                if not move:
                    state.illegal += 1
                    result = (
                        "缺少 move 参数。请调用 play 并提供本局规则要求的"
                        "动作字符串。"
                    )
                else:
                    if getattr(self._plugin, "stopping", False):
                        raise asyncio.CancelledError
                    # 最小思考时间在**提交前**补足：已经想够时不再多睡。
                    await self._wait_min_think(started)
                    if getattr(self._plugin, "stopping", False):
                        # 停机可能发生在最小思考等待期间：醒来后必须再检查一次，
                        # 不能在停机后提交动作。
                        raise asyncio.CancelledError
                    # 模型已不再有 say 参数：表达来自本轮普通正文。只有本轮
                    # 合法落子成功才把它作为内部传输 say 交给房间层原子落库
                    # （对手气泡）；非法/迟到时房间层绝不追加聊天。
                    utterance = _clean_utterance(
                        round_text,
                        int(
                            getattr(
                                getattr(self._plugin, "settings", None),
                                "max_say_len",
                                280,
                            )
                            or 280
                        ),
                    )
                    match, error = await room.apply_ai_move(
                        seat,
                        move,
                        expected_move_count=snap.move_count,
                        say=utterance or None,
                        persist_say=True,
                    )
                    if match is None:
                        # 错误 tool result 只写动作/错误，不附带旧局面或旧分析。
                        if _is_stale_error(error):
                            state.stale = True
                            transcript.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call_id,
                                    "content": f"动作 {move} 失败：{error}。",
                                }
                            )
                            continue
                        state.illegal += 1
                        result = (
                            f"动作 {move} 失败：{error}。"
                            "不要重复同样的非法动作。"
                        )
                    else:
                        state.applied = True
                        state.applied_move = match.log[-1].move if match.log else move
                        result = f"已执行动作 {state.applied_move}。"

            transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                }
            )
        return state.stale

    def _record_history(
        self,
        match_id: str,
        seat: int,
        transcript: list[dict[str, Any]],
        history_rounds: int,
    ) -> None:
        if history_rounds == 0:
            return
        try:
            self._history.append(match_id, seat, transcript)
            self._history.trim(match_id, seat, history_rounds)
        except Exception:
            logger.warning("game_agent 历史写入失败: %s seat %s", match_id, seat, exc_info=True)

    def _prune_inactive_history(self, room: Any) -> None:
        """回收不在 active_rooms 的历史；拿不到管理器时静默跳过。

        ``plugin.rooms`` 在插件 setup 前读取会抛 RuntimeError，测试假插件
        也可能没有该属性——都按「无可回收信息」处理，不阻断走子。
        """
        try:
            manager = getattr(self._plugin, "rooms", None)
        except Exception:
            return
        active_rooms = getattr(manager, "active_rooms", None)
        if not callable(active_rooms):
            return
        try:
            keep = {str(item.match.id) for item in (active_rooms() or ())}
        except Exception:
            return
        current_id = str(getattr(getattr(room, "match", None), "id", "") or "")
        if current_id:
            keep.add(current_id)
        self._history.retain_matches(keep)

    async def _current_view(
        self, room: Any, seat: int, expected_move_count: int
    ) -> tuple[str, Any, bool]:
        """重新读取当前局面并构造最新 user 消息（永远放在跨手历史之后）。"""
        latest = await room.ai_snapshot(seat)
        if not latest.get("active") or int(latest.get("move_count", -1)) != int(
            expected_move_count
        ):
            return "", None, False
        match = latest["match"]
        if modes.role_of(match) == modes.ROLE_TEAMMATE:
            # 队友局：座位上的 plan 属于「对手意图」，game_agent 决策绝不
            # 读取队友 plan/队友上下文——plan_text 强制置空。
            plan_text, review = "", False
        else:
            _plan, plan_text, _seq, review = _active_plan(match, seat)
        # 单一集成点：agent_view_text 已把 rules / 当前 board+chat /
        # plan / threat_summary 按固定顺序排在当前 user 消息里；跨手历史
        # 不经过该 builder，因此不会回放旧局面/旧 plan/旧威胁分析。
        view = agent_view_text(
            match,
            seat,
            plan_text=plan_text,
            plan_review=review,
            chat_limit=3,
            threat_analysis=True,
        )
        return view, match, True

    async def _module_media(self, match: Any, seat: int) -> Any | None:
        """可选视觉扩展点：本宿主对局无图片输入，恒返回 ``None``。

        保留钩子位置（原 nori 会调用模块 ``agent_media``），将来接回图片
        输入时只需在这里返回 media 对象并让 ``host.llm`` 支持多模态。
        """
        return None

    async def _latest_user_message(
        self, current_view: str, media: Any | None
    ) -> dict[str, Any]:
        """把本手最新局面组装成 history 之后的 user 消息。

        媒体已下线：``media`` 非空时也只发送文本局面，绝不把图片 URL 当
        纯文本混进 prompt。
        """
        return {"role": "user", "content": current_view}

    # ------------------------------------------------------------------
    #  prompt
    # ------------------------------------------------------------------
    def _system_prompt(self, match: Any, seat: int) -> str:
        """稳定 system：身份来自配置（bot/agent），不写死棋种/人名/符号。"""
        custom = str(getattr(self._plugin.settings, "ai_agent_prompt", "") or "").strip()
        if custom:
            return custom
        game_name = games.get(match.kind).name["zh"]
        name, persona = _seat_identity(self._plugin, match, seat)
        lines = [
            f"你是{name}，正在参与一局{game_name}，坐在第{seat}席；"
            "具体身份、局面表示与动作格式以本局规则和最新局面为准。",
        ]
        if persona:
            lines.append(
                "以下是你原本的身份/人设，保持原样，不要因为游戏改写或覆盖："
                f"{persona}"
            )
        lines.extend(
            [
                "轮到你时调用 play 工具执行本局动作，提交本手动作时需根据局势"
                "在同一次回复的普通正文里写一两句保持你原本人设的玩家短话；"
                "不要解说，也不要只发一条没有工具调用的纯文字消息。"
                "动作成功后结束，不续模型轮。",
                "短话写在同一回复的普通正文里，和工具调用一起提交，不通过"
                "工具参数传递，也不要只写在 reasoning/思考里；只有本手合法"
                "动作成功时才会作为你的发言被对手看到，失败时不会发出，也"
                "不需要为说话重试或改写动作。",
                "主 agent 的总体指令优先；本局策略与 plan 是战术提示，"
                "即使偏离默认目标也优先遵从，但必须符合规则。",
            ]
        )
        return "\n".join(lines)

    def _init_prompt(self, match: Any, seat: int) -> str:
        """稳定 user 初始化：只含身份/说明，规则与当前局面由最新消息承载。"""
        module = games.get(match.kind)
        name, _persona = _seat_identity(self._plugin, match, seat)
        lines = [
            f"本局是{module.name['zh']}（{module.name['en']}）。",
            f"你是{name}，坐在第{seat}席。",
            (
                "这是本局连续的决策记录；旧回合只用于理解前因，"
                "该执行哪一步永远以下一条最新的当前局面为准。"
            ),
            (
                "轮到你时调用 play 工具执行本局动作，提交本手动作时需根据局势"
                "在同一次回复的普通正文里写一两句保持你原本人设的玩家短话；"
                "不要解说，也不要只发一条没有工具调用的纯文字消息。"
                "动作成功后结束，不续模型轮。"
            ),
            (
                "短话写在同一回复的普通正文里，和工具调用一起提交，不通过"
                "工具参数传递，也不要只写在 reasoning/思考里；只有本手合法"
                "动作成功时才会作为你的发言被对手看到，失败时不会发出，也"
                "不需要为说话重试或改写动作。"
            ),
            (
                "主 agent 的总体指令优先；本局策略与 plan 是战术提示，"
                "即使偏离默认目标也优先遵从，但必须符合规则。"
            ),
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  兜底 / 节奏 / 日志
    # ------------------------------------------------------------------
    async def _fallback(
        self,
        room: Any,
        seat: int,
        snap: _Snapshot,
        reason: str,
        started: float,
        *,
        retries: int = 0,
        model: str = "",
    ) -> None:
        """仅在仍有效的当前回合用规则 bot 补一手。"""
        if getattr(self._plugin, "stopping", False):
            return
        latest = await room.ai_snapshot(seat)
        if not latest["active"] or int(latest["move_count"]) != snap.move_count:
            await self._log(
                snap,
                source="none",
                move="",
                model=model,
                fallback="stale",
                retries=retries,
                started=started,
            )
            return
        module = games.get(latest["match"].kind)
        if type(module).bot is GameModule.bot:
            await self._log(
                snap,
                source="none",
                move="",
                model=model,
                fallback="no_bot",
                retries=retries,
                started=started,
            )
            return
        move = module.bot(latest["match"].state, seat)
        if not move:
            await self._log(
                snap,
                source="none",
                move="",
                model=model,
                fallback="no_bot",
                retries=retries,
                started=started,
            )
            return
        await self._wait_min_think(started)
        if getattr(self._plugin, "stopping", False):
            return
        match, _error = await room.apply_ai_move(
            seat, move, expected_move_count=int(latest["move_count"])
        )
        if match is None:
            # 迟到回合：锁内已拒绝，不再补第二手。
            await self._log(
                snap,
                source="none",
                move="",
                model=model,
                fallback="stale",
                retries=retries,
                started=started,
            )
            return
        applied = match.log[-1].move if match.log else move
        self._record_bot_history(snap, applied, int(latest["move_count"]))
        await self._log(
            snap,
            source="bot",
            move=applied,
            model=model,
            fallback=reason,
            retries=retries,
            started=started,
        )

    def _record_bot_history(self, snap: _Snapshot, move: str, move_count: int) -> None:
        """把 bot 兜底作为完整 assistant/tool 事实写入跨手历史。"""
        cfg = self._plugin.settings
        limit = _history_rounds(cfg)
        if limit == 0:
            return
        call_id = f"bot-{snap.match.id}-{snap.seat}-{move_count}"
        messages = [
            {
                "role": "assistant",
                "content": "（本手由规则 bot 兜底执行。）",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "play",
                            "arguments": json.dumps(
                                {"move": move}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"已执行动作 {move}（规则 bot 兜底，不是模型候选）。",
            },
        ]
        try:
            self._history.append(snap.match.id, snap.seat, messages)
            self._history.trim(snap.match.id, snap.seat, limit)
        except Exception:
            logger.warning(
                "game_agent bot 兜底历史写入失败: %s seat %s",
                snap.match.id,
                snap.seat,
                exc_info=True,
            )

    async def _wait_min_think(self, started: float) -> None:
        cfg = self._plugin.settings
        minimum = max(0, int(getattr(cfg, "ai_min_think_ms", 0) or 0))
        jitter = max(0, int(getattr(cfg, "ai_think_jitter_ms", 0) or 0))
        target = (minimum + random.uniform(0, jitter)) / 1000.0
        remaining = target - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _snapshot(self, room: Any, seat: int) -> _Snapshot:
        data = await room.ai_snapshot(seat)
        match = data["match"]
        plan, text, seq, review = _active_plan(match, seat)
        labels = _seat_labels(match)
        return _Snapshot(
            match=match,
            active=bool(data["active"]),
            move_count=int(data["move_count"]),
            version=int(data["version"]),
            plan=plan,
            plan_text=text,
            plan_seq=seq,
            plan_review=review,
            board_hash=_board_hash(match.state),
            seat_label=labels[seat] if 0 <= seat < len(labels) else "",
            seat=seat,
        )

    async def _log_cancel(self, snap: _Snapshot, started: float) -> None:
        try:
            await self._log(
                snap,
                source="none",
                move="",
                model="",
                fallback="cancelled",
                retries=0,
                started=started,
            )
        except Exception:
            logger.debug("game_agent 取消日志写入失败", exc_info=True)

    async def _log(
        self,
        snap: _Snapshot,
        *,
        source: str,
        move: str,
        model: str,
        fallback: str,
        retries: int,
        started: float,
    ) -> None:
        cfg = self._plugin.settings
        if not bool(getattr(cfg, "ai_decision_log", True)):
            return
        entry = {
            "t": _now_ms(),
            "match_id": str(getattr(snap.match, "id", "")),
            "seat": snap.seat,
            "move_count": snap.move_count,
            "model": str(model or ""),
            "plan_seq": snap.plan_seq,
            "move": str(move or ""),
            "source": str(source or ""),
            "fallback": str(fallback or ""),
            "retries": int(retries),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "board_hash": snap.board_hash,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        path = self._log_path()
        try:
            await asyncio.to_thread(_append_line, path, line)
        except Exception:
            logger.warning("game_agent 决策日志写入失败: %s", path, exc_info=True)

    def _log_path(self) -> Path:
        base = getattr(self._plugin, "data_dir", None)
        return Path(base or ".") / "decision_log.jsonl"


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
