"""黑白棋（Reversi / Othello）游戏模块：8×8，座位 0 执黑先行。

规则事实与 bot 参考 rain-go ``packages/engine/src/games/reversi.ts``；坐标
沿用本插件 GTP 约定（列 A-H，**行 1 在底部**），棋盘索引与 rain-go 相同，
只是显示行号上下翻转（rain-go ``d3`` == 本插件 ``D6``）。

pass 只写在 state（``passed``）里：``apply`` 落子后若对手无合法点、自己仍
有，则 ``toPlay`` 保持自己、``passed=对手色``；双方都无合法点则 ``over``。
pass 不进入对局 ``log``（一次 apply 一条日志、``move_count`` 语义不变），
因此规则 bot 连走（autoplay）、S2 行动通知回合与委托代下的同座位连走都由
既有回合推进直接成立。**例外是 advanced game_agent**：模型决策不是
autoplay 的一部分，AI 落子后仍轮到它时必须由房间层补排一次驱动
（``room.GameRoom.apply_ai_move`` 的 re-drive），否则会停在同一座位。
"""

from __future__ import annotations

from typing import Any

from ...coords import from_gtp, to_gtp
from ...types import (
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
from .rules import (
    CORNERS,
    SIZE,
    apply_move,
    counts,
    flips_for,
    initial_cells,
    legal_moves,
)

# 一次翻掉至少这么多子记为「大翻」（通知/关键手信号口径，纯规则）。
BIG_FLIP = 5

# 经典位置权重（角位金贵，角旁是陷阱），与 rain-go 一致。
WEIGHTS = (
    120, -20, 20, 5, 5, 20, -20, 120,
    -20, -40, -5, -5, -5, -5, -40, -20,
    20, -5, 15, 3, 3, 15, -5, 20,
    5, -5, 3, 3, 3, 3, -5, 5,
    5, -5, 3, 3, 3, 3, -5, 5,
    20, -5, 15, 3, 3, 15, -5, 20,
    -20, -40, -5, -5, -5, -5, -40, -20,
    120, -20, 20, 5, 5, 20, -20, 120,
)

COLOR_NAMES = {1: "黑", 2: "白"}
SYMBOLS = {1: "X（黑）", 2: "O（白）"}


def color_of(seat: Seat) -> int:
    return 1 if seat == 0 else 2


def seat_of(color: int) -> Seat:
    return color - 1


def _other(color: int) -> int:
    return 2 if color == 1 else 1


def _cells_of(state: dict[str, Any]) -> list[int]:
    return list(state.get("cells") or [])


def board_text(state: dict[str, Any], mark_color: int | None = None) -> str:
    """ASCII 棋盘：行号从下往上 1-8；``mark_color`` 的合法点显示 ``*``。"""
    size = int(state.get("size") or SIZE)
    cells = _cells_of(state)
    legal = (
        legal_moves(cells, mark_color)
        if mark_color in (1, 2) and not state.get("over")
        else {}
    )
    head = "    " + " ".join("ABCDEFGH"[:size])
    rows = [head]
    for y in range(size):
        marks: list[str] = []
        for x in range(size):
            p = y * size + x
            value = cells[p] if p < len(cells) else 0
            if value == 1:
                marks.append("X")
            elif value == 2:
                marks.append("O")
            elif p in legal:
                marks.append("*")
            else:
                marks.append(".")
        label = str(size - y).rjust(2)
        rows.append(f"{label}  " + " ".join(marks) + f"  {label}")
    rows.append(head)
    return "\n".join(rows)


def _point_list(points: list[int], size: int) -> str:
    return " ".join(to_gtp(p, size) for p in points) if points else "无"


def _legal_text(cells: list[int], color: int, size: int) -> str:
    legal = legal_moves(cells, color)
    if not legal:
        return "无"
    return " ".join(
        f"{to_gtp(p, size)}({flips})" for p, flips in sorted(legal.items())
    )


def _corner_owner(cells: list[int], size: int, color: int) -> str:
    owned = [to_gtp(p, size) for p in CORNERS if p < len(cells) and cells[p] == color]
    return " ".join(owned) if owned else "无"


def reversi_ranked_moves(state: dict[str, Any], seat: Seat) -> list[dict[str, Any]]:
    """单层贪心：位置权重 + 翻子数 + 确定性微扰；无合法点返回 None。"""
    size = int(state.get("size") or SIZE)
    cells = _cells_of(state)
    if len(cells) != size * size:
        return []
    legal = legal_moves(cells, color_of(seat))
    if not legal:
        return []
    moves = int(state.get("moves") or 0)
    ranked = []
    for p, flips in legal.items():
        score = WEIGHTS[p] + flips * 2 + ((p * 7 + moves) % 5) * 0.01
        ranked.append({"move": to_gtp(p, size), "score": score})
    return sorted(ranked, key=lambda item: -item["score"])


def reversi_bot(state: dict[str, Any], seat: Seat) -> str | None:
    ranked = reversi_ranked_moves(state, seat)
    return ranked[0]["move"] if ranked else None


def describe(state: dict[str, Any], seat: Seat, names: list[str]) -> str:
    """给模型的英文局面描述（与 rain-go 同风格，坐标按本插件行向）。"""
    size = int(state.get("size") or SIZE)
    cells = _cells_of(state)
    mine = color_of(seat)
    other = _other(mine)
    b, w = counts(cells)
    opponent_name = names[1 - seat] if 0 <= 1 - seat < len(names) else "Opponent"
    symbol = lambda c: "X (black)" if c == 1 else "O (white)"  # noqa: E731
    out = [
        f"You are {symbol(mine)}. {opponent_name} is {symbol(other)}. "
        f"Discs: black {b}, white {w}."
    ]
    last = state.get("last")
    if isinstance(last, int) and 0 <= last < len(cells):
        flipped = list(state.get("flipped") or [])
        out.append(
            f"Last move: {to_gtp(last, size)} flipped {len(flipped)}."
        )
    passed = state.get("passed")
    if passed in (1, 2):
        who = "You" if passed == mine else opponent_name
        out.append(f"{who} had no legal move and passed.")
    if not state.get("over") and state.get("toPlay") == mine:
        out.append(
            "Your legal moves (discs flipped): "
            + _legal_text(cells, mine, size)
        )
    out.append("")
    out.append(board_text(state, mine if state.get("toPlay") == mine else None))
    out.append(
        f"Coordinates: columns A-H left to right, row 1 at the bottom "
        f"(e.g. D6). Move syntax: {move_help()}."
    )
    if not state.get("over") and state.get("toPlay") == mine:
        out.append("(* marks your legal moves)")
    return "\n".join(out)


def move_help() -> str:
    return '"D6" (columns A-H left to right, row 1 at the bottom)'


def chat_context(
    state: dict[str, Any],
    viewer: Seat | None,
    names: list[str] | None = None,
) -> str | None:
    """聊天/行动通知用的完整盘面快照（纯事实、只读、不做推荐）。

    输出完整 8×8 盘、坐标规则、双方子数、当前行棋方及其合法点（点+翻子
    数）、最后一手与被迫过手事实。合法点只是「尚未落子」的事实描述，不是
    候选白名单或推荐下一手；身份行优先用 ``names[viewer]``（座位显示名），
    缺省写「你」，不假设固定座位号或玩家人数。
    """
    size = int(state.get("size") or SIZE)
    cells = _cells_of(state)
    if len(cells) != size * size:
        return None
    moves = int(state.get("moves") or 0)
    b, w = counts(cells)
    to_play = int(state.get("toPlay") or 1)
    if to_play not in (1, 2):
        to_play = 1

    if viewer is None:
        side_line = "执色：未提供座位，无法确定双方执色。"
    else:
        mine = color_of(viewer)
        other = _other(mine)
        viewer_name = "你"
        if names and isinstance(viewer, int) and 0 <= viewer < len(names):
            viewer_name = str(names[viewer] or "") or "你"
        side_line = (
            f"执色：{viewer_name}执 {SYMBOLS[mine]}，"
            f"对方执 {SYMBOLS[other]}。"
        )

    lines = [
        "黑白棋完整局面（8×8）。",
        (
            "坐标规则：列 A-H 从左到右，行从下往上 1-8（如 D6）；"
            "X=黑、O=白、*=当前行棋方合法点、.=空。"
        ),
        side_line,
        f"子数：黑（X）{b} 子，白（O）{w} 子；已落子 {moves} 手。",
    ]
    if state.get("over"):
        lines.append("对局已结束：双方都没有可落点（或一方棋子已归零）。")
    else:
        lines.append(
            f"当前行棋方：{COLOR_NAMES[to_play]}（{SYMBOLS[to_play]}）。"
        )
        lines.append(
            f"当前行棋方合法点（括号内为翻子数）："
            f"{_legal_text(cells, to_play, size)}。"
        )
    last = state.get("last")
    if isinstance(last, int) and 0 <= last < size * size:
        flipped = list(state.get("flipped") or [])
        flipped_text = (
            "翻掉 " + _point_list(sorted(flipped), size)
            if flipped
            else "未翻子"
        )
        lines.append(
            f"最后一手：{to_gtp(last, size)}（{flipped_text}）。"
        )
    passed = state.get("passed")
    if passed in (1, 2):
        lines.append(
            f"最近 pass：{COLOR_NAMES[passed]}无合法点，已自动过手"
            "（pass 不写入对局日志）。"
        )
    else:
        lines.append("最近 pass：无。")
    lines.extend(
        [
            "棋盘上的 X/O 是已经发生的落子；* 是当前行棋方尚未落子的合法点。"
            "以下只是事实描述，不是候选白名单或推荐下一手。",
            f"完整棋盘（来源：本轮 state.cells，已行 {moves} 手）：",
            board_text(state, None if state.get("over") else to_play),
        ]
    )
    return "\n".join(lines)


def threat_summary(state: dict[str, Any], seat: Seat) -> str:
    """给 game_agent 的当前盘面威胁摘要（纯规则，不做搜索/胜率评估）。

    只输出事实：双方子数、角位归属、双方机动性（合法落点数）与最近 pass。
    ``seat`` 仅用于标明哪一边是己方；不假设固定座位号，也不给推荐落点。
    """
    size = int(state.get("size") or SIZE)
    cells = _cells_of(state)
    mine = color_of(seat)
    other = _other(mine)
    b, w = counts(cells)
    lines: list[str] = []
    for color in (mine, other):
        who = "己方" if color == mine else "对方"
        legal = legal_moves(cells, color)
        parts = [
            f"子数 {b if color == 1 else w}",
            f"角位 {_corner_owner(cells, size, color)}",
            f"机动性（合法落点数）{len(legal)} 个",
            "合法落点坐标 " + (" ".join(to_gtp(p, size) for p in sorted(legal)) or "无"),
        ]
        lines.append(
            f"- {who}（{COLOR_NAMES[color]}）：" + "；".join(parts) + "。"
        )
    passed = state.get("passed")
    if passed in (1, 2):
        to_play = int(state.get("toPlay") or 1)
        lines.append(
            f"- 最近 pass：{COLOR_NAMES[passed]}无合法点已自动过手；"
            f"当前由{COLOR_NAMES.get(to_play, COLOR_NAMES[1])}行棋。"
        )
    else:
        lines.append("- 最近 pass：无。")
    lines.append(
        "以上仅当前盘面事实（子数/角位/机动性/pass），"
        "不是胜率或搜索评估，也不覆盖计划。"
    )
    return "\n".join(lines)


KEY_EVENT_NAMES = ("corner", "big_flip", "pass", "wipeout")


def key_events(
    state: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
    seat: int | None = None,
) -> dict[str, Any]:
    """确定性关键手信号：角位 / 大翻 / 被迫 pass / 吃光终局。

    只认「本手新增」：``before`` 是落子前局面时，要求手数递增、``last``
    变化且落点此前为空，才把本手的大翻/pass/角位/吃光计入；重复判定同一
    局面（``before == state``）或手数未推进时不会重复报 key。连续两手各自
    让对手 pass 仍是两次新事件（每手 moves 递增、last 不同）。``before=None``
    退化为现状诊断（离线/兼容用途）。``seat`` 只用于解析执色，不参与其它
    判断。
    """
    flags: dict[str, Any] = {name: False for name in KEY_EVENT_NAMES}
    flags["key"] = False
    size = int(state.get("size") or SIZE)
    cells = _cells_of(state)
    if len(cells) != size * size:
        return flags
    before_cells: list[int] | None = None
    if before is not None:
        raw = _cells_of(before)
        if len(raw) == size * size:
            before_cells = raw
    # 「本手新增」判据：有 before 时，只有 after 的手数/最后一手确实推进了
    # 才认新事件。``before == state``（重复判定同一局面）或手数倒退/不变
    # 都不算新落子——否则历史已有的大翻/pass 会在每次判定时重复刷 key。
    last = state.get("last")
    new_move = True
    if before_cells is not None:
        try:
            before_moves = int(before.get("moves") or 0)
        except (TypeError, ValueError):
            before_moves = 0
        try:
            moves_now = int(state.get("moves") or 0)
        except (TypeError, ValueError):
            moves_now = 0
        before_last = before.get("last")
        new_move = (
            moves_now > before_moves
            and isinstance(last, int)
            and 0 <= last < len(before_cells)
            and last != before_last
            and before_cells[last] == 0
        )

    if isinstance(last, int) and 0 <= last < size * size:
        if new_move and last in CORNERS:
            flags["corner"] = True
        flipped = list(state.get("flipped") or [])
        if new_move and len(flipped) >= BIG_FLIP:
            flags["big_flip"] = True

    # pass 是最后一手之后发生的事实：只有本手推进后才算新增。连续两手各自
    # 让对手 pass 时，每手都是新 pass（last 变化、moves 递增）。
    if new_move and state.get("passed") in (1, 2):
        flags["pass"] = True

    if state.get("over"):
        b, w = counts(cells)
        if b == 0 or w == 0:
            if before_cells is None:
                flags["wipeout"] = True
            else:
                bb, bw = counts(before_cells)
                if (b == 0 and bb > 0) or (w == 0 and bw > 0):
                    flags["wipeout"] = True

    flags["key"] = any(bool(flags[name]) for name in KEY_EVENT_NAMES)
    return flags


class Reversi(GameModule):
    def ranked_moves(self, state, seat):
        return reversi_ranked_moves(state, seat)

    kind = "reversi"
    name = {"zh": "黑白棋", "en": "Reversi"}
    family = "棋"
    blurb = "夹住对方就翻面，最后子多者胜。"
    ready = True
    players = PlayerSpec(min=2, max=2, default=2)
    options: list[OptionSpec] = []
    rules = (
        "Reversi (Othello) on an 8x8 board. Seat 0 plays black and moves first. "
        "A move must outflank at least one opposing disc in a straight line; "
        "all outflanked discs flip to your colour. If a player has no legal "
        "move they pass automatically (the pass is not a logged move); when "
        "neither side can move, or one side has no discs left, the game ends "
        "and the side with more discs wins (equal is a draw). Moves: "
        '"D6" (columns A-H left to right, row 1 is the bottom; letters are '
        "case-insensitive, e.g. D6 and d6 are the same square)."
    )
    move_help = move_help()
    supports_turn_driver = True

    def create(self, ctx: CreateContext) -> dict[str, Any]:
        return {
            "size": SIZE,
            "cells": initial_cells(),
            "toPlay": 1,
            "moves": 0,
            "flipped": [],
            "over": False,
        }

    def apply(self, state: dict[str, Any], seat: Seat, move: str) -> MoveResult:
        if state.get("over"):
            return MoveResultErr("对局已经结束")
        color = color_of(seat)
        if color != state.get("toPlay"):
            return MoveResultErr("还没轮到你")
        size = int(state.get("size") or SIZE)
        p = from_gtp(move, size)
        if p is None:
            return MoveResultErr(f"看不懂这步：{move}")
        cells = _cells_of(state)
        if p >= len(cells) or cells[p] != 0:
            return MoveResultErr("这里已经有子了")
        flips = flips_for(cells, p, color)
        if not flips:
            return MoveResultErr("这里不能下：要夹住对方的棋子")

        new_cells, flips = apply_move(cells, p, color)
        enemy = _other(color)
        b, w = counts(new_cells)
        over = b == 0 or w == 0
        to_play = enemy
        passed: int | None = None
        if not over and not legal_moves(new_cells, enemy):
            if legal_moves(new_cells, color):
                to_play = color
                passed = enemy
            else:
                over = True

        next_state: dict[str, Any] = {
            "size": size,
            "cells": new_cells,
            "toPlay": to_play,
            "moves": int(state.get("moves") or 0) + 1,
            "last": p,
            "flipped": flips,
            "over": over,
        }
        if passed is not None:
            next_state["passed"] = passed
        return MoveResultOk(next_state, to_gtp(p, size))

    def waiting_on(self, state: dict[str, Any]) -> list[Seat]:
        if state.get("over"):
            return []
        return [seat_of(int(state.get("toPlay") or 1))]

    def outcome(self, state: dict[str, Any]) -> Outcome | None:
        if not state.get("over"):
            return None
        b, w = counts(_cells_of(state))
        text = f"黑 {b} : 白 {w}"
        if b == w:
            return Outcome(winners=[], text=text)
        return Outcome(winners=[seat_of(1 if b > w else 2)], text=text)

    def seat_labels(self, state: dict[str, Any]) -> list[str]:
        return ["黑", "白"]

    def view(self, state: dict[str, Any], viewer: Seat | None) -> dict[str, Any]:
        legal: dict[str, int] = {}
        if not state.get("over"):
            legal = {
                str(p): flips
                for p, flips in legal_moves(
                    _cells_of(state), int(state.get("toPlay") or 1)
                ).items()
            }
        return {
            **state,
            "legal": legal,
            "you": None if viewer is None else color_of(viewer),
        }

    def describe(
        self, state: dict[str, Any], seat: Seat, names: list[str]
    ) -> str:
        return describe(state, seat, names)

    def plan_prompt(self, state: dict[str, Any], seat: Seat) -> str:
        """棋类策略指导：只提示角位/机动性取舍，不替代规则合法性。"""
        return (
            "黑白棋策略：优先抢角，避免把角位送给对手；在稳定子、"
            "机动性与翻子数之间取舍，任何策略都不得走非法点。"
        )

    def bot(self, state: dict[str, Any], seat: Seat) -> str | None:
        return reversi_bot(state, seat)

    def chat_context(
        self, state: dict[str, Any], viewer: Seat | None, names: list[str] | None = None
    ) -> str | None:
        return chat_context(state, viewer, names)

    def threat_summary(self, state: dict[str, Any], seat: Seat) -> str:
        return threat_summary(state, seat)

    def key_events(
        self,
        state: dict[str, Any],
        *,
        before: dict[str, Any] | None = None,
        seat: Seat | None = None,
    ) -> dict[str, Any]:
        return key_events(state, before=before, seat=seat)


reversi = Reversi()

__all__ = [
    "BIG_FLIP",
    "KEY_EVENT_NAMES",
    "Reversi",
    "board_text",
    "chat_context",
    "describe",
    "key_events",
    "legal_moves",
    "reversi",
    "reversi_bot",
    "threat_summary",
]
