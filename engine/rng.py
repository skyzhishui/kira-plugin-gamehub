"""mulberry32 PRNG 的 Python 逐位等价实现（设计 §5.2 移植纪律）。

JS 的 ``Math.imul`` / ``|0`` / ``>>>`` 语义必须逐条模拟，否则与 rain-go
导出的对局无法复现。golden 向量见 ``tests/test_engine.py``。
"""

from __future__ import annotations

_MASK32 = 0xFFFFFFFF


def to_int32(value: int) -> int:
    """JS ``x | 0``：按 2^32 取模后解释为有符号 32 位。"""
    value &= _MASK32
    return value - 0x100000000 if value >= 0x80000000 else value


def to_uint32(value: int) -> int:
    """JS ``x >>> 0``：按 2^32 取模的无符号整数。"""
    return value & _MASK32


def imul(a: int, b: int) -> int:
    """JS ``Math.imul``：有符号 32 位乘法（低 32 位）。"""
    return to_int32(a * b)


def next_random(state: int) -> tuple[float, int]:
    """返回 ``(0..1 浮点, 下一个 RNG 状态)``。"""
    a = to_int32(state + 0x6D2B79F5)
    t = a
    t = imul(t ^ (to_uint32(t) >> 15), t | 1)
    t ^= to_int32(t + imul(t ^ (to_uint32(t) >> 7), t | 61))
    t = to_int32(t ^ (to_uint32(t) >> 14))
    return to_uint32(t) / 4294967296.0, a


def random_int(state: int, n: int) -> tuple[int, int]:
    """返回 ``[0, n)`` 的整数与下一个状态。"""
    r, s = next_random(state)
    return int(r * n), s


def shuffled(items: list, state: int) -> tuple[list, int]:
    """Fisher-Yates；返回新列表与下一个状态，不修改入参。"""
    out = list(items)
    s = state
    for i in range(len(out) - 1, 0, -1):
        j, s = random_int(s, i + 1)
        out[i], out[j] = out[j], out[i]
    return out, s