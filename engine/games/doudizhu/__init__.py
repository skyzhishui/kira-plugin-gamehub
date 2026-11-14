"""三席叫分斗地主：规则/视角/文本边界，状态机对齐 rain-go 06e4b75。"""

from __future__ import annotations

import copy
import re
from typing import ClassVar

from ...rng import shuffled
from ...types import (
    CreateContext,
    GameModule,
    MoveResultErr,
    MoveResultOk,
    OptionSpec,
    Outcome,
    PlayerSpec,
)
from .cards import (
    combo_name,
    deck,
    move_text,
    parse_cards,
    plays,
    read_against,
    sort_cards,
)

PASS_WORDS = {"pass", "p", "不要", "不出", "要不起", "过", "pass.", "不叫", "no"}
RULES = """Dou Dizhu for exactly 3 seats. Each deal: 17 cards per player and 3 hidden bottom cards.
Rank order: 3 < 4 < ... < 10 < J < Q < K < A < 2 < BJ (small joker) < RJ (big joker).
Bidding: in seat order each seat bids once, 1/2/3 strictly above current bid, or pass.
Bid 3 immediately ends bidding. Highest bidder becomes landlord and takes the revealed bottom cards.
All pass: redeal with the next first bidder. Landlord plays first; the other two farmers are a team.
Combinations: single/pair/triple/triple+single/triple+pair; straight of 5+; consecutive pairs of 3+;
plane of 2+ triples, optionally with one single or pair per triple; four+two singles/four+two pairs;
bomb of four; rocket BJ RJ. Chains contain only 3..A. Wings never share a main rank or contain both jokers.
Rocket beats everything; bombs beat non-bombs; otherwise match type, length and size with higher main rank.
You may pass when following, never when leading. Two consecutive passes clear the trick; its owner leads.
First empty hand wins: landlord alone, or both farmers. Score=bid*2^bombs, doubled for spring/anti-spring.
Spring: farmers made zero plays. Anti-spring: landlord made only its first play. Landlord pays/receives
twice the hand score; each farmer receives/pays once. Scores accumulate for 1,3,6 hands.
Moves: bidding "bid 1", "bid 2", "bid 3", "pass"; playing "3 3 3 4", "10 J Q K A", "BJ RJ", "pass".
Suits optional (S3 H3); rank-only picks your own cards deterministically. Never claim to know hidden hands.
Private hand and strategy must not be repeated in public table speech. Only public actions and counts may be discussed publicly."""


def deal(s: dict, hand_no: int, first: int, redeals: int) -> dict:
    cards, rng = shuffled(deck(), s["rng"])
    return {
        **s,
        "rng": rng,
        "handNo": hand_no,
        "firstBidder": first,
        "redeals": redeals,
        "cards": [sort_cards(cards[i * 17 : (i + 1) * 17]) for i in range(3)],
        "bottom": sort_cards(cards[51:]),
        "phase": "bid",
        "bids": [],
        "bid": 0,
        "bidder": None,
        "landlord": None,
        "turn": first,
        "trick": None,
        "passes": 0,
        "plays": [],
        "bombs": 0,
        "playsBy": [0, 0, 0],
    }


def create(seed: int, hands: int = 1) -> dict:
    return deal(
        {"rng": seed & 0xFFFFFFFF, "hands": hands, "scores": [0, 0, 0], "results": []},
        1,
        0,
        0,
    )


def parse_bid(text: str) -> int | None:
    found = re.fullmatch(
        r"(?:bid|叫|call)?\s*([123一二三])\s*(?:分|points?)?", text.strip().lower()
    )
    if not found:
        return None
    return {"一": 1, "二": 2, "三": 3}.get(
        found[1], int(found[1]) if found[1].isdigit() else 0
    )


def legal_bids(s):
    return [0] + [b for b in (1, 2, 3) if b > s["bid"]]


def become_landlord(s, seat):
    s["cards"][seat] = sort_cards(s["cards"][seat] + s["bottom"])
    s.update(landlord=seat, phase="play", turn=seat, trick=None, passes=0)


def finish_hand(s, winner):
    landlord = s["landlord"]
    won = winner == landlord
    spring = won and sum(n for i, n in enumerate(s["playsBy"]) if i != landlord) == 0
    anti = not won and s["playsBy"][landlord] == 1
    score = s["bid"] * 2 ** s["bombs"] * (2 if spring or anti else 1)
    deltas = [
        (2 if won else -2) * score if i == landlord else (-1 if won else 1) * score
        for i in range(3)
    ]
    s["results"].append(
        {
            "hand": s["handNo"],
            "landlord": landlord,
            "winner": winner,
            "landlordWon": won,
            "bid": s["bid"],
            "bombs": s["bombs"],
            "spring": spring,
            "antiSpring": anti,
            "score": score,
            "deltas": deltas,
        }
    )
    s["scores"] = [a + b for a, b in zip(s["scores"], deltas)]
    note = (
        ("地主胜" if won else "农民胜")
        + (" · 春天" if spring else " · 反春" if anti else "")
        + f" · {score} 分"
    )
    if s["handNo"] >= s["hands"]:
        s.update(phase="over", trick=None, passes=0)
    else:
        s = deal(s, s["handNo"] + 1, (s["firstBidder"] + 1) % 3, 0)
    return s, note


def apply(s, seat, move):
    if s["phase"] == "over":
        return MoveResultErr("对局已经结束")
    if type(seat) is not int or seat not in range(3):
        return MoveResultErr("没有这个座位")
    if seat != s["turn"]:
        return MoveResultErr("还没轮到你")
    if not isinstance(move, str) or not move.strip():
        return MoveResultErr("没有写动作")
    text = move.strip()
    passed = text.lower() in PASS_WORDS
    out = copy.deepcopy(s)
    if s["phase"] == "bid":
        bid = 0 if passed else parse_bid(text)
        if bid is None:
            return MoveResultErr("还在叫分：叫 1、2、3 分或不叫")
        if bid and bid <= s["bid"]:
            return MoveResultErr(f"要叫得比 {s['bid']} 分高")
        out["bids"].append({"seat": seat, "bid": bid})
        if bid:
            out.update(bid=bid, bidder=seat)
        log = f"叫 {bid} 分" if bid else "不叫"
        if bid == 3:
            become_landlord(out, seat)
            log += " · 当地主"
        elif len(out["bids"]) >= 3:
            if out["bidder"] is None:
                out = deal(
                    out, s["handNo"], (s["firstBidder"] + 1) % 3, s["redeals"] + 1
                )
                log += " · 都不叫，重新发牌"
            else:
                become_landlord(out, out["bidder"])
                log += f" · 地主：座位 {out['landlord'] + 1}"
        else:
            out["turn"] = (seat + 1) % 3
        return MoveResultOk(out, log)
    if passed:
        if not s["trick"]:
            return MoveResultErr("该你先出，不能不要")
        act = {
            "seat": seat,
            "cards": None,
            "combo": None,
            "name": "不要",
            "n": len(s["plays"]),
        }
        out["plays"].append(act)
        out["passes"] += 1
        if out["passes"] >= 2:
            out.update(trick=None, passes=0)
        out["turn"] = (seat + 1) % 3
        return MoveResultOk(out, "不要")
    if parse_bid(text) is not None and text not in ("1", "2", "3"):
        return MoveResultErr("叫分已经结束")
    try:
        cards = parse_cards(s["cards"][seat], text)
    except ValueError as exc:
        return MoveResultErr(str(exc))
    reading = read_against(cards, s["trick"]["combo"] if s["trick"] else None)
    if reading is None:
        return MoveResultErr("这不是有效的牌型，或压不过当前出牌")
    name = combo_name(reading)
    act = {
        "seat": seat,
        "cards": cards,
        "combo": reading,
        "name": name,
        "n": len(s["plays"]),
    }
    for card in cards:
        out["cards"][seat].remove(card)
    out["plays"].append(act)
    out.update(trick=act, passes=0, turn=(seat + 1) % 3)
    out["playsBy"][seat] += 1
    out["bombs"] += int(reading["type"] in ("bomb", "rocket"))
    log = name + "（" + move_text(cards) + "）"
    if not out["cards"][seat]:
        out, note = finish_hand(out, seat)
        log += " · 出完 · " + note
    return MoveResultOk(out, log)


class Doudizhu(GameModule):
    kind = "doudizhu"
    name: ClassVar[dict] = {"zh": "斗地主", "en": "Dou Dizhu"}
    family = "牌"
    blurb = "三人叫分抢地主，两位农民联手对抗。"
    players = PlayerSpec(3, 3, 3)
    options: ClassVar[list] = [
        OptionSpec(
            "hands",
            "局数",
            [{"value": str(n), "label": f"{n} 局"} for n in (1, 3, 6)],
            "1",
        )
    ]
    supports_turn_driver = True
    hidden_information = True
    rules = RULES
    move_help = (
        "叫分：bid 1 / bid 2 / bid 3 / pass；出牌：3 3 3 4、10 J Q K A、BJ RJ 或 pass"
    )

    def create(self, ctx: CreateContext):
        hands = str(ctx.options.get("hands", "1"))
        return create(ctx.seed, int(hands) if hands in ("1", "3", "6") else 1)

    def apply(self, state, seat, move):
        return apply(state, seat, move)

    def waiting_on(self, state):
        return [] if state["phase"] == "over" else [state["turn"]]

    def outcome(self, state):
        if state["phase"] != "over":
            return None
        scores = state["scores"]
        return Outcome(
            []
            if len(set(scores)) == 1
            else [i for i, n in enumerate(scores) if n == max(scores)],
            "积分 " + " · ".join(f"{n:+d}" for n in scores),
        )

    def seat_labels(self, state):
        if state["landlord"] is not None:
            return ["地主" if i == state["landlord"] else "农民" for i in range(3)]
        return [
            "叫分中" if i == state["turn"] and state["phase"] == "bid" else "待定"
            for i in range(3)
        ]

    def view(self, state, viewer):
        """显式白名单！不得 spread 原始 state（含 RNG 和三家手牌）。"""
        mine = type(viewer) is int and viewer in range(3)
        active = state["phase"] != "over"
        my_turn = mine and active and viewer == state["turn"]
        fields = (
            "phase",
            "handNo",
            "hands",
            "firstBidder",
            "redeals",
            "bid",
            "bidder",
            "bids",
            "landlord",
            "trick",
            "bombs",
            "scores",
            "results",
        )
        view = {k: copy.deepcopy(state[k]) for k in fields}
        latest = [None, None, None]
        for act in state["plays"][-2:]:
            latest[act["seat"]] = copy.deepcopy(act)
        if active:
            latest[state["turn"]] = None
        view.update(
            viewer=viewer if mine else None,
            you=viewer if mine else None,
            hand=list(state["cards"][viewer]) if mine else [],
            counts=[len(h) for h in state["cards"]],
            bottom=list(state["bottom"]) if state["landlord"] is not None else None,
            turn=state["turn"] if active else None,
            latest=latest,
            recent=copy.deepcopy(state["plays"][-8:]),
            multiplier=2 ** state["bombs"],
            myTurn=my_turn,
            legalBids=legal_bids(state) if my_turn and state["phase"] == "bid" else [],
            hints=plays(
                state["cards"][viewer],
                state["trick"]["combo"] if state["trick"] else None,
            )[:60]
            if my_turn and state["phase"] == "play"
            else [],
            canPass=bool(my_turn and (state["phase"] == "bid" or state["trick"])),
        )
        return view

    def describe(self, state, seat, names):
        view = self.view(state, seat)
        labels = self.seat_labels(state)
        names = list(names or [])
        names += [f"Seat {i}" for i in range(len(names), 3)]
        lines = [
            f"Hand {state['handNo']}/{state['hands']}; phase={state['phase']}; redeals={state['redeals']}.",
            "Seats: "
            + "; ".join(
                f"{names[i]} (seat {i}, {labels[i]}, {view['counts'][i]} cards)"
                for i in range(3)
            ),
            f"Current bid={state['bid']}; multiplier={view['multiplier']}; scores={state['scores']}.",
            "Bottom cards: "
            + (
                " ".join(view["bottom"])
                if view["bottom"] is not None
                else "3 hidden cards"
            ),
            "Bids: " + str(view["bids"]),
        ]
        if view["viewer"] is not None:
            lines.append(
                "Your PRIVATE hand (do not repeat in table speech): "
                + move_text(view["hand"])
            )
            lines.append("Your PRIVATE hand with suits: " + " ".join(view["hand"]))
            if state["landlord"] is not None:
                partners = [
                    names[i] for i in range(3) if i != seat and i != state["landlord"]
                ]
                lines.append(
                    "You are landlord, playing alone."
                    if seat == state["landlord"]
                    else "You are a farmer; partner=" + ", ".join(partners)
                )
        lines.append(
            "Recent PUBLIC plays: "
            + "; ".join(
                f"{names[a['seat']]}: {a['name']} ({move_text(a['cards']) if a['cards'] else 'pass'})"
                for a in view["recent"]
            )
        )
        if view["myTurn"]:
            if state["phase"] == "bid":
                lines.append(
                    "YOUR TURN to bid. Legal: "
                    + ", ".join(
                        "pass" if b == 0 else f"bid {b}" for b in view["legalBids"]
                    )
                )
            else:
                lines.append(
                    "YOUR TURN. "
                    + (
                        "Beat " + state["trick"]["name"] + " or pass."
                        if state["trick"]
                        else "Lead a new trick; passing is forbidden."
                    )
                )
                lines.append(
                    "Legal examples (not exhaustive; any legal combination accepted): "
                    + "; ".join(p["move"] for p in view["hints"][:40])
                )
        else:
            lines.append(
                "Waiting for "
                + (
                    names[view["turn"]]
                    if view["turn"] is not None
                    else "match finished"
                )
            )
        if state["trick"]:
            owner = state["trick"]["seat"]
            lines.append(
                "Current PUBLIC play to beat: "
                + names[owner]
                + ": "
                + move_text(state["trick"]["cards"])
            )
        return "\n".join(lines)

    def chat_context(self, state, viewer, names=None):
        return self.describe(state, viewer, names or ["座位1", "座位2", "座位3"])

    def key_events(self, state, *, before=None, seat=None):
        if before is None:
            return {}
        last = state["plays"][-1] if state["plays"] else None
        new_play = (
            len(state["plays"]) > len(before["plays"])
            and state["handNo"] == before["handNo"]
        )
        flags = {
            "bomb": state["bombs"] > before["bombs"],
            "landlord": state["landlord"] is not None and before["landlord"] is None,
            "alarm": bool(
                new_play
                and type(seat) is int
                and seat in range(3)
                and 0 < len(state["cards"][seat]) <= 2 < len(before["cards"][seat])
            ),
            "out": len(state["results"]) > len(before["results"]),
            "rocket": bool(
                new_play
                and last
                and last["combo"]
                and last["combo"]["type"] == "rocket"
            ),
        }
        return {**flags, "key": any(flags.values())}

    def bot(self, state, seat):
        from .bot import bot_move

        return bot_move(state, seat)

    def plan_prompt(self, state, seat):
        return "先保证合法；叫分只依据自己手牌；农民注意配合让牌、报单报双，保留控制牌；不得利用或泄漏其他座位私牌。"


doudizhu = Doudizhu()
