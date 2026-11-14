"""黑白棋规则 golden（坐标按本插件行 1 在底部；括号内为 rain-go 行号）。

坐标变换助手 ``rg``：rain-go 行 1 在顶部，本插件行 1 在底部，
``our_row = 9 - rain_row``；列字母相同，所以 ``d3`` → ``D6``。
测试里逐条标注两套坐标，避免手抄坐标假绿。
"""

from __future__ import annotations

import json
import re

from engine import CreateContext, MoveResultErr, MoveResultOk, games
from engine.coords import to_gtp


def _mod():
    return games.get("reversi")


def rg(name: str) -> str:
    """rain-go 坐标 → 本插件坐标（行上下翻转）。

    注意：rain-go ``d3``（行 1 在顶部）对应本插件 ``D6``；但 rain-go
    golden 序列里的落点若已在 rain-go 棋盘上验证过，逐手 ``rg`` 后在本插件
    棋盘上**同样成立**（棋盘索引一致，只是显示行号翻转）。调用方务必确认
    传入的是 rain-go 坐标，不是本插件坐标。
    """
    m = re.fullmatch(r"\s*([a-hA-H])\s*([1-8])\s*", name)
    assert m, name
    return f"{m.group(1).upper()}{9 - int(m.group(2))}"


def rg_seq(text: str) -> list[str]:
    """rain-go 棋谱（空格分隔，如 ``d6 c6 ...``）→ 本插件坐标序列。"""
    return [rg(move) for move in text.split()]


def _initial():
    return _mod().create(CreateContext(seed=1, players=2, options={}))


def _replay(seq: list[str]):
    """按当前行棋方依次落子（自动 pass 时不改座位）；返回最终 state。"""
    mod = _mod()
    state = _initial()
    for index, move in enumerate(seq):
        seat = int(state["toPlay"]) - 1
        result = mod.apply(state, seat, move)
        assert isinstance(result, MoveResultOk), (index, move, getattr(result, "error", ""))
        state = result.state
    return state


def _legal_names(state, color):
    if color == state["toPlay"]:
        return sorted(to_gtp(int(p), 8) for p in _mod().view(state, None)["legal"])
    return sorted(
        to_gtp(p, 8) for p in _legal_map(state["cells"], color)
    )


def _legal_map(cells, color):
    from engine.games.reversi.rules import legal_moves

    return legal_moves(cells, color)


# ----------------------------------------------------------------------
#  初始局面 / 落子翻转
# ----------------------------------------------------------------------
def test_initial_board_and_four_legal_points():
    state = _initial()
    assert state["size"] == 8
    assert len(state["cells"]) == 64
    assert state["toPlay"] == 1 and state["moves"] == 0
    assert state["over"] is False
    # rain-go d3/c4/f5/e6 → 本插件 D6/C5/F4/E3
    assert _legal_names(state, 1) == sorted(["D6", "C5", "F4", "E3"])
    assert _legal_names(state, 1) == sorted([rg(x) for x in ("d3", "c4", "f5", "e6")])
    # 黑子 E5/D4，白子 D5/E4（本插件行号）
    assert [to_gtp(p, 8) for p in (28, 35)] == ["E5", "D4"]
    assert [to_gtp(p, 8) for p in (27, 36)] == ["D5", "E4"]


def test_first_move_flips_one_disc_and_switches_turn():
    mod = _mod()
    state = _initial()
    result = mod.apply(state, 0, "D6")  # rain-go d3
    assert isinstance(result, MoveResultOk)
    assert result.log == "D6"
    assert result.state["flipped"] == [27]  # 翻 D5（rain-go d4）
    assert result.state["moves"] == 1
    assert result.state["toPlay"] == 2
    assert result.state["last"] == 19
    assert state["cells"][19] == 0  # 输入不被变异
    assert result.state["cells"][19] == 1 and result.state["cells"][27] == 1


# ----------------------------------------------------------------------
#  八方向 + 边界不越行
# ----------------------------------------------------------------------
def _eight_direction_state():
    """黑子落在中心 C3（本插件；rain-go c6），八个方向各一白、端点各一黑。"""
    cells = [0] * 64
    center = 8 * 5 + 2  # x=2,y=5 → C3
    dirs = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))
    expect = []
    for dx, dy in dirs:
        x, y = 2, 5
        enemy = (y + dy) * 8 + (x + dx)
        end = (y + 2 * dy) * 8 + (x + 2 * dx)
        cells[enemy] = 2
        cells[end] = 1
        expect.append(enemy)
    return {
        "size": 8,
        "cells": cells,
        "toPlay": 1,
        "moves": 0,
        "flipped": [],
        "over": False,
    }, expect, center


def test_all_eight_directions_flip():
    mod = _mod()
    state, expect, center = _eight_direction_state()
    result = mod.apply(state, 0, to_gtp(center, 8))
    assert isinstance(result, MoveResultOk)
    assert sorted(result.state["flipped"]) == sorted(expect)
    assert all(result.state["cells"][p] == 1 for p in expect)


def test_edge_wrap_is_blocked():
    """行边缘不环绕：H5 落黑不能把 A4 的白与 B4 的黑连起来。"""
    from engine.games.reversi.rules import flips_for

    cells = [0] * 64
    right_edge = 8 * 3 + 7  # H5（本插件）
    cells[right_edge + 1] = 2  # A4
    cells[right_edge + 2] = 1  # B4
    assert flips_for(cells, right_edge, 1) == []

    cells = [0] * 64
    left_edge = 8 * 4  # A4（本插件）
    cells[left_edge - 1] = 2  # H5
    cells[left_edge - 2] = 1  # G5
    assert flips_for(cells, left_edge, 1) == []


# ----------------------------------------------------------------------
#  非法 / 轮次 / 终局后
# ----------------------------------------------------------------------
def test_illegal_moves_do_not_change_state():
    mod = _mod()
    state = _initial()
    before = json.loads(json.dumps(state))
    cases = [
        (1, "D6", "还没轮到你"),
        (0, "D5", "已经有子"),
        (0, "A1", "要夹住"),
        (0, "I9", "看不懂"),
        (0, "ZZ", "看不懂"),
        (0, "", "看不懂"),
    ]
    for seat, move, marker in cases:
        result = mod.apply(state, seat, move)
        assert isinstance(result, MoveResultErr), (seat, move)
        assert marker in result.error
    assert state == before  # 输入未被变异


def test_moves_after_game_over_are_rejected():
    state = _replay([rg(m) for m in "e6 f4 e3 f6 g5 d6 e7 f5 c5".split()])
    assert state["over"] is True
    result = _mod().apply(state, 1, "A1")
    assert isinstance(result, MoveResultErr) and "结束" in result.error


PASS_TWICE_SEQ = (
    "F4 F5 C6 D3 D6 B7 E3 F3 G5 F6 G4 C3 G6 H6 C4 D7 C2 B2 G2 B1 E6 G3 "
    "F2 F1 G7 H5 D8 G8 C1 H4 A1 E7 D2 B4 H8 H3 E2 E1 B5 A2 F7 C8 H2 A4 A3 "
    "F8 H1 C5 A5 G1 C7 B3 B6 A7 A6 B8 E8"
).split()


# ----------------------------------------------------------------------
#  自动 pass / 双停终局 / 吃光终局 / 平局
# ----------------------------------------------------------------------
def test_pass_sequence_keeps_same_seat_and_does_not_log():
    # rain-go 序列 d6 c6 b6 b7 f4 a6 a8 c8 → 本插件行翻转
    seq = "D6 C6 B6 B7 F4 A6 A8 C8".split()
    state = _replay(seq)
    assert state["passed"] == 1  # 黑无合法点被迫过手
    assert state["toPlay"] == 2
    assert state["over"] is False
    assert state["moves"] == len(seq)  # pass 不计手数、不进 log
    assert _mod().waiting_on(state) == [1]


def test_double_stop_ends_with_wipeout():
    seq = [rg(m) for m in "e6 f4 e3 f6 g5 d6 e7 f5 c5".split()]
    state = _replay(seq)
    assert state["over"] is True
    assert state["cells"].count(1) == 13 and state["cells"].count(2) == 0
    outcome = _mod().outcome(state)
    assert outcome.winners == [0]
    assert outcome.text == "黑 13 : 白 0"
    assert _mod().waiting_on(state) == []


def test_pass_retained_when_same_side_moves_again():
    """白 pass 后黑再落子，若白仍无合法点则 pass 事实保持（连走两轮）。"""
    state = _replay(PASS_TWICE_SEQ)
    assert state["passed"] == 2 and state["toPlay"] == 1
    mod = _mod()
    result = mod.apply(state, 0, "A8")
    assert isinstance(result, MoveResultOk)
    assert result.state["passed"] == 2  # 白依旧无合法点：继续过手
    assert result.state["toPlay"] == 1  # 黑再走一手（同座位连走）
    assert result.state["over"] is False
    assert result.state["moves"] == state["moves"] + 1


def test_key_events_consecutive_passes_each_report_new():
    """同一座位连续两手各自让对手 pass：每手都应报新的 pass 事件。"""
    base = _replay(PASS_TWICE_SEQ)
    s1 = _mod().apply(base, 0, "A8").state
    s2 = _mod().apply(s1, 0, "H7").state
    assert s1["passed"] == 2 and s2["passed"] == 2
    assert s2["moves"] == s1["moves"] + 1
    assert _mod().key_events(s1, before=base, seat=0)["pass"] is True
    assert _mod().key_events(s2, before=s1, seat=0)["pass"] is True


def test_key_events_does_not_repeat_when_before_is_same_state():
    """before == state（没有新落子）不得重复报 pass/big_flip/corner/key。"""
    base = _replay(PASS_TWICE_SEQ)
    after = _mod().apply(base, 0, "A8").state
    assert _mod().key_events(after, before=base, seat=0)["pass"] is True
    repeat = _mod().key_events(after, before=after, seat=0)
    assert repeat["pass"] is False
    assert repeat["big_flip"] is False
    assert repeat["corner"] is False
    assert repeat["key"] is False


def test_key_events_big_flip_not_repeated_without_new_move():
    cells = [0] * 64
    for p in range(1, 6):  # B8-F8 五颗白子
        cells[p] = 2
    cells[6] = 1  # G8 黑
    before = {
        "size": 8,
        "cells": cells,
        "toPlay": 1,
        "moves": 10,
        "last": 63,
        "flipped": [],
        "over": False,
    }
    after = _mod().apply(before, 0, "A8").state
    assert len(after["flipped"]) == 5
    flags = _mod().key_events(after, before=before, seat=0)
    assert flags["big_flip"] is True and flags["corner"] is True
    repeat = _mod().key_events(after, before=after, seat=0)
    assert repeat["big_flip"] is False and repeat["corner"] is False
    assert repeat["wipeout"] is False and repeat["key"] is False


def test_draw_outcome_text():
    mod = _mod()
    # 终局 32:32：outcome 只依赖 state，构造终局盘面验证和局文案。
    draw = {
        "size": 8,
        "cells": [1] * 32 + [2] * 32,
        "toPlay": 1,
        "moves": 60,
        "flipped": [],
        "over": True,
    }
    outcome = mod.outcome(draw)
    assert outcome is not None and outcome.winners == []
    assert outcome.text == "黑 32 : 白 32"


# ----------------------------------------------------------------------
#  bot / view / describe / chat_context / threat / key_events
# ----------------------------------------------------------------------
def test_bot_returns_legal_move_deterministically_and_prefers_corner():
    mod = _mod()
    state = _initial()
    move = mod.bot(state, 0)
    assert isinstance(mod.apply(state, 0, move), MoveResultOk)
    assert mod.bot(state, 0) == move  # 同局面确定性
    empty = {
        "size": 8,
        "cells": [0] * 64,
        "toPlay": 1,
        "moves": 0,
        "flipped": [],
        "over": False,
    }
    assert mod.bot(empty, 0) is None  # 无合法点

    corner_state = {
        "size": 8,
        "cells": [0] * 64,
        "toPlay": 1,
        "moves": 10,
        "flipped": [],
        "over": False,
    }
    corner_state["cells"][1] = 2  # B8
    corner_state["cells"][2] = 1  # C8
    corner_state["cells"][8] = 2  # A7
    # A8（角位，本插件 p=0）可翻 B8 → 角位权重最高
    assert mod.bot(corner_state, 0) == "A8"


def test_bot_plays_whole_game_to_end():
    mod = _mod()
    state = _initial()
    for _ in range(200):
        if state["over"]:
            break
        seat = int(state["toPlay"]) - 1
        move = mod.bot(state, seat)
        assert move
        result = mod.apply(state, seat, move)
        assert isinstance(result, MoveResultOk), (move, result.error)
        state = result.state
    assert state["over"] is True
    assert mod.outcome(state) is not None


def test_view_legal_you_and_json():
    mod = _mod()
    state = _initial()
    view = mod.view(state, 0)
    assert view["you"] == 1
    assert set(view["legal"]) == {"19", "26", "37", "44"}
    assert all(v == 1 for v in view["legal"].values())
    assert mod.view(state, None)["you"] is None
    assert mod.view(state, 1)["you"] == 2
    json.dumps(view, ensure_ascii=False)
    over = _replay([rg(m) for m in "e6 f4 e3 f6 g5 d6 e7 f5 c5".split()])
    assert mod.view(over, 0)["legal"] == {}


def test_describe_and_chat_context_facts_without_recommendation():
    mod = _mod()
    state = _replay("D6 C6 B6 B7 F4 A6 A8 C8".split())
    text = mod.describe(state, 1, ["玩家", "诺里"])
    assert "You are O (white)" in text
    assert "Discs: black" in text
    assert "Last move:" in text
    assert "row 1 at the bottom" in text

    ctx = mod.chat_context(state, 1, ["玩家", "诺里"])
    assert "黑白棋完整局面（8×8）" in ctx
    assert "行从下往上 1-8" in ctx
    assert "子数：黑（X）" in ctx
    assert "pass" in ctx and "黑无合法点" in ctx
    assert "合法点" in ctx
    assert "不是候选白名单或推荐下一手" in ctx  # 明确边界，不给实际推荐
    for word in ("建议下一手", "最佳落点"):
        assert word not in ctx
    # 完整 8 行棋盘
    assert ctx.count("\n") > 10


def test_threat_summary_is_factual_not_search():
    mod = _mod()
    state = _initial()
    text = mod.threat_summary(state, 0)
    assert "己方（黑）" in text and "对方（白）" in text
    assert "机动性" in text and "角位" in text
    assert "不是胜率或搜索评估" in text
    for word in ("建议下", "推荐"):
        assert word not in text


def test_key_events_only_when_new_this_move():
    mod = _mod()
    before = _initial()
    after = mod.apply(before, 0, "D6").state
    flags = mod.key_events(after, before=before, seat=0)
    assert set(flags) >= {"corner", "big_flip", "pass", "wipeout", "key"}
    assert flags["corner"] is False and flags["big_flip"] is False
    assert flags["pass"] is False and flags["wipeout"] is False
    assert flags["key"] is False

    # pass 手
    pass_state = _replay("D6 C6 B6 B7 F4 A6 A8 C8".split())
    flags = mod.key_events(pass_state, before=None, seat=0)
    assert flags["pass"] is True and flags["key"] is True

    # 角位手：B8/A7 白、C8 黑，黑下 A8 拿角
    cells = [0] * 64
    cells[1] = 2
    cells[2] = 1
    cells[8] = 2
    before = {"size": 8, "cells": cells, "toPlay": 1, "moves": 10, "flipped": [], "over": False}
    after = mod.apply(before, 0, "A8").state
    flags = mod.key_events(after, before=before, seat=0)
    assert flags["corner"] is True

    # 吃光终局
    wipe = _replay([rg(m) for m in "e6 f4 e3 f6 g5 d6 e7 f5 c5".split()])
    flags = mod.key_events(wipe, before=None, seat=0)
    assert flags["wipeout"] is True and flags["key"] is True


def test_rules_use_plugin_orientation_and_case_example():
    """规则不得把 rain-go 映射写成运行时等价（D6 ≠ d3）。"""
    rules = _mod().rules
    assert "D6" in rules and "d6" in rules
    assert "d3" not in rules
    assert "case-insensitive" in rules
    assert "row 1 is the bottom" in rules
    assert "d3" not in _mod().move_help
    text = _mod().describe(_initial(), 0, ["甲", "乙"])
    assert "row 1 at the bottom" in text and "d3" not in text


def test_module_declares_turn_driver_and_registry_entry():
    mod = _mod()
    assert mod.supports_turn_driver is True
    assert mod.kind == "reversi" and mod.name["zh"] == "黑白棋"
    assert games.get("gomoku").supports_turn_driver is True
    entry = next(g for g in games.catalog() if g["kind"] == "reversi")
    assert entry["supports_turn"] is True
    assert entry["has_bot"] is True
    assert entry["has_chat_context"] is True
