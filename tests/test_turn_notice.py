"""Action-turn notice wording (``director.build_turn_notice``).

The notice is the **only** instruction the host model gets for its turn, so it
must spell out the action format of the bound game. A model that has to infer
"bid 1" from a generic "call the tool" line replies with plain text instead of
calling ``play_<match>``, and the turn then stalls until the watchdog fills it
with a rule-bot move.
"""
from __future__ import annotations

from types import SimpleNamespace

from director import build_nudge_notice, build_turn_notice
from engine import games
from engine.match import Match, SeatInfo


def _plugin(host_name: str = "汐祈（Seki）", agent_name: str = "nori"):
    return SimpleNamespace(
        host_agent_name=host_name,
        settings=SimpleNamespace(agent_name=agent_name),
    )


def _doudizhu_match():
    """A three-seat doudizhu match (human + two AI seats)."""
    module = games.get("doudizhu")
    ctx = SimpleNamespace(seed=1, options={"hands": "1"})
    state = module.create(ctx)
    seats = [
        SeatInfo(kind="human", name="Human", token=""),
        SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        SeatInfo(kind="ai", name="nori", token=""),
    ]
    return Match(
        id="m1", kind="doudizhu", created_at=0, updated_at=0, version=1,
        seats=seats, state=state,
    )


def test_doudizhu_notice_spells_out_bid_and_play_formats():
    text = build_turn_notice(
        _doudizhu_match(),
        seat=1,
        kind="nori_turn",
        move_count=0,
        tool_name="play_m1",
        plugin=_plugin(),
    )
    assert "play_m1" in text
    # Bid phase is the very first action: the formats must be literal.
    assert "bid 1" in text and "bid 2" in text and "bid 3" in text
    assert "pass" in text
    assert "牌面字符串" in text
    # The host agent is named, not the plugin persona.
    assert "汐祈（Seki）" in text


def test_gomoku_notice_keeps_its_coordinate_contract():
    module = games.get("gomoku")
    state = module.create(SimpleNamespace(seed=1, options={}))
    match = Match(
        id="m2", kind="gomoku", created_at=0, updated_at=0, version=1,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
    )
    text = build_turn_notice(
        match, seat=1, kind="nori_turn", move_count=1, tool_name="play_m2",
        plugin=_plugin(),
    )
    assert "play_m2" in text
    # The coordinate format comes from the game's own move_help / rules.
    assert "H8" in text
    assert "bid" not in text


def test_gomoku_subsequent_turn_notice_swaps_rules_for_a_pointer():
    """方案1：规则只随首条行动通知下发，后续回合换一行指针。

    规则是静态文本而每条通知都会逐字进会话历史——每轮重复会堆满滚动
    上下文窗口；盘面/动作格式/威胁分析仍是每轮的当前事实，必须保留。
    """
    module = games.get("gomoku")
    state = module.create(SimpleNamespace(seed=1, options={}))
    match = Match(
        id="m3", kind="gomoku", created_at=0, updated_at=0, version=1,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
    )
    first = build_turn_notice(
        match, seat=1, kind="nori_turn", move_count=1, tool_name="play_m3",
        plugin=_plugin(),
    )
    later = build_turn_notice(
        match, seat=1, kind="nori_turn", move_count=2, tool_name="play_m3",
        plugin=_plugin(), include_rules=False,
    )
    assert "规则（完整）" in first
    assert "规则（完整）" not in later
    assert "完整规则见本局首条行动通知" in later
    # Per-turn facts survive the slimming.
    assert "动作格式" in later and "H8" in later
    assert "完整局面" in later
    # The mandatory-tool guidance is untouched by the slimming.
    assert "play_m3" in later and "一次回复只调用一次" in later


def test_doudizhu_subsequent_turn_notice_keeps_action_formats():
    later = build_turn_notice(
        _doudizhu_match(),
        seat=1,
        kind="nori_turn",
        move_count=1,
        tool_name="play_m1",
        plugin=_plugin(),
        include_rules=False,
    )
    assert "规则（完整）" not in later
    assert "完整规则见本局首条行动通知" in later
    # The bid/play formats stay literal on every turn: a missing format is
    # what makes the model answer with plain text and stall the turn.
    assert "bid 1" in later and "pass" in later and "牌面字符串" in later


def test_nudge_notice_spells_out_the_call_syntax():
    """F2：催促通知必须给出具体的调用格式（含合法示例 move）。

    事件复盘：故障切换到 flash 级模型后，模型决策正确（叫3）却把结果
    写成正文。通知只说「调用工具」不够，要给出 move 参数的填写示例。
    """
    module = games.get("gomoku")
    state = module.create(SimpleNamespace(seed=1, options={}))
    match = Match(
        id="m4", kind="gomoku", created_at=0, updated_at=0, version=1,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
    )
    text = build_nudge_notice(match, seat=1, tool_name="play_m4", plugin=_plugin())
    assert "play_m4" in text
    assert 'move 参数填' in text
    assert "示例只展示格式" in text
    assert "只在正文里写结果不算行动" in text

    # Text-only variant (F1): names the exact mistake and keeps the syntax.
    corrective = build_nudge_notice(
        match, seat=1, tool_name="play_m4", plugin=_plugin(), text_only=True
    )
    assert "刚才只发了文字" in corrective
    assert "play_m4" in corrective and 'move 参数填' in corrective


def test_doudizhu_bid_nudge_example_is_a_bid_or_pass():
    text = build_nudge_notice(
        _doudizhu_match(), seat=1, tool_name="play_m1", plugin=_plugin()
    )
    # Bidding phase: the example must be a bid/pass form, never a card string.
    assert 'move 参数填 "bid' in text or 'move 参数填 "pass"' in text
