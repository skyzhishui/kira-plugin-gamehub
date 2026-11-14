"""GTP 风格坐标（与 rain-go ``packages/engine/src/coords.ts`` 对齐）。

列 A..T 跳过 I；行从棋盘底部数起，1 = 最下一行。
"""

from __future__ import annotations

import re

COLUMNS = "ABCDEFGHJKLMNOPQRST"

_GTP_RE = re.compile(r"^\s*([A-HJ-Ta-hj-t])\s*(\d{1,2})\s*$")

_neighbor_cache: dict[int, list[list[int]]] = {}
_neighbor8_cache: dict[int, list[list[int]]] = {}


def point_of(x: int, y: int, size: int) -> int:
    return y * size + x


def xy_of(p: int, size: int) -> tuple[int, int]:
    return p % size, p // size


def to_gtp(p: int, size: int) -> str:
    x, y = xy_of(p, size)
    return f"{COLUMNS[x]}{size - y}"


def from_gtp(text: str, size: int) -> int | None:
    """解析 ``D4`` / ``d4``；格式错误或越界返回 None。"""
    m = _GTP_RE.match(text or "")
    if not m:
        return None
    x = COLUMNS.find(m.group(1).upper())
    row = int(m.group(2))
    if x < 0 or x >= size or row < 1 or row > size:
        return None
    return point_of(x, size - row, size)


def neighbors(size: int) -> list[list[int]]:
    """每个点的上下左右邻点（按棋盘尺寸缓存）。"""
    cached = _neighbor_cache.get(size)
    if cached is not None:
        return cached
    out: list[list[int]] = []
    for p in range(size * size):
        x, y = p % size, p // size
        lst: list[int] = []
        if y > 0:
            lst.append(p - size)
        if x < size - 1:
            lst.append(p + 1)
        if y < size - 1:
            lst.append(p + size)
        if x > 0:
            lst.append(p - 1)
        out.append(lst)
    _neighbor_cache[size] = out
    return out


def neighbors8(size: int) -> list[list[int]]:
    """每个点的 8 邻点（含斜向；按棋盘尺寸缓存）。

    五子棋的连五可沿斜线，成五点与既有棋子可能只有**斜向**相邻（如纯斜
    四的补点），正交 4 邻点剪枝会漏掉这类棋形，威胁/成五点扫描必须用
    8 邻点。
    """
    cached = _neighbor8_cache.get(size)
    if cached is not None:
        return cached
    out: list[list[int]] = []
    for p in range(size * size):
        x, y = p % size, p // size
        lst: list[int] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < size and 0 <= ny < size:
                    lst.append(ny * size + nx)
        out.append(lst)
    _neighbor8_cache[size] = out
    return out