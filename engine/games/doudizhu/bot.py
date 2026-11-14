"""确定性非透视陪练：自己手牌 + 公开牌数/阵营，失败兜底也用同一规则。"""

from collections import Counter

from .cards import plays, rank, read_against


def bid_strength(hand):
    counts = Counter(rank(c) for c in hand)
    return (
        4 * bool(counts[14])
        + 3 * bool(counts[13])
        + 2 * counts[12]
        + counts[11]
        + 4 * sum(counts[r] == 4 for r in range(12))
        + 2 * bool(counts[13] and counts[14])
    )


def bot_move(s, seat):
    if s["phase"] == "over" or seat != s["turn"]:
        return None
    hand = s["cards"][seat]
    if s["phase"] == "bid":
        strength = bid_strength(hand)
        want = (
            3 if strength >= 11 else 2 if strength >= 8 else 1 if strength >= 5 else 0
        )
        if want > s["bid"]:
            return f"bid {want}"
        if not s["bid"] and len(s["bids"]) == 2 and s["redeals"] >= 2:
            return "bid 1"
        return "pass"
    target = s["trick"]["combo"] if s["trick"] else None
    options = plays(hand, target)
    if not options:
        return "pass" if target else None
    finish = next((p for p in options if len(p["cards"]) == len(hand)), None)
    if finish:
        return finish["move"]
    landlord = s["landlord"]
    farmer = seat != landlord
    enemies = [landlord] if farmer else [i for i in range(3) if i != seat]
    enemy_min = min(len(s["cards"][i]) for i in enemies)
    partner = (
        next((i for i in range(3) if i != seat and i != landlord), None)
        if farmer
        else None
    )
    normal = [p for p in options if p["combo"]["type"] not in ("bomb", "rocket")]
    if target and farmer and s["trick"]["seat"] != landlord:
        # 搭档当前占圈：通常让牌，让搭档继续领出。但地主只剩 1–2 张时
        # 无条件让牌等于把主动权交给地主——地主可能用最后一张/一对直接
        # 走完。这里只在地主临门时顶住，既保留配合让牌的主策略，又不再
        # 出现「明明能压却一路不要」的漏防。
        trick_type = s["trick"]["combo"]["type"]
        if enemy_min <= 2:
            if trick_type == "single":
                singles = [p for p in options if p["combo"]["type"] == "single"]
                if enemy_min == 1 and s["trick"]["combo"]["key"] < 14:
                    # 地主只剩一张时，炸弹/王炸不会被单张压过，能稳稳
                    # 把出牌权拿回来；搭档已出大王（key 14）时单张必过，
                    # 不必浪费炸弹。
                    bombs = [
                        p
                        for p in options
                        if p["combo"]["type"] in ("bomb", "rocket")
                    ]
                    if bombs:
                        return bombs[0]["move"]
                if singles:
                    return max(singles, key=lambda p: p["combo"]["key"])["move"]
            elif trick_type == "pair" and enemy_min == 2:
                # 搭档对 2（key 13）已是最大对子：地主两张里只有王炸能压，
                # 而王炸也压得过我们的炸弹，炸出去纯亏；继续让牌。
                if s["trick"]["combo"]["key"] >= 13:
                    return "pass"
                # 否则地主两张可能是更大的对子，搭档的对子会被直接压完；
                # 有炸弹就用炸弹截住，没有则维持让牌。
                bombs = [
                    p
                    for p in options
                    if p["combo"]["type"] in ("bomb", "rocket")
                ]
                if bombs:
                    return bombs[0]["move"]
        return "pass"
    if (
        not target
        and partner is not None
        and len(s["cards"][partner]) in (1, 2)
        and enemy_min > 2
    ):
        kind = "single" if len(s["cards"][partner]) == 1 else "pair"
        feed = next((p for p in normal if p["combo"]["type"] == kind), None)
        if feed:
            return feed["move"]
    if target:
        if normal:
            if enemy_min <= 2:
                return normal[-1]["move"]
            # 不为小牌轻易拆炸弹/王炸；保留手牌结构。
            counts = Counter(rank(c) for c in hand)

            def cost(p):
                used = Counter(rank(c) for c in p["cards"])
                breaks = sum(40 for r, n in used.items() if counts[r] == 4 and n < 4)
                breaks += 30 * bool(
                    counts[13] and counts[14] and (used[13] or used[14])
                )
                return breaks + p["combo"]["key"] * 0.6 - len(p["cards"])

            return min(normal, key=cost)["move"]
        best = options[0]
        left = list(hand)
        for c in best["cards"]:
            left.remove(c)
        return best["move"] if enemy_min <= 4 or read_against(left) else "pass"
    pool = normal or options
    if enemy_min == 1:
        nonsingle = [p for p in pool if p["combo"]["type"] != "single"]
        if not nonsingle:
            return max(pool, key=lambda p: p["combo"]["key"])["move"]
        pool = nonsingle
    return max(
        pool,
        key=lambda p: (len(p["cards"]) - p["combo"]["key"] * 0.25, -p["combo"]["key"]),
    )["move"]
