"""game_agent 输入层：规则注入、计划位置与盘面威胁摘要。

移植自 nori ``tests/test_agent_inputs.py``：只保留与宿主无关的输入层断言
（威胁摘要数值、``agent_view_text`` 区块顺序、关闭威胁分析时不调用模块）。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from engine import (
    CreateContext,
    GameModule,
    MoveResultOk,
    NewMatchOptions,
    NewSeat,
    agent_view_text,
    apply_match_action,
    create_match,
    games,
)
from host.settings import Settings

_gomoku_module = importlib.import_module("engine.games.gomoku")


# 黑：H8/J8/K8/L8 活四；白：H10/J10/K10/L10 活四。双方都有即时成五点，
# 且轮到黑棋——适合验证双方视角与真实坐标，而不是文案占位。
_THREAT_MOVES = [
    (0, "H8"),
    (1, "H10"),
    (0, "J8"),
    (1, "J10"),
    (0, "K8"),
    (1, "K10"),
    (0, "L8"),
    (1, "L10"),
]


def _state_with(moves):
    mod = games.get("gomoku")
    state = mod.create(CreateContext(seed=1, players=2, options={}))
    for seat, move in moves:
        result = mod.apply(state, seat, move)
        assert isinstance(result, MoveResultOk), (seat, move, result)
        state = result.state
    return mod, state


def _match_with(moves):
    seats = [
        NewSeat(kind="human", token="tok0", name="甲"),
        NewSeat(kind="human", token="tok1", name="乙"),
    ]
    match = create_match(
        NewMatchOptions(id="m-view", kind="gomoku", seats=seats, seed=1, now=1000)
    )
    for i, (seat, move) in enumerate(moves):
        result = apply_match_action(
            match, seat, {"type": "move", "move": move}, 1001 + i
        )
        assert result.ok, result.error
        match = result.match
    return match


# ----------------------------------------------------------------------
#  威胁摘要：真实盘面、双方视角、准确坐标
# ----------------------------------------------------------------------
def test_threat_summary_real_board_both_perspectives() -> None:
    mod, state = _state_with(_THREAT_MOVES)

    black = mod.threat_summary(state, 0)
    assert "己方（黑）" in black and "对方（白）" in black
    assert "当前即可成五：G8 M8" in black
    assert "当前即可成五：G10 M10" in black
    assert "活四" in black
    assert "不等同严格双三" in black

    white = mod.threat_summary(state, 1)
    assert "己方（白）" in white and "对方（黑）" in white
    assert "当前即可成五：G10 M10" in white
    assert "当前即可成五：G8 M8" in white
    # 自己/对手的字段随座位翻转，而不是固定按黑方写死。
    assert white.index("己方（白）") < white.index("对方（黑）")
    assert black.index("己方（黑）") < black.index("对方（白）")


def test_single_live_three_reports_coordinates_and_extension_points() -> None:
    # 黑 H8/J8/K8 只有一条横向连续活三；摘要须给出三点坐标与两端延伸点，
    # 且不能把它说成多方向双活三/严格双三。
    mod, state = _state_with(
        [(0, "H8"), (1, "A1"), (0, "J8"), (1, "A2"), (0, "K8"), (1, "A3")]
    )
    text = mod.threat_summary(state, 0)

    assert "活三：H8 J8 K8（延伸 G8 L8）" in text
    assert "双活三信号" not in text
    assert "启发式多威胁信号" in text
    assert "不等同严格双三" in text


def test_crossing_live_threes_reports_double_live_three_signal() -> None:
    # 黑横向 H8/J8/K8 与纵向 J8/J9/J10 在 J8 交叉，两个方向各有活三。
    mod, state = _state_with(
        [
            (0, "H8"),
            (1, "A1"),
            (0, "J8"),
            (1, "A2"),
            (0, "K8"),
            (1, "A3"),
            (0, "J9"),
            (1, "A4"),
            (0, "J10"),
        ]
    )
    text = mod.threat_summary(state, 0)

    live_line = next(line for line in text.splitlines() if "活三：" in line)
    assert "H8 J8 K8" in live_line and "J8 J9 J10" in live_line
    assert (
        "双活三信号：2 个方向已有活三"
        "（未验证同一落点形成的严格双三）"
    ) in text


def test_separated_live_threes_only_report_signal_not_strict_double_three() -> None:
    # 横向 H8/J8/K8 与远处纵向 D2/D3/D4 各有活三，但并非同一落点/交叉点
    # 形成；只能报“双活三信号”，不能声称严格双三。
    mod, state = _state_with(
        [
            (0, "H8"),
            (1, "A1"),
            (0, "J8"),
            (1, "A3"),
            (0, "K8"),
            (1, "A5"),
            (0, "D2"),
            (1, "A7"),
            (0, "D3"),
            (1, "C1"),
            (0, "D4"),
        ]
    )
    text = mod.threat_summary(state, 0)

    live_line = next(line for line in text.splitlines() if "活三：" in line)
    assert "H8 J8 K8" in live_line and "D2 D3 D4" in live_line
    assert (
        "双活三信号：2 个方向已有活三"
        "（未验证同一落点形成的严格双三）"
    ) in text
    assert "严格双三：是" not in text
    assert "双三：是" not in text


def test_blocked_three_is_not_counted_as_live_three() -> None:
    # H8/J8/K8 两端相邻点 G8/L8 虽为空，但外侧 F8/M8 被白子封住，
    # 两端的延伸都只能成冲四、不能成活四，因此不计连续活三。
    mod, state = _state_with(
        [
            (0, "H8"),
            (1, "F8"),
            (0, "J8"),
            (1, "A1"),
            (0, "K8"),
            (1, "M8"),
        ]
    )
    text = mod.threat_summary(state, 0)

    assert "活三：H8 J8 K8" not in text
    assert "双活三信号" not in text
    assert "严格双三：是" not in text


def test_jump_three_reports_stones_and_fill_point() -> None:
    # 黑 H8/J8/L8：4 格窗口 H8 J8 K8 L8 内 3 子 1 中间空，补 K8 即成四。
    mod, state = _state_with(
        [(0, "H8"), (1, "A1"), (0, "J8"), (1, "A2"), (0, "L8"), (1, "A3")]
    )
    text = mod.threat_summary(state, 0)

    assert "跳三：H8 J8 L8（补 K8）" in text


def test_sleeping_four_reports_run_with_coordinates() -> None:
    # 黑 H8/J8/K8/L8 四连，白已堵 M8：只有 G8 可成五 -> 眠四。
    mod, state = _state_with(
        [
            (0, "H8"),
            (1, "A1"),
            (0, "J8"),
            (1, "A2"),
            (0, "K8"),
            (1, "A3"),
            (0, "L8"),
            (1, "M8"),
        ]
    )
    text = mod.threat_summary(state, 0)

    assert "眠四：H8 J8 K8 L8" in text
    assert "当前即可成五：G8" in text


def test_live_four_reports_run_with_coordinates() -> None:
    # 黑 H8/J8/K8/L8 四连两端开 -> 活四，两端都可成五。
    mod, state = _state_with(
        [
            (0, "H8"),
            (1, "A1"),
            (0, "J8"),
            (1, "A2"),
            (0, "K8"),
            (1, "A3"),
            (0, "L8"),
            (1, "A4"),
        ]
    )
    text = mod.threat_summary(state, 0)

    assert "活四：H8 J8 K8 L8" in text
    assert "当前即可成五：G8 M8" in text


def test_sleeping_three_reports_run_with_coordinates() -> None:
    # 黑 H8/J8/K8 三连，白堵 L8：只剩 G8 可延伸成冲四 -> 眠三（非活三）。
    mod, state = _state_with(
        [(0, "H8"), (1, "A1"), (0, "J8"), (1, "A2"), (0, "K8"), (1, "L8")]
    )
    text = mod.threat_summary(state, 0)

    assert "眠三：H8 J8 K8" in text
    assert "活三：H8 J8 K8" not in text


def test_diagonal_four_win_points_are_reported() -> None:
    # 纯斜四 F6/G7/H8/J9：补点 E5/K10 与棋子只有斜向相邻，正交 4 邻点
    # 剪枝曾把整条斜线漏成「无成五点」；8 邻点修复后成五点、活四都报坐标。
    mod, state = _state_with(
        [
            (0, "F6"),
            (1, "A1"),
            (0, "G7"),
            (1, "P15"),
            (0, "H8"),
            (1, "A15"),
            (0, "J9"),
        ]
    )
    text = mod.threat_summary(state, 0)

    assert "当前即可成五：K10 E5" in text
    assert "活四：F6 G7 H8 J9" in text


def test_diagonal_extension_points_enter_double_threat_heuristic() -> None:
    # 斜活三 G7/H8/J9 的两端延伸点 F6/K10 只有斜向邻子，既要作为延伸点
    # 输出，也须计入「可形成双成五威胁的落点」启发式（恰好 2 个）。
    mod, state = _state_with(
        [(0, "G7"), (1, "A1"), (0, "H8"), (1, "P15"), (0, "J9"), (1, "A15")]
    )
    text = mod.threat_summary(state, 0)

    assert "活三：G7 H8 J9（延伸 F6 K10）" in text
    assert "可形成双成五威胁的落点 2 个" in text


# ----------------------------------------------------------------------
#  user 输入结构：规则在前，plan 紧邻末尾威胁块
# ----------------------------------------------------------------------
def test_agent_view_text_rules_current_plan_then_threat() -> None:
    match = _match_with(_THREAT_MOVES)
    view = agent_view_text(
        match,
        0,
        plan_text="这局放水，别把活四堵死",
        plan_review=True,
    )

    # 规则与坐标说明来自游戏模块，进入 user 输入。
    assert "Rules:" in view
    assert "Freestyle gomoku" in view
    assert "Move syntax:" in view
    # 身份/座位/状态与原语义保留。
    assert "You are 甲 in seat 0" in view
    assert "Status: YOUR TURN" in view
    # 棋盘只在当前局面块出现一次（board_text 自带上下列头，但棋盘块不重复）。
    assert view.count("Moves so far:") == 1
    assert view.count("You are X (black)") == 1

    assert view.index("Rules:") < view.index("Current position:")
    assert view.index("Current position:") < view.index("Current plan:")
    assert view.index("Current plan:") < view.index("Game strategy:")
    assert view.index("Game strategy:") < view.index("Threat analysis")
    assert "Game strategy: 五子棋策略" in view

    # plan 与威胁块之间不再夹着任何其它区块。
    between = view[view.index("Current plan:") : view.index("Threat analysis")]
    assert "Current position:" not in between
    assert "Recent messages:" not in between
    assert "Rules:" not in between
    # 威胁分析在最后。
    assert "Threat analysis" in view[view.rfind("Current plan:") :]
    assert view.rstrip().endswith("。")

    # plan 是优先战术指令，放水也不丢。
    assert "这局放水，别把活四堵死" in view
    assert "即使偏离默认目标" in view
    assert "必要时可调整，但仍优先执行计划" in view


def test_agent_view_text_without_plan_still_ends_with_threat() -> None:
    match = _match_with([])
    view = agent_view_text(match, 1)

    assert "Current plan:" not in view
    # 未设定 plan 时只显示模块动态策略，不伪造 Current plan。
    assert "Game strategy: 五子棋策略" in view
    assert view.index("Game strategy:") < view.index("Threat analysis")
    assert "Threat analysis" in view
    assert view.rstrip().endswith("。")
    # 空盘也应有明确摘要，而不是空块。
    assert "暂无成五点/活四/眠四，也未检出活三、跳三、眠三" in view


def test_expired_plan_text_keeps_strategy_without_current_plan() -> None:
    # game_agent 对过期 plan 传入空 plan_text；此时策略说明仍应在，
    # 但不能出现假的 Current plan，review 标记也不应泄漏。
    view = agent_view_text(
        _match_with(_THREAT_MOVES), 0, plan_text="", plan_review=True
    )
    assert "Current plan:" not in view
    assert "必要时可调整" not in view
    assert "Game strategy: 五子棋策略" in view


def test_default_plan_prompt_is_game_agnostic() -> None:
    prompt = GameModule.plan_prompt(object(), {}, 0)
    assert "游戏策略" in prompt
    assert "下一次动作决策" in prompt
    assert "规则合法性" in prompt
    for word in ("每手", "胜率", "放水", "执色"):
        assert word not in prompt


def test_gomoku_overrides_plan_prompt_in_module() -> None:
    prompt = games.get("gomoku").plan_prompt({}, 0)
    assert "五子棋策略" in prompt
    assert "成五" in prompt and "挡五" in prompt


# ----------------------------------------------------------------------
#  关闭/缺失威胁能力：不显示且完全不调用
# ----------------------------------------------------------------------
def test_threat_analysis_disabled_never_calls_module(monkeypatch) -> None:
    match = _match_with(_THREAT_MOVES)
    match.mode_options["level"] = "beginner"
    calls: list[str] = []

    def _boom_analyze(*args, **kwargs):
        calls.append("analyze")
        raise AssertionError("threat analysis must not be called when disabled")

    def _boom_summary(*args, **kwargs):
        calls.append("threat_summary")
        raise AssertionError("threat summary must not be called when disabled")

    monkeypatch.setattr(_gomoku_module, "analyze", _boom_analyze)
    monkeypatch.setattr(_gomoku_module.Gomoku, "threat_summary", _boom_summary)

    view = agent_view_text(match, 0, threat_analysis=False)

    assert calls == []
    assert "Threat analysis" not in view
    assert "当前即可成五" not in view
    # 其余输入照常。
    assert "Rules:" in view and "Current position:" in view


def test_game_without_threat_summary_does_not_break(monkeypatch) -> None:
    import engine.match as match_module

    match = _match_with([])
    fake = SimpleNamespace(
        name={"en": "Dummy", "zh": "哑棋"},
        rules="Dummy rules only.",
        move_help='"A1"',
        seat_labels=lambda state: ["红", "蓝"],
        describe=lambda state, seat, names: "dummy board",
        outcome=lambda state: None,
        waiting_on=lambda state: [0],
    )
    monkeypatch.setattr(match_module.games, "get", lambda kind: fake)

    view = agent_view_text(match, 0, threat_analysis=True)

    assert "Dummy rules only." in view
    assert "dummy board" in view
    assert "Game strategy:" not in view
    assert "Threat analysis" not in view


# ----------------------------------------------------------------------
#  自定义 system 不影响规则：规则始终进 user 输入
# ----------------------------------------------------------------------
def test_rules_live_in_user_input_not_custom_system() -> None:
    cfg = Settings({"ai_agent_prompt": "CUSTOM SYSTEM ONLY"})
    assert "Freestyle gomoku" not in cfg.ai_agent_prompt

    view = agent_view_text(_match_with([]), 0)
    assert "Rules:" in view
    assert "Freestyle gomoku" in view


def test_doudizhu_agent_view_requires_same_reply_speech_and_public_talk():
    """牌类隐藏信息局：状态行明确要求同轮短话，并给出公开信息的正向许可。"""
    match = create_match(
        NewMatchOptions(
            id="dz-speech",
            kind="doudizhu",
            seed=7,
            now=1,
            seats=[
                NewSeat("human", "yoru"),
                NewSeat("ai", "蕾依"),
                NewSeat("bot", "小白"),
            ],
            mode_options={"lineup": "agent_bot"},
        )
    )
    bid = apply_match_action(match, 0, {"type": "move", "move": "bid 3"}, 2)
    played = apply_match_action(
        bid.match,
        0,
        {"type": "move", "move": bid.match.state["cards"][0][0]},
        3,
    )
    view = agent_view_text(played.match, 1, threat_analysis=True)
    assert "Status: YOUR TURN" in view
    assert "one or two short player-style lines" in view
    assert "do not reply with text only" in view
    assert "Table talk:" in view
    assert "never reveal your private hand" in view