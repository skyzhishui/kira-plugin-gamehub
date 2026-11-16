"""共享的两席棋类执色辅助（五子棋 / 黑白棋逐字相同的实现）。

只有「座位 ↔ 执色」这一条零和映射放在这里：座位 0 执黑（1），座位 1 执白
（2）。游戏规则、候选与文本仍各自实现。之前两个模块各写一份逐字相同的
实现，任一侧改动都会让同一座位在两个棋种里执色不一致。
"""

from __future__ import annotations

from ..types import Seat


def color_of(seat: Seat) -> int:
    """座位 → 执色（0→1 黑，其余→2 白）。"""
    return 1 if seat == 0 else 2


def seat_of(color: int) -> Seat:
    """执色 → 座位（1→0，其余→1）。"""
    return color - 1
