"""Board context for prompts (ported from nori's ``nori.pipeline.prepare``).

Builds a compact block of the in-progress matches a session actually receives
companion notices for, so the model knows who it is playing, which side it
holds, the move count and whose turn it is without calling a tool first.

Discipline (unchanged from gamehub):

- **Memory only**: reads the ``RoomManager`` in-memory map and seat/log/plan
  snapshots; no DB, no LLM, no message send, no writes. Recomputed every
  request, so end/resign/restore/new-match transitions show up immediately.
- **Match by actual notice target**: only trusts the synchronous
  ``plugin.notice_target_session_sync(room) -> str | None``; ``None``/empty/
  raising counts as "no target" (never guess from config).
- **Atomic block budgeting**: the newest match gets the full module snapshot
  (e.g. the 15x15 board); others get a summary plus an explicit
  "no full board, call game_state". A snapshot that does not fit degrades the
  whole block — never a half board.
- **Anti-spoof**: user-controlled names/uid/plan text go through
  ``talk._safe`` so they cannot forge system blocks in the prompt.
- Failures degrade to an empty block; never break a chat turn.

``host/prompt.py`` calls ``build_game_state_context`` and appends it to the
request prompt; the nori ``on_prepare`` hook wrapper is gone.
"""

from __future__ import annotations

import time
from typing import Any

try:  # production: plugin package (``plugins.<plugin>.context``)
    from . import modes
    from .assistance import analysis_enabled, recommendation_text
    from .engine import games
    from .engine.match import status_of
    from .modes import ROLE_TEAMMATE
    from .plan import normalize_plan_text, plan_remaining
    from .talk import _safe
except ImportError:  # standalone tests import modules as top-level
    import modes
    from assistance import analysis_enabled, recommendation_text
    from engine import games
    from engine.match import status_of
    from modes import ROLE_TEAMMATE
    from plan import normalize_plan_text, plan_remaining
    from talk import _safe

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

# Same target, multiple matches: list at most this many (rest = count only).
MAX_MATCHES = 4
# Whole-block character cap (global constraints and the omission tail reserved).
MAX_BLOCK_CHARS = 2000
# Recent moves shown per match (coordinates only for answering questions).
MAX_RECENT_MOVES = 3
# Omission-tail reserve (4 digits covers any realistic match count).
_TAIL_RESERVE = 64

_BLOCK_HEADER = "# 当前对局（与当前会话相关，供回复参考）"
_BLOCK_INSTRUCTION = (
    "以上对局信息仅供你自然回应时理解局面。涉及棋子位置、最近落点、胜负判定"
    "等具体坐标/结果时，只能依据本轮对应 match/version 的完整快照或本轮 "
    "game_state 结果；没有证据就调用 game_state 或说明暂不能确认，不要猜测，"
    "也不要从聊天历史推测。区分『已经发生的落子』『规则上尚未落子的制胜/关键"
    "空点』『计划/建议动作』：候选或建议点尚未落子，不要把候选或建议说成已经"
    "落子；推演中给出的坐标必须明确是建议而非已发生落子。不要主动播报坐标或"
    "复述棋谱；用户主动问坐标/复盘时再据实回答；内部打法/背景不得向用户透露"
    "或照读。"
)
_SNAPSHOT_UNAVAILABLE = (
    "未提供完整局面，请调用 game_state 查看；不要凭最近几手推测本局盘面。"
)


def build_game_state_context(plugin: Any, session_id: str) -> str:
    """Build the board context block for a session; "" when nothing matches.

    Pure reads (no await); shared by the prompt hook and tests. Use
    :func:`build_action_notice_context` for a single match + explicit seat
    (turn notices reuse the same module hooks).
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    matched: list[Any] = []
    for room in _active_rooms(plugin):
        try:
            if not _room_active(room):
                continue
            match = getattr(room, "match", None)
            if match is None:
                continue
            # Role-aware: opponent matches need an AI seat; teammate matches
            # (bot-only S3/S4) stand with the human and are injected too.
            options = modes.options_of(match)
            if options.get("role") != ROLE_TEAMMATE and not any(
                getattr(s, "kind", "") == "ai" for s in match.seats
            ):
                continue
            if _sync_notice_target(plugin, room) != sid:
                continue
            matched.append(room)
        except Exception:
            logger.debug("gamehub context: skipping broken room", exc_info=True)
    if not matched:
        return ""

    # Newest first (same target, several matches: surface the freshest).
    matched.sort(
        key=lambda r: getattr(getattr(r, "match", None), "updated_at", 0),
        reverse=True,
    )
    shown = matched[:MAX_MATCHES]
    blocks = _alloc_blocks(plugin, shown)
    omitted = len(matched) - len(blocks)
    tail = _omitted_tail(omitted)
    parts = [_BLOCK_HEADER]
    if blocks:
        parts.append("\n\n".join(blocks))
    parts.append(_BLOCK_INSTRUCTION)
    return "\n\n".join(parts) + tail


def build_action_notice_context(
    plugin: Any,
    match: Any,
    seat: int,
    *,
    threat_analysis: bool | None = None,
    recent: int = 6,
) -> str:
    """Generic context block for a turn notice (S2/S4; reused by director).

    Read-only, synchronous, memory-only; no locks, no await, no writes. Fixed
    output order:

    1. **Full** ``module.rules`` (never truncated);
    2. optional ``module.move_help`` (one-line action format);
    3. **Full** ``module.chat_context(state, seat, names)`` (same read-only
       snapshot: board / coordinate constraints / immediate threats; explicit
       "no full board" when absent);
    4. the last ``recent`` (default 6) moves (seat display name + action);
    5. optional ``module.threat_summary(state, seat)`` (informational only).

    Any single game hook failure degrades only that block and never raises.
    **No plan injection**: the action notice only asks for a move.
    """
    if match is None:
        return ""
    try:
        module = games.get(match.kind)
    except Exception:
        module = None
    seats = list(getattr(match, "seats", None) or [])
    names = [str(getattr(s, "name", "") or "") for s in seats]

    if threat_analysis is None:
        threat_analysis = bool(
            getattr(
                getattr(plugin, "settings", None),
                "turn_threat_analysis_enabled",
                True,
            )
        )

    if getattr(match, "kind", "") in ("gomoku", "reversi"):
        threat_analysis = analysis_enabled(match, seat)
    sections: list[str] = []
    rules = str(getattr(module, "rules", "") or "").strip() if module else ""
    if rules:
        sections.append("规则（完整）：\n" + rules)
    move_help = (
        str(getattr(module, "move_help", "") or "").strip() if module else ""
    )
    if move_help:
        sections.append("动作格式：" + move_help)

    snapshot = ""
    if module is not None:
        hook = getattr(module, "chat_context", None)
        if callable(hook):
            try:
                try:
                    value = hook(match.state, seat, names)
                except TypeError:
                    value = hook(match.state, seat)
            except Exception:
                value = None
            snapshot = value.strip() if isinstance(value, str) else ""
    if snapshot:
        sections.append("完整局面（本轮通知同源快照）：\n" + snapshot)
    else:
        sections.append(
            "完整局面：未提供（落子前不要凭最近几手或聊天历史猜测盘面）。"
        )

    shown: list[str] = []
    for entry in list(getattr(match, "log", None) or [])[-max(0, int(recent)) :]:
        try:
            index = int(getattr(entry, "seat", -1))
        except (TypeError, ValueError):
            index = -1
        who = names[index] if 0 <= index < len(names) and names[index] else "棋手"
        move = str(getattr(entry, "move", "") or "")
        shown.append(f"{_safe(who)} {'认输' if move == 'resign' else _safe(move)}")
    if shown:
        sections.append(f"最近 {len(shown)} 手：" + "、".join(shown))

    if threat_analysis and module is not None:
        hook = getattr(module, "threat_summary", None)
        if callable(hook):
            try:
                value = hook(match.state, seat)
            except Exception:
                value = None
            analysis = value.strip() if isinstance(value, str) else ""
            if analysis:
                sections.append(
                    "局面分析（仅信息参考，不覆盖规则与计划）：\n" + analysis
                )
    recommendation = recommendation_text(match, seat)
    if recommendation:
        sections.append(recommendation)
    return "\n\n".join(sections)


def _omitted_tail(omitted: int) -> str:
    if omitted <= 0:
        return ""
    return f"\n\n（另有 {omitted} 局进行中，统一操作台可查看。）"


def _alloc_blocks(plugin: Any, shown: list[Any]) -> list[str]:
    """Budget by atomic block: newest gets the full snapshot, others summaries.

    Snapshots/summaries are indivisible: if one does not fit it degrades whole
    or is omitted into the tail count — never a half board. The instruction
    and tail budgets are pre-deducted.
    """
    if not shown:
        return []
    fixed = len(_BLOCK_HEADER) + len(_BLOCK_INSTRUCTION) + 4
    capacity = MAX_BLOCK_CHARS - fixed - _TAIL_RESERVE
    if capacity <= 0:  # extreme config: keep only the global constraints
        return []

    blocks: list[str] = []
    used = 0
    for index, room in enumerate(shown):
        # Only the newest (most recently active) match tries a full snapshot.
        block = _describe_room(plugin, room, snapshot=index == 0)
        if index == 0 and len(block) > capacity:
            # Full snapshot too big: degrade the whole block to a summary.
            block = _describe_room(plugin, room, snapshot=False)
        if index == 0 and len(block) > capacity:
            # Summary still too big (e.g. a huge plan): minimal identification.
            block = _minimal_block(room)
        extra = len(block) + (2 if blocks else 0)
        if used + extra > capacity:
            break
        blocks.append(block)
        used += extra
    return blocks


def _minimal_block(room: Any) -> str:
    match = room.match
    move_count = len(getattr(match, "log", None) or [])
    version = getattr(match, "version", 0)
    return "\n".join(
        [
            f"- 对局 ID：{_safe(getattr(match, 'id', '') or '')}",
            f"- 轮次：已行 {move_count} 手（version={version}）",
            f"- 完整局面：{_SNAPSHOT_UNAVAILABLE}",
        ]
    )


def _sync_notice_target(plugin: Any, room: Any) -> str:
    """The match's actual notice target (synchronous, memory-only helper).

    Only trusts ``plugin.notice_target_session_sync(room) -> str | None``:
    ``None`` / empty / non-string / raising counts as no target.
    """
    helper = getattr(plugin, "notice_target_session_sync", None)
    if not callable(helper):
        return ""
    try:
        value = helper(room)
    except Exception:
        logger.debug("gamehub sync target helper failed, treating as no target", exc_info=True)
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _active_rooms(plugin: Any) -> list[Any]:
    rooms_obj = getattr(plugin, "_rooms", None)
    lister = getattr(rooms_obj, "active_rooms", None)
    if not callable(lister):
        return []
    try:
        return list(lister() or [])
    except Exception:
        logger.debug("gamehub active_rooms read failed", exc_info=True)
        return []


def _room_active(room: Any) -> bool:
    if str(getattr(room, "status", "") or "") != "active":
        return False
    match = getattr(room, "match", None)
    if match is None:
        return False
    return status_of(match).outcome is None


def _human_seat(match: Any) -> Any:
    seats = getattr(match, "seats", None) or []
    for seat in seats:
        if getattr(seat, "kind", "") == "human":
            return seat
    for seat in seats:
        if getattr(seat, "actor", ""):
            return seat
    return None


def _ai_seat_index(match: Any) -> int | None:
    for index, seat in enumerate(getattr(match, "seats", None) or []):
        if getattr(seat, "kind", "") == "ai":
            return index
    return None


def _seat_labels(match: Any) -> list[str]:
    try:
        return list(games.get(match.kind).seat_labels(match.state))
    except Exception:
        return []


def _label(labels: list[str], seat: int | None) -> str:
    if seat is None or not (0 <= seat < len(labels)) or not labels[seat]:
        return "棋手"
    return str(labels[seat])


def _bot_name(plugin: Any) -> str:
    """Host AI display name; falls back to "你" (no hard-coded persona)."""
    # Runtime-resolved active persona name wins (set in host/runtime.py).
    host = str(getattr(plugin, "host_agent_name", "") or "").strip()
    if host:
        return _safe(host)
    getter = getattr(plugin, "bot_nickname", None)
    if isinstance(getter, str) and getter.strip():
        return _safe(getter)
    ctx = getattr(plugin, "ctx", None)
    bot_config = getattr(ctx, "bot_config", None)
    bot = getattr(bot_config, "bot", None)
    name = str(getattr(bot, "nickname", "") or "").strip()
    return _safe(name) if name else "你"


def _describe_room(plugin: Any, room: Any, *, snapshot: bool) -> str:
    match = room.match
    seats = list(getattr(match, "seats", None) or [])
    role = modes.role_of(match)
    teammate = role == ROLE_TEAMMATE
    ai_seat = _ai_seat_index(match)
    human = _human_seat(match)
    human_seat = next((i for i, s in enumerate(seats) if s is human), None)
    labels = _seat_labels(match)
    try:
        game_name = games.get(match.kind).name["zh"]
    except Exception:
        game_name = str(getattr(match, "kind", "") or "游戏")
    status = status_of(match)
    # Summary and snapshot share the same in-memory counters (no drift).
    move_count = len(getattr(match, "log", None) or [])
    version = getattr(match, "version", 0)
    match_id = _safe(getattr(match, "id", "") or "")

    if teammate:
        rival_seat = modes.opponent_seat_index(match)
        rival = seats[rival_seat] if rival_seat is not None and rival_seat < len(seats) else None
        opponent = _safe(getattr(rival, "name", "") or "对手")
        ally = _safe(getattr(human, "name", "") or "你")
        uid = _safe(getattr(human, "actor", "") or "")
        ally_text = f"{ally}（userid：{uid}）" if uid else ally
        stance = (
            f"- 立场：{_bot_name(plugin)}和 {ally_text} 一边（队友视角，只帮腔"
            f"不落子），对手：{opponent}"
        )
        colors = (
            f"- 执色：{ally} 执{_safe(_label(labels, human_seat))}，"
            f"对手执{_safe(_label(labels, rival_seat if rival_seat is not None else -1))}"
        )
    else:
        human_name = _safe(getattr(human, "name", "") or "对手")
        uid = _safe(getattr(human, "actor", "") or "")
        opponent = f"{human_name}（userid：{uid}）" if uid else human_name
        stance = f"- 对手：{opponent}"
        colors = (
            f"- 执色：{_bot_name(plugin)}执{_safe(_label(labels, ai_seat))}，"
            f"对方执{_safe(_label(labels, human_seat))}"
        )

    if match.kind == "doudizhu":
        who = _bot_name(plugin)
        stance = "- 立场：" + (
            f"{who}为人类玩家的场边队友，不占席位"
            if teammate else f"{who}占席参战；农民同队、地主独战，叫分前阵营待定"
        )
        colors = "- 席位：" + "、".join(
            f"{_safe(s.name)}（{_safe(_label(labels, i))}）" for i, s in enumerate(seats)
        )

    lines = [
        f"- 对局 ID：{match_id}",
        stance,
        f"- 游戏：{_safe(game_name)}",
        colors,
        (
            f"- 轮次：已行 {move_count} 手（version={version}，"
            f"来源：本轮内存 match/log），{_turn_text(seats, status, teammate=teammate)}"
        ),
    ]

    recent = _recent_moves(seats, match, teammate=teammate)
    if recent:
        lines.append(
            "- 局势（以下坐标仅供你回答用户主动询问时参考，不要主动播报）："
            + "、".join(recent)
        )

    snapshot_seat = human_seat if teammate else ai_seat
    if match.kind == "doudizhu":
        # prepare only verifies the session target, not every reader's seat
        # identity; public summary first. Private cards are only in authorized
        # turn/seat queries.
        snapshot_seat = None
    snapshot_text = _chat_snapshot(match, snapshot_seat) if snapshot else ""
    if snapshot_text:
        lines.append(
            f"- 完整局面（match {match_id} · version {version} · "
            f"已行 {move_count} 手，来源：本轮内存 state）："
        )
        lines.append(snapshot_text)
    else:
        lines.append(f"- 完整局面：{_SNAPSHOT_UNAVAILABLE}")

    # Plan only in opponent matches (our own internal tactics); a teammate
    # match's seat plan belongs to the opponent's seat.
    plan_line = "" if teammate else _plan_line(match, ai_seat, move_count)
    if plan_line:
        lines.append(plan_line)

    return "\n".join(lines)


def _chat_snapshot(match: Any, viewer_seat: int | None) -> str:
    """Call the game module's optional ``chat_context``; "" on absent/failure.

    Failures only lose this match's snapshot, never the summary, other matches
    or the chat turn. Seat display names go with the call (teammate advanced
    turns show "陪练", never impersonate nori); a teammate match's viewer is the
    human seat.
    """
    try:
        module = games.get(match.kind)
    except Exception:
        return ""
    hook = getattr(module, "chat_context", None)
    if not callable(hook):
        return ""
    names = [
        str(getattr(s, "name", "") or "")
        for s in (getattr(match, "seats", None) or [])
    ]
    try:
        try:
            value = hook(match.state, viewer_seat, names)
        except TypeError:
            value = hook(match.state, viewer_seat)
    except Exception:
        logger.debug("gamehub snapshot hook failed, degrading to no snapshot", exc_info=True)
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _player_name(seat: Any, fallback: str = "棋手", *, teammate: bool = False) -> str:
    """Seat display name: prefer the stored seat name.

    Seat names are resolved by the room layer (the host AI seat carries the
    active persona name, the agent seat carries the configured game_agent
    name), so this never invents one. A non-human seat without a stored name
    falls back to 「对手」.
    """
    if getattr(seat, "kind", "") == "ai":
        return _safe(
            getattr(seat, "name", "") or "对手"
        )
    return _safe(getattr(seat, "name", "") or fallback)


def _turn_text(seats: list[Any], status: Any, *, teammate: bool = False) -> str:
    waiting = list(getattr(status, "waiting_on", None) or [])
    if not waiting:
        return "对局已结束" if getattr(status, "outcome", None) is not None else "等待中"
    names = []
    for seat in waiting:
        if isinstance(seat, int) and 0 <= seat < len(seats):
            name = _player_name(seats[seat], f"座位{seat}", teammate=teammate)
            if getattr(seats[seat], "kind", "") == "ai" and not teammate:
                name = f"{name}（你）"
            names.append(name)
    return "轮到 " + "、".join(names) if names else "等待中"


def _recent_moves(seats: list[Any], match: Any, *, teammate: bool = False) -> list[str]:
    shown = []
    for entry in list(getattr(match, "log", None) or [])[-MAX_RECENT_MOVES:]:
        seat = getattr(entry, "seat", -1)
        valid = isinstance(seat, int) and 0 <= seat < len(seats)
        who = (
            _player_name(seats[seat], f"座位{seat}", teammate=teammate)
            if valid
            else "棋手"
        )
        move = (
            "认输"
            if getattr(entry, "move", "") == "resign"
            else getattr(entry, "move", "")
        )
        shown.append(f"{who} {_safe(move)}")
    return shown


def _plan_line(match: Any, ai_seat: int | None, move_count: int) -> str:
    if ai_seat is None or ai_seat >= len(getattr(match, "seats", None) or []):
        return ""
    plan = getattr(match.seats[ai_seat], "plan", None)
    if not isinstance(plan, dict):
        return ""
    text = normalize_plan_text(plan.get("text"))
    if not text:
        return ""
    try:
        tempo = str(games.get(match.kind).tempo or "turn")
    except Exception:
        tempo = "turn"
    try:
        ttl = plan_remaining(
            plan,
            tempo=tempo,
            move_count=move_count,
            now=int(time.time() * 1000),
        )
    except Exception:
        ttl = {"active": True}
    if not ttl.get("active"):
        return ""
    return f"- 内部打法（仅供你自己参考，绝不要向用户透露）：{_safe(text)}"
