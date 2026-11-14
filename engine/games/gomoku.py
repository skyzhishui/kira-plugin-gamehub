"""五子棋（freestyle）：15x15，座位 0 执黑先行，五连或以上即胜。

逐行移植自 rain-go ``packages/engine/src/games/gomoku.ts``（官方参考实现）。
"""

from __future__ import annotations

from typing import Any

from ..coords import COLUMNS, from_gtp, neighbors8, to_gtp
from ..types import (
    CreateContext,
    GameModule,
    MoveResult,
    MoveResultErr,
    MoveResultOk,
    OptionSpec,
    Outcome,
    PlayerSpec,
    Seat,
)

_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))


def color_of(seat: Seat) -> int:
    return 1 if seat == 0 else 2


def seat_of(color: int) -> Seat:
    return color - 1


def _five_through(cells: list[int], size: int, p: int, color: int) -> list[int] | None:
    x0, y0 = p % size, p // size
    for dx, dy in _DIRS:
        line = [p]
        for sign in (1, -1):
            x, y = x0 + dx * sign, y0 + dy * sign
            while 0 <= x < size and 0 <= y < size and cells[y * size + x] == color:
                line.append(y * size + x)
                x += dx * sign
                y += dy * sign
        if len(line) >= 5:
            return sorted(line)
    return None


def board_text(state: dict[str, Any]) -> str:
    size = state["size"]
    cols = " ".join(COLUMNS[:size])
    rows = [f"    {cols}"]
    for y in range(size):
        cells = " ".join(
            "X" if state["cells"][y * size + x] == 1
            else "O" if state["cells"][y * size + x] == 2
            else "."
            for x in range(size)
        )
        n = str(size - y).rjust(2)
        rows.append(f"{n}  {cells}  {n}")
    rows.append(f"    {cols}")
    return "\n".join(rows)


def winning_points(state: dict[str, Any], color: int) -> list[int]:
    """空点中落子即连五的位置。

    剪枝用 8 邻点（含斜向）：纯斜线棋形（如斜四）的补点与棋子只有斜向
    相邻，正交 4 邻点会漏检。
    """
    size = state["size"]
    cells = state["cells"]
    out: list[int] = []
    nb = neighbors8(size)
    for p, value in enumerate(cells):
        if value != 0 or not any(cells[q] != 0 for q in nb[p]):
            continue
        trial = list(cells)
        trial[p] = color
        if _five_through(trial, size, p, color):
            out.append(p)
    return out


def chat_context(
    state: dict[str, Any],
    viewer: Seat | None,
    names: list[str] | None = None,
) -> str | None:
    """给聊天上下文/行动通知的五子棋完整盘面快照（纯规则、只读）。

    只输出**事实**：完整棋盘、坐标规则、双方当前即可成五的空点。即时成五
    点直接复用 ``winning_points`` + ``to_gtp``，是「尚未落子」的事实描述，
    不是候选白名单，也不是推荐下一手。``viewer`` 是行动座位，只用于标明
    执色；身份行优先用 ``names[viewer]``（座位显示名，例如陪练/其他身份），
    缺省写「你」，不冒用固定身份名。不假设玩家总数或固定座位号。返回
    ``None`` 表示当前状态不完整。
    """
    size = int(state.get("size") or 15)
    cells = list(state.get("cells") or [])
    if len(cells) != size * size:
        return None
    moves = int(state.get("moves") or 0)

    if viewer is None:
        side_line = "执色：未提供座位，无法确定双方执色。"
    else:
        mine = color_of(viewer)
        other = 2 if mine == 1 else 1
        symbol = lambda color: "X（黑）" if color == 1 else "O（白）"  # noqa: E731
        viewer_name = "你"
        if names and isinstance(viewer, int) and 0 <= viewer < len(names):
            viewer_name = str(names[viewer] or "") or "你"
        side_line = (
            f"执色：{viewer_name}执 {symbol(mine)}，对方执 {symbol(other)}。"
        )

    def _points(color: int) -> str:
        wins = winning_points(state, color)
        return " ".join(to_gtp(p, size) for p in wins) if wins else "无"

    lines = [
        "五子棋完整局面（15×15）。",
        (
            f"坐标规则：列 A-P 跳过 I，行从下往上 1-{size}（如 H8）；"
            "X=黑、O=白、.=空。"
        ),
        side_line,
        (
            "棋盘上的 X/O 是已经发生的落子。以下可成五点尚未落子，"
            "只是当前事实描述，不是候选白名单或推荐下一手。"
        ),
        (
            f"当前即可成五的空点：黑（X）{_points(1)}；"
            f"白（O）{_points(2)}。"
        ),
        f"完整棋盘（来源：本轮 state.cells，已行 {moves} 手）：",
        board_text(state),
    ]
    return "\n".join(lines)


def _state_of(cells: list[int], size: int) -> dict[str, Any]:
    return {"size": size, "cells": cells}


def _candidate_points(cells: list[int], size: int) -> list[int]:
    """空点且 5×5 盒内（切比雪夫 ≤2）有棋子（威胁只可能在已有棋子附近产生）。

    单枚落子要造出新成五点，最近的己方子最远只隔 2 格（落点在一端、补点
    相邻、另三子占 +2/+3/+4），因此 8 邻域会漏掉跳空棋形，用 ≤2 盒覆盖
    （与 ``gomoku_bot`` 的候选口径一致）。
    """
    out: list[int] = []
    for p, value in enumerate(cells):
        if value != 0:
            continue
        x, y = p % size, p // size
        near = False
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                nx, ny = x + dx, y + dy
                if (
                    0 <= nx < size
                    and 0 <= ny < size
                    and cells[ny * size + nx] != 0
                ):
                    near = True
                    break
            if near:
                break
        if near:
            out.append(p)
    return out


def _four_making_points(cells: list[int], size: int, color: int) -> set[int]:
    """落子后能立刻形成「双头四 / 双四」（两个即时成五点）的空点集合。

    这是 ``double_three`` 的判定底座：一手若同时制造两个这样的点，等价于
    两个独立的活三/四威胁。只统计 *这一手之后* 的局面，历史既有威胁要
    靠调用方对比 before/after 才不会每手重复触发。

    实现上不做全盘重扫：加一枚己方子只会 (a) 占掉 ``p`` 原有的成五点、
    (b) 在过 ``p`` 的 4 条直线 ±4 格内新增成五点，其余成五点与基盘一致。
    因此基线成五点只算一次，每个候选点在射线上**增量**检查，且基线与
    新增取**并集**计数（同一空点两边都出现时相加会重复计数）。试补采用
    原地写入后立即还原，不复制整盘。
    """
    base = set(winning_points(_state_of(cells, size), color))
    out: set[int] = set()
    for p in _candidate_points(cells, size):
        wins = base - {p}
        if len(wins) < 2:
            x0, y0 = p % size, p // size
            cells[p] = color
            try:
                for dx, dy in _DIRS:
                    if len(wins) >= 2:
                        break
                    for sign in (1, -1):
                        if len(wins) >= 2:
                            break
                        for step in range(1, 5):
                            x, y = x0 + dx * step * sign, y0 + dy * step * sign
                            if not (0 <= x < size and 0 <= y < size):
                                break
                            q = y * size + x
                            v = cells[q]
                            if v not in (0, color):
                                break  # 敌子挡线：更远的点无法与 p 同连五
                            if v == color:
                                continue
                            cells[q] = color
                            if _five_through(cells, size, q, color):
                                wins.add(q)
                            cells[q] = 0
            finally:
                cells[p] = 0
        if len(wins) >= 2:
            out.add(p)
    return out


def _runs(cells: list[int], size: int, color: int):
    """枚举某颜色的极大连续段（每段只输出一次），产出 (长度, 开放端数)。"""
    for p, value in enumerate(cells):
        if value != color:
            continue
        x0, y0 = p % size, p // size
        for dx, dy in _DIRS:
            px, py = x0 - dx, y0 - dy
            if 0 <= px < size and 0 <= py < size and cells[py * size + px] == color:
                continue  # 不是该方向上的段首
            yield _run_at(cells, size, p, color, dx, dy)


_DIR_LABELS = {(1, 0): "横", (0, 1): "纵", (1, 1): "斜A", (1, -1): "斜B"}


def _can_extend_to_open_four(
    cells: list[int],
    size: int,
    color: int,
    p: int,
    dx: int,
    dy: int,
) -> bool:
    """段首 p 的连续三，是否有一端延伸后能在同方向形成活四。"""
    x0, y0 = p % size, p // size
    for step in (-1, 3):
        x, y = x0 + dx * step, y0 + dy * step
        if not (0 <= x < size and 0 <= y < size):
            continue
        q = y * size + x
        if cells[q] != 0:
            continue
        trial = list(cells)
        trial[q] = color
        length, ends = _run_at(trial, size, q, color, dx, dy)
        if length == 4 and ends == 2:
            return True
    return False


def _fmt_points(points: list[int], size: int) -> str:
    """坐标格式化：统一按「行号从下往上、列从左到右」排序后输出。"""
    ordered = sorted(points, key=lambda p: (size - p // size, p % size))
    return " ".join(to_gtp(p, size) for p in ordered)


def _runs_detailed(
    cells: list[int], size: int, color: int
):
    """枚举某颜色的极大连续段：产出 ``(方向, 各点索引, 开放端数)``。

    与 ``_runs`` 同源（每段只输出一次），但额外带出段内每个点的索引，
    供威胁摘要输出坐标。
    """
    for p, value in enumerate(cells):
        if value != color:
            continue
        x0, y0 = p % size, p // size
        for dx, dy in _DIRS:
            px, py = x0 - dx, y0 - dy
            if 0 <= px < size and 0 <= py < size and cells[py * size + px] == color:
                continue  # 不是该方向上的段首
            points: list[int] = []
            x, y = x0, y0
            while 0 <= x < size and 0 <= y < size and cells[y * size + x] == color:
                points.append(y * size + x)
                x += dx
                y += dy
            ends = 0
            if 0 <= x < size and 0 <= y < size and cells[y * size + x] == 0:
                ends += 1
            if 0 <= px < size and 0 <= py < size and cells[py * size + px] == 0:
                ends += 1
            yield (dx, dy), points, ends


def _open_ends(
    cells: list[int],
    size: int,
    points: list[int],
    dx: int,
    dy: int,
) -> list[int]:
    """段两端相邻的空点（有序；段两侧都被堵时返回空列表）。"""
    x0, y0 = points[0] % size, points[0] // size
    x1, y1 = points[-1] % size, points[-1] // size
    out: list[int] = []
    for x, y in ((x0 - dx, y0 - dy), (x1 + dx, y1 + dy)):
        if 0 <= x < size and 0 <= y < size:
            q = y * size + x
            if cells[q] == 0:
                out.append(q)
    return out


def _live_three_directions(
    cells: list[int], size: int, color: int
) -> dict[tuple[int, int], int]:
    """严格活三的方向计数（供多方向信号判定）。"""
    out: dict[tuple[int, int], int] = {}
    for p, value in enumerate(cells):
        if value != color:
            continue
        x0, y0 = p % size, p // size
        for dx, dy in _DIRS:
            px, py = x0 - dx, y0 - dy
            if 0 <= px < size and 0 <= py < size and cells[py * size + px] == color:
                continue  # 不是该方向上的段首
            length, ends = _run_at(cells, size, p, color, dx, dy)
            if (
                length == 3
                and ends == 2
                and _can_extend_to_open_four(cells, size, color, p, dx, dy)
            ):
                out[(dx, dy)] = out.get((dx, dy), 0) + 1
    return out


def _jump_threes(
    cells: list[int], size: int, color: int
) -> list[tuple[list[int], int]]:
    """跳三：产出 ``(三点索引, 补点索引)``。

    口径：同一直线 4 格窗口内恰好 3 枚己方子 + 1 个**中间**空位，补上该
    空位即成四且该四至少一端可成五。端部空位属连续三（由活三/眠三覆盖），
    此处不重复；补空后成五的跳四已由成五点覆盖，故要求成四而非成五。
    """
    out: list[tuple[list[int], int]] = []
    seen: set[tuple[int, ...]] = set()
    # 原地试补（trial 复用单个 list，避免每个窗口复制整盘）：
    # cells 只读，写入 gap 后立即还原，不改变调用方数据。
    trial = list(cells)
    for y in range(size):
        for x in range(size):
            for dx, dy in _DIRS:
                window: list[int] = []
                for step in range(4):
                    nx, ny = x + dx * step, y + dy * step
                    if not (0 <= nx < size and 0 <= ny < size):
                        window = []
                        break
                    window.append(ny * size + nx)
                if not window:
                    continue
                values = [cells[p] for p in window]
                if values.count(color) != 3 or values.count(0) != 1:
                    continue
                gap_index = values.index(0)
                if gap_index in (0, 3):
                    continue  # 空位在端部 = 连续三，不是跳三
                gap = window[gap_index]
                stones = [p for p in window if p != gap]
                key = tuple(sorted(stones))
                if key in seen:
                    continue
                trial[gap] = color
                try:
                    length, ends = _run_at(trial, size, gap, color, dx, dy)
                finally:
                    trial[gap] = 0
                if length != 4 or ends < 1:
                    continue
                seen.add(key)
                out.append((stones, gap))
    return out


def _threat_profile(cells: list[int], size: int, color: int) -> dict[str, Any]:
    """某颜色的威胁画像（纯函数；供 analyze 与 key_events 共用）。"""
    state = _state_of(cells, size)
    wins = winning_points(state, color)
    four_points = _four_making_points(cells, size, color)
    runs = list(_runs(cells, size, color))
    return {
        "win_points": wins,
        "open_three_points": len(four_points),
        "double_three": len(four_points) >= 2,
        "open_four": any(length == 4 and ends == 2 for length, ends in runs),
        "four": any(length == 4 and ends >= 1 for length, ends in runs),
    }


def threat_summary(state: dict[str, Any], seat: Seat) -> str:
    """给 ``game_agent`` 的当前盘面威胁摘要（纯规则，不做落子决策）。

    每一类威胁都输出**具体坐标**，便于模型直接使用而不必自行数格子：

    - 即时成五点（落子即成五）；
    - 活四（``XXXX`` 两端开，挡不住）/ 眠四（``XXXX`` 单端开）；
    - 连续活三（三点索引 + 两端延伸空点）；
    - 跳三（``XX_X`` / ``X_XX`` 型，补点坐标）；
    - 眠三（连续三但只有单端可延伸，属可防的三，单列一行）。

    每类存在即各输出一条，不合并计数。「双活三信号」只表示多个独立方向
    各有连续活三，**未验证这些活三是否由同一落点形成严格双三**；该检测
    不覆盖跳四/其它复合棋形。仅反映当前盘面，不覆盖 seat.plan。
    """
    size = int(state.get("size") or 15)
    cells = list(state.get("cells") or [])
    summary = analyze(state)
    threats = summary.get("threats") or {}
    mine = color_of(seat)
    lines: list[str] = []
    for color in (mine, 2 if mine == 1 else 1):
        profile = threats.get(color) or {}
        who = "己方" if color == mine else "对方"
        color_name = "黑" if color == 1 else "白"
        parts: list[str] = []
        wins = [to_gtp(int(p), size) for p in (profile.get("win_points") or [])]
        if wins:
            parts.append("当前即可成五：" + " ".join(wins))

        if len(cells) == size * size:
            live_four: list[str] = []
            sleep_four: list[str] = []
            live_three: list[str] = []
            sleep_three: list[str] = []
            for (dx, dy), points, ends in _runs_detailed(cells, size, color):
                coords = _fmt_points(points, size)
                if len(points) == 4:
                    if ends == 2:
                        live_four.append(coords)
                    elif ends == 1:
                        sleep_four.append(coords)
                elif len(points) == 3:
                    if ends == 2 and _can_extend_to_open_four(
                        cells, size, color, points[0], dx, dy
                    ):
                        live_three.append(
                            coords
                            + "（延伸 "
                            + _fmt_points(
                                _open_ends(cells, size, points, dx, dy), size
                            )
                            + "）"
                        )
                    elif ends == 1:
                        sleep_three.append(coords)
            jumps = _jump_threes(cells, size, color)
            jump_text = [
                _fmt_points(stones, size) + "（补 " + to_gtp(gap, size) + "）"
                for stones, gap in jumps
            ]
        else:
            live_four, sleep_four, live_three, sleep_three, jump_text = (
                [], [], [], [], [],
            )

        if live_four:
            parts.append("活四：" + "、".join(live_four))
        if sleep_four:
            parts.append("眠四：" + "、".join(sleep_four))
        if live_three:
            parts.append("活三：" + "、".join(live_three))
        if jump_text:
            parts.append("跳三：" + "、".join(jump_text))
        if sleep_three:
            parts.append("眠三：" + "、".join(sleep_three))

        live_dirs = (
            _live_three_directions(cells, size, color)
            if len(cells) == size * size
            else {}
        )
        if len(live_dirs) >= 2:
            parts.append(
                f"双活三信号：{len(live_dirs)} 个方向已有活三"
                "（未验证同一落点形成的严格双三）"
            )

        heuristic = int(profile.get("open_three_points") or 0)
        if heuristic:
            parts.append(
                f"启发式多威胁信号：可形成双成五威胁的落点 {heuristic} 个"
                "（不等同严格双三）"
            )
        if not parts:
            parts.append("暂无成五点/活四/眠四，也未检出活三、跳三、眠三")
        lines.append(f"- {who}（{color_name}）：" + "；".join(parts) + "。")
    return "\n".join(lines)


def analyze(state: dict[str, Any]) -> dict[str, Any]:
    """局面威胁摘要（纯规则，不做任何落子决策）。

    服务 notice/log 与 ``game_agent`` 的 ``threat_summary``：给出双方即时
    成五点、活三/四与终局标记。``double_three`` 是既有的启发式多威胁信号，
    不等同严格双三；多方向连续活三见 ``_live_three_directions``，同样不
    验证是否由同一落点形成严格双三。不进入 ``bot`` / ``candidates``，也不
    改变对局状态。
    """
    size = int(state.get("size") or 15)
    cells = list(state.get("cells") or [])
    if len(cells) != size * size:
        return {"moves": 0, "to_play": 0, "win": False, "threats": {}}
    last = state.get("last")
    return {
        "moves": int(state.get("moves") or 0),
        "to_play": int(state.get("toPlay") or 0),
        "last": to_gtp(last, size) if isinstance(last, int) else "",
        "win": bool(state.get("winLine")),
        "full": bool(state.get("full")),
        "threats": {
            color: _threat_profile(cells, size, color) for color in (1, 2)
        },
    }


KEY_EVENT_NAMES = (
    "win_now",
    "block_win",
    "open_four",
    "four",
    "double_three",
)


def key_events(
    state: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
    seat: int | None = None,
) -> dict[str, Any]:
    """确定性快速通道信号（纯规则，不联网）。

    关键口径：**只认这一手新制造/解除的威胁**。``before`` 是落子前的局面，
    有了它才能避免「历史上已经有活四，之后每手都 key」的重复刷屏。
    ``before=None`` 时退化为只看当前局面（用于离线诊断，不用于线上循环）。

    返回各布尔位 + ``key`` 汇总裁剪。``four``/``double_three`` 属于战术
    提示而非胜负证明，因此同样要求「本手新增」才置位。
    """
    size = int(state.get("size") or 15)
    cells = list(state.get("cells") or [])
    flags: dict[str, Any] = {name: False for name in KEY_EVENT_NAMES}
    flags["key"] = False
    if len(cells) != size * size:
        return flags

    mover: int | None = None
    if seat is not None:
        mover = color_of(int(seat))
    else:
        last = state.get("last")
        if isinstance(last, int) and 0 <= last < len(cells):
            mover = int(cells[last]) or None
        if mover not in (1, 2):
            mover = int(state.get("toPlay") or 0) or None
    if mover not in (1, 2):
        return flags
    opponent = 2 if mover == 1 else 1

    if state.get("winLine"):
        flags["win_now"] = True
        flags["key"] = True
        return flags

    before_cells: list[int] | None = None
    if before is not None:
        raw = list(before.get("cells") or [])
        if len(raw) == size * size:
            before_cells = raw

    after_wins = set(winning_points(_state_of(cells, size), mover))
    if before_cells is not None:
        before_wins = set(winning_points(_state_of(before_cells, size), mover))
    else:
        before_wins = set()

    if after_wins - before_wins:
        if len(after_wins) >= 2 and len(before_wins) < 2:
            flags["open_four"] = True  # 双头四 / 双四：挡不住
        elif len(after_wins) >= 1:
            flags["four"] = True  # 单四：对手必须应
    elif len(after_wins) >= 2 and len(before_wins) < 2:
        # 本手把已有单四升级/转移成双威胁；即时成五点集合与 before 相同
        # 也算新增（例如唯一成五点被转移，但数量变多）。
        flags["open_four"] = True

    if before_cells is not None:
        before_block = set(winning_points(_state_of(before_cells, size), opponent))
        after_block = set(winning_points(_state_of(cells, size), opponent))
        # 只有「本手确实减少了对手的必挡点」才算 block_win；
        # 对手一直有威胁而本方一直没挡，不重复触发。
        if before_block and len(after_block) < len(before_block):
            flags["block_win"] = True

    after_three = _four_making_points(cells, size, mover)
    if len(after_three) >= 2:
        if before_cells is None:
            flags["double_three"] = True
        else:
            before_three = _four_making_points(before_cells, size, mover)
            if len(before_three) < 2:
                flags["double_three"] = True

    flags["key"] = any(bool(flags[name]) for name in KEY_EVENT_NAMES)
    return flags


def _run_at(
    cells: list[int], size: int, p: int, color: int, dx: int, dy: int
) -> tuple[int, int]:
    length, open_ends = 1, 0
    x0, y0 = p % size, p // size
    for sign in (1, -1):
        x, y = x0 + dx * sign, y0 + dy * sign
        while 0 <= x < size and 0 <= y < size and cells[y * size + x] == color:
            length += 1
            x += dx * sign
            y += dy * sign
        if 0 <= x < size and 0 <= y < size and cells[y * size + x] == 0:
            open_ends += 1
    return length, open_ends


def _run_score(length: int, open_ends: int) -> float:
    if length >= 5:
        return 1_000_000
    if open_ends == 0:
        return 0
    if length == 4:
        return 50_000 if open_ends == 2 else 6_000
    if length == 3:
        return 4_000 if open_ends == 2 else 400
    if length == 2:
        return 200 if open_ends == 2 else 30
    return 10 if open_ends == 2 else 2


def gomoku_ranked_moves(state: dict[str, Any], seat: Seat) -> list[dict[str, Any]]:
    """启发式 bot：只评分已有棋子附近的空点，兼顾自己的形与封堵。"""
    size = state["size"]
    cells = state["cells"]
    me = color_of(seat)
    them = 2 if me == 1 else 1
    center = size // 2
    if state["moves"] == 0:
        points = sorted(range(size * size), key=lambda p: (abs(p % size - center) + abs(p // size - center), p))
        return [{"move": to_gtp(p, size), "score": float(-(abs(p % size - center) + abs(p // size - center)))} for p in points]

    ranked = []
    for p, value in enumerate(cells):
        if value != 0:
            continue
        x, y = p % size, p // size
        near = any(
            cells[(y + dy) * size + (x + dx)]
            for dy in range(-2, 3)
            for dx in range(-2, 3)
            if 0 <= y + dy < size and 0 <= x + dx < size
        )
        if not near:
            continue
        trial = list(cells)
        score = 0.0
        for dx, dy in _DIRS:
            trial[p] = me
            score += _run_score(*_run_at(trial, size, p, me, dx, dy))
            trial[p] = them
            score += 0.9 * _run_score(*_run_at(trial, size, p, them, dx, dy))
        score -= (abs(x - center) + abs(y - center)) * 0.01
        ranked.append({"move": to_gtp(p, size), "score": score})
    return sorted(ranked, key=lambda item: -item["score"])


def gomoku_bot(state: dict[str, Any], seat: Seat) -> str | None:
    ranked = gomoku_ranked_moves(state, seat)
    return ranked[0]["move"] if ranked else None


class Gomoku(GameModule):
    def ranked_moves(self, state, seat):
        return gomoku_ranked_moves(state, seat)

    kind = "gomoku"
    name = {"zh": "五子棋", "en": "Gomoku"}
    family = "棋"
    blurb = "横竖斜连成五子即胜。"
    ready = True
    players = PlayerSpec(min=2, max=2, default=2)
    options: list[OptionSpec] = []
    rules = (
        'Freestyle gomoku on a 15x15 board. Seat 0 plays black and moves first. '
        "Five or more stones in a row (horizontal, vertical or diagonal) wins. "
        "Full board is a draw. Moves: a coordinate like \"H8\" "
        "(columns A-P skip I, row 1 is the bottom)."
    )
    move_help = '"H8" (columns A-P skip I, row 1 at the bottom)'
    supports_turn_driver = True

    def create(self, ctx: CreateContext) -> dict[str, Any]:
        size = 15
        return {
            "size": size,
            "cells": [0] * (size * size),
            "toPlay": 1,
            "moves": 0,
        }

    def apply(self, state: dict[str, Any], seat: Seat, move: str) -> MoveResult:
        if state.get("winLine") or state.get("full"):
            return MoveResultErr("对局已经结束")
        if color_of(seat) != state["toPlay"]:
            return MoveResultErr("还没轮到你")
        p = from_gtp(move, state["size"])
        if p is None:
            return MoveResultErr(f"看不懂这步：{move}")
        if state["cells"][p] != 0:
            return MoveResultErr("这里已经有子了")
        cells = list(state["cells"])
        cells[p] = state["toPlay"]
        win_line = _five_through(cells, state["size"], p, state["toPlay"])
        full = not win_line and all(c != 0 for c in cells)
        next_state = {
            "size": state["size"],
            "cells": cells,
            "last": p,
            "moves": state["moves"] + 1,
            "toPlay": 2 if state["toPlay"] == 1 else 1,
        }
        if win_line:
            next_state["winLine"] = win_line
        if full:
            next_state["full"] = True
        return MoveResultOk(next_state, to_gtp(p, state["size"]))

    def waiting_on(self, state: dict[str, Any]) -> list[Seat]:
        if state.get("winLine") or state.get("full"):
            return []
        return [seat_of(state["toPlay"])]

    def outcome(self, state: dict[str, Any]) -> Outcome | None:
        win_line = state.get("winLine")
        if win_line:
            winner = state["cells"][win_line[0]]
            return Outcome(
                winners=[seat_of(winner)],
                text=f"{'黑' if winner == 1 else '白'}连五",
            )
        return Outcome(winners=[], text="棋盘下满") if state.get("full") else None

    def seat_labels(self, state: dict[str, Any]) -> list[str]:
        return ["黑", "白"]

    def view(self, state: dict[str, Any], viewer: Seat | None) -> dict[str, Any]:
        return {**state, "you": None if viewer is None else color_of(viewer)}

    def describe(
        self, state: dict[str, Any], seat: Seat, names: list[str]
    ) -> str:
        mine = color_of(seat)
        symbol = lambda c: "X (black)" if c == 1 else "O (white)"  # noqa: E731
        out = [
            f"You are {symbol(mine)}. {names[1 - seat]} is {symbol(2 if mine == 1 else 1)}. "
            f"Moves so far: {state['moves']}."
        ]
        if "last" in state:
            out.append(f"Last move: {to_gtp(state['last'], state['size'])}.")
        if not state.get("winLine") and not state.get("full"):
            wins = winning_points(state, mine)
            threats = winning_points(state, 2 if mine == 1 else 1)
            if wins:
                out.append(
                    "You can win now at: "
                    + " ".join(to_gtp(p, state["size"]) for p in wins)
                    + "."
                )
            if threats:
                out.append(
                    "Opponent threatens five at: "
                    + " ".join(to_gtp(p, state["size"]) for p in threats)
                    + ". Block it unless you can win."
                )
        out.append("")
        out.append(board_text(state))
        return "\n".join(out)

    def threat_summary(self, state: dict[str, Any], seat: Seat) -> str:
        """可选钩子：给 game_agent 的当前盘面威胁摘要。"""
        return threat_summary(state, seat)

    def chat_context(
        self, state: dict[str, Any], viewer: Seat | None, names: list[str] | None = None
    ) -> str | None:
        """可选钩子：聊天上下文的完整盘面快照（不参与落子决策）。"""
        return chat_context(state, viewer, names)

    def plan_prompt(self, state: dict[str, Any], seat: Seat) -> str:
        """棋类策略指导：只提示攻防取舍，不替代 seat.plan 或规则合法性。"""
        return (
            "五子棋策略：先看双方是否有成五/挡五，再看活四、冲四、双三与活三；"
            "进攻与防守服从当前计划，任何策略都不得走非法点。"
        )

    def bot(self, state: dict[str, Any], seat: Seat) -> str | None:
        return gomoku_bot(state, seat)

    def key_events(
        self,
        state: dict[str, Any],
        *,
        before: dict[str, Any] | None = None,
        seat: Seat | None = None,
    ) -> dict[str, Any]:
        """可选钩子：复用模块级纯规则 ``key_events``（无坐标假设泄漏）。"""
        return key_events(state, before=before, seat=seat)


gomoku = Gomoku()
