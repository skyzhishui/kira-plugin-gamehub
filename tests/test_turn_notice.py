"""Action-turn notice wording (``director.build_turn_notice``).

The notice is header-only: it triggers the turn and carries the mandatory
``play_<match>`` call plus behavioral boundaries. Rules / board / move syntax
ride the per-request ``gamehub_context`` block instead
(``context.build_game_state_context``), so the persisted session history
never accumulates stale boards. The header must end with the fallback
pointer: if the injected block is missing, the model has to call
``state_<match>`` before acting.
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


def _gomoku_match(match_id: str, *, version: int = 1):
    module = games.get("gomoku")
    state = module.create(SimpleNamespace(seed=1, options={}))
    return Match(
        id=match_id, kind="gomoku", created_at=0, updated_at=0, version=version,
        state=state,
        seats=[
            SeatInfo(kind="human", name="Human", token=""),
            SeatInfo(kind="ai", name="汐祈（Seki）", token=""),
        ],
    )


def test_doudizhu_notice_is_header_only():
    text = build_turn_notice(
        _doudizhu_match(),
        seat=1,
        kind="nori_turn",
        move_count=0,
        tool_name="play_m1",
        plugin=_plugin(),
    )
    assert "play_m1" in text
    # The host agent is named, not the plugin persona.
    assert "汐祈（Seki）" in text
    # Call discipline and the private-card boundary stay in the header.
    assert "系统会替你随手打一张" in text
    assert "禁止透露自己的未出手牌" in text
    # Rules / move formats moved to the per-request injected block.
    assert "bid 1" not in text and "牌面字符串" not in text
    assert "规则（完整）" not in text
    assert "完整局面" not in text
    # The fallback pointer names the injected block and the bound state tool.
    assert "gamehub_context" in text and "state_m1" in text


def test_gomoku_notice_is_header_only():
    text = build_turn_notice(
        _gomoku_match("m2"),
        seat=1, kind="nori_turn", move_count=1, tool_name="play_m2",
        plugin=_plugin(),
    )
    assert "play_m2" in text
    assert "汐祈（Seki）" in text
    # The mandatory-tool guidance survives the slimming.
    assert "一次回复只调用一次" in text
    assert "注意你是棋手，不是解说" in text
    # Board / coordinate content lives in the injected block now.
    assert "H8" not in text
    assert "规则（完整）" not in text
    assert "完整局面" not in text
    assert "gamehub_context" in text and "state_m2" in text


def test_every_turn_notice_has_the_same_header_shape():
    """规则/盘面不再区分首条与后续：每条通知都是同一形态的纯回合头。

    通知逐字持久化进滚动历史——首条带规则、后续带指针的方案已废弃；
    规则与盘面改由每轮请求的 gamehub_context 注入块承载（persist=False）。
    """
    first = build_turn_notice(
        _gomoku_match("m3", version=1),
        seat=1, kind="nori_turn", move_count=1, tool_name="play_m3",
        plugin=_plugin(),
    )
    later = build_turn_notice(
        _gomoku_match("m3", version=3),
        seat=1, kind="nori_turn", move_count=3, tool_name="play_m3",
        plugin=_plugin(),
    )
    for text in (first, later):
        assert "play_m3" in text
        assert "gamehub_context" in text and "state_m3" in text
        assert "规则（完整）" not in text
        assert "完整局面" not in text
        assert "一次回复只调用一次" in text
    # Only the trigger facts differ between the two turns.
    assert "刚落子" in first and "刚落子" in later


def test_delegated_notice_keeps_its_wording():
    text = build_turn_notice(
        _gomoku_match("m5"),
        seat=0, kind="delegated_human", move_count=2, tool_name="play_m5",
        plugin=_plugin(),
    )
    assert "play_m5" in text
    assert "已把这一局的人类座位委托给你" in text
    assert "不要代替对手落子" in text
    assert "gamehub_context" in text and "state_m5" in text


def test_nudge_notice_spells_out_the_call_syntax():
    """F2：催促通知必须给出具体的调用格式（含合法示例 move）。

    事件复盘：故障切换到 flash 级模型后，模型决策正确（叫3）却把结果
    写成正文。通知只说「调用工具」不够，要给出 move 参数的填写示例。
    """
    text = build_nudge_notice(
        _gomoku_match("m4"), seat=1, tool_name="play_m4", plugin=_plugin()
    )
    assert "play_m4" in text
    assert 'move 参数填' in text
    assert "示例只展示格式" in text
    assert "只在正文里写结果不算行动" in text

    # Text-only variant (F1): names the exact mistake and keeps the syntax.
    corrective = build_nudge_notice(
        _gomoku_match("m4"), seat=1, tool_name="play_m4", plugin=_plugin(),
        text_only=True,
    )
    assert "刚才只发了文字" in corrective
    assert "play_m4" in corrective and 'move 参数填' in corrective


def test_doudizhu_bid_nudge_example_is_a_bid_or_pass():
    text = build_nudge_notice(
        _doudizhu_match(), seat=1, tool_name="play_m1", plugin=_plugin()
    )
    # Bidding phase: the example must be a bid/pass form, never a card string.
    assert 'move 参数填 "bid' in text or 'move 参数填 "pass"' in text
