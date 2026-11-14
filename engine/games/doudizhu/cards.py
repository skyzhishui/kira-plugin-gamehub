"""斗地主牌型、文本解析和合法组合枚举；规则基线 rain-go 06e4b75。"""

from __future__ import annotations

import itertools
import re
from collections import Counter

RANKS = ["3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2", "BJ", "RJ"]
SUITS = "SHCD"
TYPE_NAMES = dict(
    zip(
        [
            "single",
            "pair",
            "triple",
            "triple1",
            "triple2",
            "straight",
            "pairs",
            "plane",
            "plane1",
            "plane2",
            "four2",
            "four4",
            "bomb",
            "rocket",
        ],
        [
            "单张",
            "对子",
            "三张",
            "三带一",
            "三带二",
            "顺子",
            "连对",
            "飞机",
            "飞机带单",
            "飞机带对",
            "四带二",
            "四带两对",
            "炸弹",
            "王炸",
        ],
    )
)
LEAD_ORDER = [
    "rocket",
    "bomb",
    "single",
    "pair",
    "triple",
    "triple1",
    "triple2",
    "straight",
    "pairs",
    "plane",
    "plane1",
    "plane2",
    "four2",
    "four4",
]


def rank(card: str) -> int:
    return RANKS.index(card if card in ("BJ", "RJ") else card[1:])


def deck() -> list[str]:
    return [s + r for r in RANKS[:13] for s in SUITS] + ["BJ", "RJ"]


def sort_cards(cards) -> list[str]:
    return sorted(
        cards,
        key=lambda c: (rank(c), SUITS.index(c[0]) if c not in ("BJ", "RJ") else 0),
    )


def move_text(cards) -> str:
    return " ".join(RANKS[rank(c)] for c in sort_cards(cards))


def combo(kind: str, key: int, length: int, size: int) -> dict:
    return {"type": kind, "key": key, "len": length, "size": size}


def interpretations(cards) -> list[dict]:
    cnt = Counter(rank(c) for c in cards)
    total = sum(cnt.values())
    if not total:
        return []
    if total == 2 and cnt[13] == cnt[14] == 1:
        return [combo("rocket", 14, 1, 2)]
    present = sorted(cnt)
    if len(present) == 1:
        kind = {1: "single", 2: "pair", 3: "triple", 4: "bomb"}.get(total)
        return [combo(kind, present[0], 1, total)] if kind else []
    out = []
    if present[-1] <= 11 and present[-1] - present[0] + 1 == len(present):
        n = cnt[present[0]]
        if all(cnt[r] == n for r in present):
            kind = (
                "straight"
                if n == 1 and len(present) >= 5
                else "pairs"
                if n == 2 and len(present) >= 3
                else "plane"
                if n == 3 and len(present) >= 2
                else None
            )
            if kind:
                out.append(combo(kind, present[-1], len(present), total))

    def wings(main, m, per):
        rest = cnt.copy()
        for r in main:
            if rest[r] != m:
                return False
            rest[r] = 0
        if rest[13] and rest[14]:
            return False
        need = len(main) * (2 if m == 4 else 1)
        return sum(rest.values()) == need * per and (
            per == 1 or all(n % 2 == 0 for n in rest.values())
        )

    for size, m, per, kind in [
        (4, 3, 1, "triple1"),
        (5, 3, 2, "triple2"),
        (6, 4, 1, "four2"),
        (8, 4, 2, "four4"),
    ]:
        if total == size:
            out.extend(combo(kind, r, 1, total) for r in present if wings([r], m, per))
    for per, kind in [(1, "plane1"), (2, "plane2")]:
        length, rem = divmod(total, 3 + per)
        if rem or length < 2:
            continue
        for start in range(13 - length):
            if wings(range(start, start + length), 3, per):
                out.append(combo(kind, start + length - 1, length, total))
    return out


def beats(a: dict, b: dict) -> bool:
    if b["type"] == "rocket":
        return False
    if a["type"] == "rocket":
        return True
    if a["type"] == "bomb":
        return b["type"] != "bomb" or a["key"] > b["key"]
    return (
        b["type"] != "bomb"
        and all(a[k] == b[k] for k in ("type", "len", "size"))
        and a["key"] > b["key"]
    )


def read_against(cards, target=None):
    reads = interpretations(cards)
    if target:
        return max(
            (c for c in reads if beats(c, target)), key=lambda c: c["key"], default=None
        )
    return min(
        reads,
        key=lambda c: (LEAD_ORDER.index(c["type"]), -c["len"], -c["key"]),
        default=None,
    )


def combo_name(c: dict) -> str:
    name = TYPE_NAMES[c["type"]]
    if c["type"] == "rocket":
        return name
    if c["type"] in ("straight", "pairs", "plane", "plane1", "plane2"):
        return f"{name} {RANKS[c['key'] - c['len'] + 1]}-{RANKS[c['key']]}"
    rank_name = {13: "小王", 14: "大王"}.get(c["key"], RANKS[c["key"]])
    return f"{name} {rank_name}"


def parse_cards(hand, text: str) -> list[str]:
    """解析为实际牌；错误短句不暴露其他座位手牌。"""
    aliases = {"小王": "BJ", "小鬼": "BJ", "大王": "RJ", "大鬼": "RJ", "T": "10"}
    suit_aliases = dict(zip("♠♥♣♦♤♡♧♢", "SHCDSHCD"))
    tokens = []
    for part in re.split(r"[\s，,、;；]+", text.strip().upper()):
        if not part:
            continue
        part = "".join(suit_aliases.get(c, c) for c in part)
        if re.fullmatch(r"[SHCD](?:10|[3-9TJQKA2])", part):
            tokens.append(part.replace("T", "10"))
        else:
            parts = re.findall(r"10|BJ|RJ|小王|大王|小鬼|大鬼|[3-9TJQKA2]", part)
            if "".join(parts) != part:
                raise ValueError("看不懂这张牌，请用 3 3 4、10 J Q K A、BJ RJ 或 pass")
            tokens.extend(aliases.get(p, p) for p in parts)
    if not tokens:
        raise ValueError("没有写要出的牌")
    left, picked = sort_cards(hand), []
    for token in (t for t in tokens if t not in RANKS):
        if token not in left:
            raise ValueError("你没有这张牌，或同一张牌写了两次")
        left.remove(token)
        picked.append(token)
    for token in (t for t in tokens if t in RANKS):
        card = next((c for c in left if rank(c) == RANKS.index(token)), None)
        if card is None:
            raise ValueError("你没有足够的这类牌，请检查自己的手牌")
        left.remove(card)
        picked.append(card)
    return sort_cards(picked)


def plays(hand, target=None) -> list[dict]:
    """枚举 rank 多重集而非手牌幂集，结果由规则解释器复核。"""
    cnt = Counter(rank(c) for c in hand)
    candidates = set()

    def add(ranks):
        candidates.add(tuple(sorted(ranks)))

    def wings(main, per, size):
        avail = [r for r in range(15) if r not in main and cnt[r] >= per]
        for chosen in itertools.combinations_with_replacement(avail, size):
            counts = Counter(chosen)
            if 13 in counts and 14 in counts:
                continue
            if all(n * per <= cnt[r] for r, n in counts.items()):
                yield [r for r in chosen for _ in range(per)]

    for r, n in cnt.items():
        for amount in range(1, min(n, 4) + 1):
            add([r] * amount)
        if n >= 3:
            for per in (1, 2):
                for w in wings([r], per, 1):
                    add([r] * 3 + w)
        if n == 4:
            for per in (1, 2):
                for w in wings([r], per, 2):
                    add([r] * 4 + w)
    if cnt[13] and cnt[14]:
        add([13, 14])
    for start in range(12):
        for end in range(start, 12):
            chain = list(range(start, end + 1))
            minimum = min(cnt[r] for r in chain)
            if not minimum:
                break
            length = len(chain)
            for amount, need in [(1, 5), (2, 3), (3, 2)]:
                if minimum >= amount and length >= need:
                    body = [r for r in chain for _ in range(amount)]
                    add(body)
                    if amount == 3:
                        for per in (1, 2):
                            for w in wings(chain, per, length):
                                add(body + w)
    out = []
    for ranks in sorted(candidates):
        left, cards = sort_cards(hand), []
        for r in ranks:
            card = next(c for c in left if rank(c) == r)
            left.remove(card)
            cards.append(card)
        reading = read_against(cards, target)
        if reading:
            out.append(
                {
                    "cards": cards,
                    "combo": reading,
                    "move": move_text(cards),
                    "name": combo_name(reading),
                }
            )
    return sorted(
        out,
        key=lambda p: (
            2
            if p["combo"]["type"] == "rocket"
            else 1
            if p["combo"]["type"] == "bomb"
            else 0,
            p["combo"]["key"],
            len(p["cards"]),
            tuple(rank(c) for c in p["cards"]),
        ),
    )
