"""每局棋类辅助等级；候选来自同一bot评分，不是搜索证明的最优解。"""

try:  # production: plugin package (``plugins.<plugin>.assistance``)
    from . import modes
    from .engine import games
except ImportError:  # standalone tests import this module as top-level
    import modes
    from engine import games

LEVELS = ("beginner", "intermediate", "advanced")


def level_of(match):
    return modes.options_of(match).get("level", "intermediate")


def _delegated_teammate(match):
    """诺里是队友且人类座位已委托代打（S4「友方高级模式」）。"""
    return modes.is_delegated(match) and modes.role_of(match) == modes.ROLE_TEAMMATE


def _delegated_human(match, seat):
    """强制高级辅助只给被代打的人类座位，绝不外溢到对手/其他座位。"""
    if seat is None:
        return False
    try:
        target = int(seat)
        human = modes.human_seat_index(match)
    except (TypeError, ValueError):  # noqa: BLE001 - 坏快照/坏座位按未委托处理
        return False
    return human is not None and target == human and _delegated_teammate(match)


def analysis_enabled(match, seat=None):
    # 代打时诺里替人类座位落子，必须看得见威胁位置（与所选等级无关）；
    # seat 缺省/非人类座位保持按等级判定（对手座位不吃代打加成）。
    return _delegated_human(match, seat) or level_of(match) != "beginner"


def ranked_moves(kind, state, seat, limit=3):
    module = games.get(kind)
    ranker = getattr(module, "ranked_moves", None)
    return ranker(state, seat)[:limit] if callable(ranker) else []


def recommendation_text(match, seat):
    if match.kind not in ("gomoku", "reversi"):
        return ""
    # 代打的人类座位固定附前三候选；其余座位/普通对局仍只在高级档提供。
    if not (_delegated_human(match, seat) or level_of(match) == "advanced"):
        return ""
    if seat not in games.get(match.kind).waiting_on(match.state):
        return ""
    moves = ranked_moves(match.kind, match.state, seat)
    if not moves:
        return ""
    return (
        "Bot top-3 recommendations（启发式排名，不保证搜索最优；可自主选择其他合法动作）：\n"
        + "\n".join(
            f"{i + 1}. {m['move']} (score={m['score']:g})" for i, m in enumerate(moves)
        )
    )