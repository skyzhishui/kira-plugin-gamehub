"""引擎契约：与 rain-go ``packages/engine/src/match/types.ts`` 逐字段对齐。

移植纪律（设计 §5.2）：

- 游戏状态必须是纯 JSON 可序列化结构（dict/list/str/int/float/bool），
  不得出现自定义类、Map、set、datetime；
- 随机性只来自 ``CreateContext.seed``，RNG 状态存进 state；
- ``apply`` 不变异输入状态，非法走子返回中文短句。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Seat = int
SeatKind = Literal["human", "ai", "bot"]
Tempo = Literal["turn", "real_time"]


@dataclass(frozen=True)
class OptionSpec:
    """大厅展示、``clean_options`` 校验的选项。"""

    key: str
    label: str
    choices: list[dict[str, str]]
    default: str


@dataclass(frozen=True)
class PlayerSpec:
    min: int
    max: int
    default: int


@dataclass
class CreateContext:
    seed: int  # 已 & 0xFFFFFFFF
    players: int
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    winners: list[Seat]  # 空=平局
    text: str  # 中文短句，名字由对局层补


@dataclass
class MoveResultOk:
    state: Any
    log: str | None = None


@dataclass
class MoveResultErr:
    error: str


MoveResult = MoveResultOk | MoveResultErr


@dataclass
class KeyEvent:
    """模块上报的关键手/阶段事件（供 System1 触发打分）。"""

    kind: str
    importance: float
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class GameEvent:
    """统一事件协议（设计 §5.5.1）。

    回合制由对局层产生 ``turn_start`` / ``key`` / ``end`` / ``idle``；
    实时制由游戏模块或页面 WS 产生 ``phase`` / ``death`` / ``objective`` /
    ``heartbeat``，且必须给出 ``safe``。
    """

    kind: str
    phase: str
    importance: float
    safe: bool
    decisive: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    t: float = 0.0


class GameModule(ABC):
    """一个游戏的规则模块（纯函数、可 JSON 序列化）。

    必须实现的抽象方法见 rain-go ``GameModule``；``bot`` / ``key_events`` /
    ``chat_context`` / ``plan_prompt`` / ``safe_to_ask`` / ``trigger_events``
    是可选钩子：调用方一律用 ``getattr`` 探测，缺省实现保持无副作用。

    兼容说明（2026-11-12）：早期的 ``candidates`` 候选白名单与 ``plan_spec``
    字段已删除——分层决策/计划结构没有落地，继续保留只会诱导调用方依赖
    不存在的功能。历史对局里的 plan 数据仍按原结构读取，不受影响。
    """

    kind: str
    name: dict[str, str]
    family: str
    blurb: str
    ready: bool = True
    players: PlayerSpec
    options: list[OptionSpec] = []
    rules: str = ""
    move_help: str = ""
    tempo: Tempo = "turn"
    # 是否支持「行动通知回合」驱动（S2 turn driver）。默认 False：未知/未
    # 声明能力的游戏保守走旧的 game_agent 路径，不会继承棋类 S2 语义。
    supports_turn_driver: bool = False

    @abstractmethod
    def create(self, ctx: CreateContext) -> Any: ...

    @abstractmethod
    def apply(self, state: Any, seat: Seat, move: str) -> MoveResult: ...

    @abstractmethod
    def waiting_on(self, state: Any) -> list[Seat]: ...

    @abstractmethod
    def outcome(self, state: Any) -> Outcome | None: ...

    @abstractmethod
    def seat_labels(self, state: Any) -> list[str]: ...

    @abstractmethod
    def view(self, state: Any, viewer: Seat | None) -> Any: ...

    @abstractmethod
    def describe(self, state: Any, seat: Seat, names: list[str]) -> str: ...

    # ── 可选钩子 ──────────────────────────────────────────────────
    def bot(self, state: Any, seat: Seat) -> str | None:
        """规则 bot 的一步合法走子；None=该游戏没有 bot 座位。"""
        return None

    def key_events(
        self,
        state: Any,
        *,
        before: Any = None,
        seat: Seat | None = None,
    ) -> dict[str, Any]:
        """确定性关键手信号（可选钩子，纯规则、不联网）。

        返回布尔位字典，至少包含 ``key`` 汇总位；其余键由游戏模块自定义
        （如成五/挡五/活四/双三）。``before`` 是这一手之前的局面，
        ``seat`` 是刚落子的座位——调用方（notice judge）只据此判断“本手
        是否新制造/解除威胁”，不假设任何具体棋种或坐标协议。没有关键
        信号能力的游戏返回空字典。
        """
        return {}

    def chat_context(self, state: Any, viewer: Seat | None, names: list[str] | None = None) -> str | None:
        """可选：聊天上下文里的一局快照文本；``None``=该游戏不提供。

        ``viewer`` 是实际接收陪玩通知的行动座位（无则 ``None``），游戏模块
        不得假设固定座位号或玩家人数。``names`` 是座位显示名列表（可缺省
        ）：给定时快照里的身份行用它标注执色方（例如座位落库的显示名，
        可能是陪练/其他身份），缺省时不得冒用固定身份名，只能写中性
        描述（「你」「对方」等）。返回值应是纯只读的当前局面快照（例如
        棋盘/坐标规则/即时成五点），不得做落子决策、不得改状态、不得调用
        工具或网络。调用方按预算**整块**取舍，绝不对返回值做半截
        截断；因此实现者应保持文本简洁，并明确标注事实与建议的边界。
        """
        return None

    def plan_prompt(self, state: Any, seat: Seat) -> str:
        """可选：给当前游戏策略/下一次动作决策的通用指导。

        默认不绑定具体棋种，只说清「按游戏策略做下一次动作决策、先保证
        规则合法性、在探索/配合/节奏/目标取舍间选择」。具体游戏可覆盖，
        例如棋类补充攻防要点；这里不是强制走子白名单，也不替代 seat.plan。
        """
        return (
            "按当前游戏策略决定下一次动作决策：先确认目标与限制"
            "（通关、探索、配合、节奏、目标取舍等），保证规则合法性，"
            "再选择合理动作。"
        )

    def safe_to_ask(self, state: Any) -> bool:
        """实时制：现在是否适合发起 5-15s 的 LLM 回合。"""
        return True

    def validate_state(self, state: Any) -> str:
        """Optional: minimal state shape validation (for deserialisation and
        bad-data entry points).

        Returns an empty string on success, or a short human-readable reason
        on failure (the caller rejects the whole snapshot based on it). This
        only probes the **shape** (required keys present, container types and
        lengths consistent with the seat count); it does not check rule
        legality and makes no move decisions. The default implementation
        accepts any dict state; concrete games should override it so that a
        bad row cannot reach view/state derivation, fail with a 500, and never
        get archived.
        """
        return ""

    def outcome_for_resign(self, state: Any, seat: Seat) -> Outcome | None:
        """Optional: the verdict when seat ``seat`` resigns (by game faction
        semantics).

        The default ``None`` means the match layer applies the generic rule
        (everyone except the resigning seat is a winner), which suits two-seat
        zero-sum board games. Multi-faction games (e.g. dou-dizhu: landlord vs
        two farmers) must override it: teammates are on the losing side and
        must not be judged a winner merely because they did not resign.
        An override returning ``None``, an invalid type, or raising fails closed
        to a terminal outcome without winners. A returned ``Outcome`` is used
        as-is; only inheriting this default selects the generic rule.
        """
        return None

    def trigger_events(self, state: Any, prev: Any) -> list[GameEvent]:
        return []
