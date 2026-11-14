"""Action-turn notice wording (``director.build_turn_notice``).

The notice is the **only** instruction the host model gets for its turn, so it
must spell out the action format of the bound game. A model that has to infer
"bid 1" from a generic "call the tool" line replies with plain text instead of
calling ``play_<match>``, and the turn then stalls until the watchdog fills it
with a rule-bot move.
"""
from __future__ import annotations

from types import SimpleNamespace

from director import build_turn_notice
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
