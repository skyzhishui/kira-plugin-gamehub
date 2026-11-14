"""plan / TTL 纯辅助（设计 §2.6，**已延期**：无活跃新建入口）。

``seat.plan`` 是 bot/玩家在本局的战术方向，**不是代码闸门**：只由
``game_agent`` 注入 prompt，任何一层都不能因 plan 拒绝合法走子。本模块只做
结构构造与过期判断，不依赖房间层，供内部 ``room.set_plan``（历史兼容）、
``game_state`` 以及 ``game_agent`` 共用。

延期说明：``game_plan`` 工具已从注册面移除（新计划行为延期），因此当前
**没有任何模型/用户可调用的新建计划入口**；本模块保留只为了让历史对局里
已存的 plan 数据仍能正确判断过期与展示（非破坏）。不要再基于这里新增
「计划功能」的对外声明。

TTL 口径：

- 回合制（``tempo="turn"``，当前五子棋）：按 ``created_move + ttl_moves``；
- 实时制（``tempo="real_time"``）：按 ``created_at + ttl_s``；
- 过期后字段保留（页面/工具仍可查看），但不再视为有效 plan。
"""

from __future__ import annotations

import math
from typing import Any

PLAN_SOURCE_AGENT = "agent"
MAX_PLAN_TEXT = 400


def normalize_plan_text(value: Any) -> str:
    """裁剪 plan 文本；空串表示没有有效内容。"""
    return str(value or "").strip()[:MAX_PLAN_TEXT]


def clamp_ttl_moves(value: Any, default: int) -> int:
    """``ttl_moves`` 只接受正整数；非法/缺省走配置默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return max(1, int(default))
    return max(1, min(100, number))


def plan_move_count(match: Any) -> int:
    """当前已走手数（回合制 TTL 的基准）。"""
    return len(getattr(match, "log", ()) or ())


def ai_seat_index(match: Any) -> int | None:
    """当前局 bot 的 AI 座位。

    只认 ``kind == "ai"``：``bot`` 是规则陪练，``human`` 是用户，都不能被
    ``game_resign`` 代认输，也不该接收 ``game_plan``。
    """
    for index, seat in enumerate(getattr(match, "seats", ()) or ()):
        if getattr(seat, "kind", "") == "ai":
            return index
    return None


def make_plan(
    text: Any,
    *,
    source: str = PLAN_SOURCE_AGENT,
    created_move: int,
    ttl_moves: int,
    ttl_s: float,
    now: int,
    previous: dict[str, Any] | None = None,
    review: bool = False,
) -> dict[str, Any]:
    """构造新的 ``seat.plan``（``seq`` 单调递增，旧决策不得覆盖新策略）。"""
    old_seq = 0
    if isinstance(previous, dict):
        try:
            old_seq = int(previous.get("seq") or 0)
        except (TypeError, ValueError):
            old_seq = 0
    return {
        "text": normalize_plan_text(text),
        "source": str(source or PLAN_SOURCE_AGENT),
        "created_move": max(0, int(created_move)),
        "ttl_moves": max(1, int(ttl_moves)),
        "created_at": int(now),
        "ttl_s": float(ttl_s),
        "seq": old_seq + 1,
        "review": bool(review),
    }


def plan_remaining(
    plan: dict[str, Any] | None,
    *,
    tempo: str,
    move_count: int,
    now: int,
) -> dict[str, Any]:
    """plan 的有效性与剩余 TTL（纯函数）。

    返回 ``{"active": bool, "mode": "turn"|"real_time",
    "moves_left": int|None, "seconds_left": int|None}``；没有 plan 时
    ``active=False`` 且两个剩余值均为 ``None``。
    """
    if not isinstance(plan, dict) or not normalize_plan_text(plan.get("text")):
        return {"active": False, "mode": tempo, "moves_left": None, "seconds_left": None}

    if tempo == "real_time":
        try:
            created_at = int(plan.get("created_at") or 0)
            ttl_s = float(plan.get("ttl_s") or 0.0)
        except (TypeError, ValueError):
            created_at, ttl_s = 0, 0.0
        left_ms = created_at + int(ttl_s * 1000) - int(now)
        seconds_left = max(0, int(math.ceil(left_ms / 1000))) if left_ms > 0 else 0
        return {
            "active": left_ms > 0,
            "mode": "real_time",
            "moves_left": None,
            "seconds_left": seconds_left,
        }

    try:
        created_move = int(plan.get("created_move") or 0)
        ttl_moves = int(plan.get("ttl_moves") or 0)
    except (TypeError, ValueError):
        created_move, ttl_moves = 0, 0
    moves_left = max(0, ttl_moves - (max(0, int(move_count)) - created_move))
    return {
        "active": moves_left > 0,
        "mode": "turn",
        "moves_left": moves_left,
        "seconds_left": None,
    }


def plan_summary(
    plan: dict[str, Any] | None,
    *,
    tempo: str,
    move_count: int,
    now: int,
    label: str = "你",
) -> str:
    """给工具/页面的单行 plan 摘要（含过期标记）。"""
    text = normalize_plan_text((plan or {}).get("text")) if isinstance(plan, dict) else ""
    if not text:
        return f"{label}的打法：未设定。"
    ttl = plan_remaining(plan, tempo=tempo, move_count=move_count, now=now)
    if not ttl["active"]:
        return f"{label}的打法：{text}（已过期，不再生效；字段保留供查看）"
    if ttl["mode"] == "real_time":
        remain = f"剩余约 {ttl['seconds_left']} 秒"
    else:
        remain = f"剩余 {ttl['moves_left']} 手"
    seq = (plan or {}).get("seq")
    return f"{label}的打法：{text}（{remain}，seq={seq}）"