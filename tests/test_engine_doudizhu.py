"""斗地主规则与隐私证据；全自动对局测试不冒充真实模型验收。"""

import copy
import hashlib
import json

import pytest
from engine.games.doudizhu import apply, create, doudizhu
from engine.games.doudizhu.cards import (
    deck,
    interpretations,
    parse_cards,
    plays,
    read_against,
)
from engine.types import MoveResultErr, MoveResultOk


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("3", "single"),
        ("33", "pair"),
        ("333", "triple"),
        ("3334", "triple1"),
        ("33344", "triple2"),
        ("34567", "straight"),
        ("334455", "pairs"),
        ("333444", "plane"),
        ("33344456", "plane1"),
        ("3334445566", "plane2"),
        ("333345", "four2"),
        ("33334455", "four4"),
        ("3333", "bomb"),
        ("BJRJ", "rocket"),
    ],
)
def test_combo_types(text, kind):
    cards = parse_cards(deck(), text)
    assert read_against(cards)["type"] == kind


@pytest.mark.parametrize("text", ["JQKA2", "333BJ RJ", "333444BJRJ", "3344", "3456"])
def test_invalid_combinations(text):
    assert not interpretations(parse_cards(deck(), text))


def test_parser_and_bombs():
    hand = deck()
    assert parse_cards(hand, "S3 3") == ["S3", "H3"]
    assert parse_cards(hand, "10JQKA") == parse_cards(hand, "T J Q K A")
    assert parse_cards(hand, "小王 大王") == ["BJ", "RJ"]
    with pytest.raises(ValueError):
        parse_cards(hand, "S3 S3")
    bomb = read_against(parse_cards(hand, "3333"))
    assert read_against(parse_cards(hand, "4444"), bomb)
    assert not read_against(parse_cards(hand, "22"), bomb)
    assert read_against(["BJ", "RJ"], bomb)


def test_bid_redeal_and_no_mutation():
    state = create(123)
    original = copy.deepcopy(state)
    for _ in range(3):
        result = apply(state, state["turn"], "pass")
        assert isinstance(result, MoveResultOk)
        state = result.state
    assert state["redeals"] == 1 and state["firstBidder"] == 1
    assert original == create(123)
    result = apply(state, 1, "bid 3")
    assert result.state["landlord"] == 1
    assert len(result.state["cards"][1]) == 20
    assert isinstance(apply(result.state, 1, "pass"), MoveResultErr)


def test_rain_go_deal_golden_seed_42():
    # 原样取自 rain-go 06e4b75 ddzCreate(42, 1)，node 原生 TS 执行生成。
    state = create(42)
    assert state["rng"] == -1711259677
    assert state["bottom"] == ["D8", "SJ", "SA"]
    assert state["cards"][0] == [
        "C3",
        "D3",
        "H5",
        "D5",
        "C6",
        "D6",
        "H7",
        "S8",
        "D9",
        "HJ",
        "SQ",
        "SK",
        "HK",
        "CK",
        "HA",
        "S2",
        "BJ",
    ]


@pytest.mark.parametrize(
    ("seed", "moves", "digest"),
    [
        (
            0,
            [
                "bid 3",
                "4 5 6 7 8 9 10 J Q K A",
                "pass",
                "pass",
                "3 3 3 6",
                "pass",
                "5 10 10 10",
                "pass",
                "pass",
                "4 4",
                "2 2",
                "pass",
                "pass",
                "BJ RJ",
                "pass",
                "pass",
                "J",
            ],
            "53c6c20eca8c6d7a5e965121e72be616fd40b96b16b8013e47422bf65ebb0fc3",
        ),
        (
            42,
            [
                "bid 1",
                "bid 3",
                "7 8 9 10 J Q K",
                "pass",
                "pass",
                "3 4 4 4",
                "pass",
                "5 K K K",
                "10 A A A",
                "pass",
                "pass",
                "J 2 2 2",
                "pass",
                "pass",
                "RJ",
            ],
            "1fd39d57cd053008c3ba5edd89a3115d390c9e5fb5a51169b83bde6ab85ab1f6",
        ),
        (
            123,
            [
                "bid 2",
                "pass",
                "pass",
                "4 4 5 5 6 6",
                "10 10 J J Q Q",
                "pass",
                "pass",
                "3 3",
                "pass",
                "9 9",
                "pass",
                "K K",
                "A A",
                "pass",
                "pass",
                "10 J Q K A",
                "pass",
                "pass",
                "3 2 2 2",
                "pass",
                "pass",
                "10",
            ],
            "1fe8cab73e7e282271d0896be5f5d2418a96b6704e7f9206786cd75f053b6875",
        ),
    ],
)
def test_rain_go_full_game_golden(seed, moves, digest):
    # rain-go ddzBot 生成的动作轨迹与终局完整状态 SHA256（含 RNG/所有出牌/积分）。
    state = create(seed)
    for move in moves:
        result = apply(state, state["turn"], move)
        assert isinstance(result, MoveResultOk)
        state = result.state
    assert state["phase"] == "over"
    raw = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(raw).hexdigest() == digest


def test_view_whitelist_and_hidden_rng():
    state = create(99)
    for seat in (None, 0, 1, 2, -1, True):
        view = doudizhu.view(state, seat)
        assert "cards" not in view and "rng" not in view
        assert view["bottom"] is None
        assert len(view["hand"]) == (
            17 if type(seat) is int and seat in range(3) else 0
        )
        assert state["bottom"] != view["bottom"]
    public = doudizhu.describe(state, None, ["人类", "诺里", "陪练"])
    assert "PRIVATE hand" not in public
    assert "Legal examples" not in public
    assert json.dumps(state)


def test_private_views_are_detached_from_authoritative_state():
    state = apply(create(99), 0, "bid 3").state
    original = copy.deepcopy(state)
    view = doudizhu.view(state, 0)
    view["hand"].clear()
    view["bottom"].clear()
    view["scores"][0] = 999
    if view["hints"]:
        view["hints"][0]["cards"].clear()
    assert state == original


def test_bot_does_not_peek_other_hands():
    state = apply(create(42), 0, "bid 3").state
    changed = copy.deepcopy(state)
    # 仅改其他玩家私牌（公开张数相同），当前席的决策不能受影响。
    changed["cards"][1], changed["cards"][2] = changed["cards"][2], changed["cards"][1]
    assert doudizhu.bot(state, 0) == doudizhu.bot(changed, 0)
    assert doudizhu.describe(state, 0, ["a", "b", "c"]) == doudizhu.describe(
        changed, 0, ["a", "b", "c"]
    )


def test_redeal_guard_and_two_pass_reset():
    state = create(8)
    state.update(redeals=2, bids=[{"seat": 0, "bid": 0}, {"seat": 1, "bid": 0}], turn=2)
    state["cards"][2] = ["S3", "S4", "S5"]
    assert doudizhu.bot(state, 2) == "bid 1"
    state = apply(create(8), 0, "bid 3").state
    state = apply(state, 0, state["cards"][0][0]).state
    for seat in (1, 2):
        state = apply(state, seat, "pass").state
    assert state["turn"] == 0 and state["trick"] is None and state["passes"] == 0
    assert isinstance(apply(state, 0, "pass"), MoveResultErr)


@pytest.mark.parametrize("seed", range(20))
def test_bot_full_match(seed):
    state = create(seed)
    for _ in range(500):
        if state["phase"] == "over":
            break
        seat = state["turn"]
        move = doudizhu.bot(state, seat)
        original = copy.deepcopy(state)
        result = apply(state, seat, move)
        assert isinstance(result, MoveResultOk), (seed, move, result)
        assert original == state
        state = result.state
        assert sum(state["scores"]) == 0
        if state["phase"] == "play":
            options = plays(
                state["cards"][state["turn"]],
                state["trick"]["combo"] if state["trick"] else None,
            )
            assert all(
                isinstance(apply(state, state["turn"], p["move"]), MoveResultOk)
                for p in options[:20]
            )
    assert state["phase"] == "over", seed
    assert len(state["results"]) == 1
    assert doudizhu.outcome(state) is not None


def test_spring_and_farmer_team():
    s = apply(create(4), 0, "bid 3").state
    s["cards"] = [["S3"], ["S4", "H4"], ["S5", "H5"]]
    result = apply(s, 0, "3").state
    assert result["results"][0]["spring"] is True
    assert result["scores"] == [12, -6, -6]
    s["cards"] = [["S3"], ["S4"], ["S5"]]
    s.update(turn=1, trick=None, playsBy=[1, 0, 0])
    result = apply(s, 1, "4").state
    assert result["results"][0]["antiSpring"] is True
    assert doudizhu.outcome(result).winners == [1, 2]


def test_six_hands_finish_rotate_and_restore():
    state = create(2026, 6)
    previous = state
    for _ in range(2000):
        if state["phase"] == "over":
            break
        move = doudizhu.bot(state, state["turn"])
        result = apply(state, state["turn"], move)
        assert isinstance(result, MoveResultOk)
        previous, state = state, json.loads(json.dumps(result.state))
        if state["handNo"] != previous["handNo"]:
            assert state["handNo"] == previous["handNo"] + 1
            assert state["firstBidder"] == (previous["firstBidder"] + 1) % 3
            assert state["phase"] == "bid"
            assert state["plays"] == []
    assert state["phase"] == "over"
    assert len(state["results"]) == 6 and sum(state["scores"]) == 0


def _play_state(cards, trick, landlord=0, turn=2):
    """构造一个出牌中局面：座位 0 地主、座位 1 搭档、座位 2 规则 bot。"""
    state = apply(create(3), 0, "bid 3").state
    state["cards"] = cards
    state.update(landlord=landlord, turn=turn, passes=0, trick=trick)
    return state


def _single_trick(seat=1, key=1):
    return {
        "seat": seat,
        "cards": ["S4"],
        "combo": {"type": "single", "key": key, "len": 1, "size": 1},
        "name": "单张 4",
        "n": 0,
    }


def _pair_trick(seat=1, key=2):
    return {
        "seat": seat,
        "cards": ["S5", "H5"],
        "combo": {"type": "pair", "key": key, "len": 1, "size": 2},
        "name": "对子 5",
        "n": 0,
    }


def test_bot_blocks_partner_single_when_landlord_has_one_card():
    """地主只剩一张时不能无条件让搭档：用最高单张顶住，防止地主直接走完。"""
    state = _play_state(
        [["D9"], ["S5", "S6"], ["S7", "S8", "H10", "CJ"]],
        _single_trick(),
    )
    assert doudizhu.bot(state, 2) == "J"


def test_bot_uses_bomb_to_block_when_landlord_has_one_card():
    """地主一张时炸弹不会被单张压过，能稳稳拿回出牌权。"""
    state = _play_state(
        [["D9"], ["S5", "S6"], ["S7", "H7", "D7", "C7", "H10"]],
        _single_trick(),
    )
    assert doudizhu.bot(state, 2) == "7 7 7 7"


def test_bot_blocks_partner_pair_with_bomb_when_landlord_has_two_cards():
    """地主两张可能是对子：搭档的对子会被直接压完，有炸弹就截住。"""
    state = _play_state(
        [["D3", "D4"], ["S5", "S6"], ["S7", "H7", "D7", "C7", "H10"]],
        _pair_trick(),
    )
    assert doudizhu.bot(state, 2) == "7 7 7 7"


def test_bot_still_lets_partner_lead_when_landlord_is_not_close():
    """地主还有牌时保留农民配合让牌：不抢搭档的牌权。"""
    state = _play_state(
        [["D3", "D4", "D5", "D6", "D7"], ["S5", "S6"], ["S7", "S8", "H10", "CJ"]],
        _single_trick(),
    )
    assert doudizhu.bot(state, 2) == "pass"


def test_bot_passes_partner_pair_when_landlord_has_one_card():
    """地主只有一张，无法压对子；搭档的对子已经够用，不必拆牌。"""
    state = _play_state(
        [["D9"], ["S5", "S6"], ["S7", "S8", "H10", "CJ"]],
        _pair_trick(),
    )
    assert doudizhu.bot(state, 2) == "pass"


def test_bot_passes_when_no_single_can_beat_partner_and_landlord_has_one_card():
    """搭档出 A（key 12），bot 最大单张只有 10：options 已过滤，应让牌而非提交非法动作。"""
    state = _play_state(
        [["D9"], ["SA"], ["S10", "S8"]],
        _single_trick(key=12),
    )
    assert doudizhu.bot(state, 2) == "pass"


def test_bot_does_not_waste_bomb_when_partner_plays_big_joker():
    """搭档已出大王，地主单张必过；不必为了顶住而浪费炸弹。"""
    state = _play_state(
        [["D9"], ["RJ", "S6"], ["S7", "H7", "D7", "C7", "H10"]],
        _single_trick(key=14),
    )
    assert doudizhu.bot(state, 2) == "pass"


def test_bot_does_not_waste_bomb_when_partner_plays_pair_of_twos():
    """搭档的对 2 已最大：地主两张里只有王炸能压，炸弹也压不过王炸，让牌。"""
    state = _play_state(
        [["D3", "D4"], ["S2", "H2"], ["S7", "H7", "D7", "C7", "H10"]],
        _pair_trick(key=13),
    )
    assert doudizhu.bot(state, 2) == "pass"
