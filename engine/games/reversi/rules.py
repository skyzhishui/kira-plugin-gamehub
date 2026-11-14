"""黑白棋纯规则：翻子、合法点、落子与计数。

坐标沿用插件 GTP 约定（``engine/coords.py``）：列 A-H，行 1 在底部，
因此与 rain-go（行 1 在顶部）的棋盘索引相同、显示行号上下翻转。
棋盘 ``cells`` 索引 = y*8+x，y=0 为最上排；0 空、1 黑、2 白。
"""

from __future__ import annotations

SIZE = 8
CELLS = SIZE * SIZE

# 8 方向偏移；跨行保护在 ``flips_for`` 内用列差判断（与 rain-go 一致）。
DIRECTIONS = (-9, -8, -7, -1, 1, 7, 8, 9)

# 四角（索引）：A1、H1、A8、H8。
CORNERS = (0, 7, 56, 63)


def initial_cells() -> list[int]:
    """标准 8×8 开局：D5/E4 白，E5/D4 黑（本插件行号）。"""
    cells = [0] * CELLS
    cells[27] = 2  # D5
    cells[28] = 1  # E5
    cells[35] = 1  # D4
    cells[36] = 2  # E4
    return cells


def flips_for(cells: list[int], p: int, color: int) -> list[int]:
    """在空点 ``p`` 落 ``color`` 能翻掉的对方棋子（8 方向）。"""
    if not (0 <= p < CELLS) or cells[p] != 0:
        return []
    enemy = 2 if color == 1 else 1
    out: list[int] = []
    for delta in DIRECTIONS:
        run: list[int] = []
        q = p
        for _ in range(CELLS):
            col = q % SIZE
            q += delta
            if q < 0 or q >= CELLS:
                break
            # 行边缘防环绕：跨行时列差必须仍为 1。
            if abs((q % SIZE) - col) > 1:
                break
            if cells[q] == enemy:
                run.append(q)
            else:
                if cells[q] == color and run:
                    out.extend(run)
                break
    return out


def legal_moves(cells: list[int], color: int) -> dict[int, int]:
    """该颜色的合法点 → 翻子数。"""
    out: dict[int, int] = {}
    for p in range(CELLS):
        flips = flips_for(cells, p, color)
        if flips:
            out[p] = len(flips)
    return out


def counts(cells: list[int]) -> tuple[int, int]:
    """(黑子数, 白子数)。"""
    return cells.count(1), cells.count(2)


def apply_move(
    cells: list[int], p: int, color: int
) -> tuple[list[int], list[int]]:
    """返回落子并翻转后的新棋盘（不改输入）与翻掉的点。"""
    flips = flips_for(cells, p, color)
    if not flips:
        return list(cells), []
    out = list(cells)
    out[p] = color
    for q in flips:
        out[q] = color
    return out, flips
