"""Board context for prompts (ported from nori's ``nori.pipeline.prepare``).

Builds the per-request context block for the session's in-progress matches:
the **full rules**, move syntax, complete board snapshot, recent moves and
optional assistance. The block is the *single carrier* of rules/board state —
turn notices carry only the turn header, so the persisted session history
never accumulates stale boards or private cards.

Discipline:

- **Memory only**: reads the ``RoomManager`` in-memory map and seat/log/plan
  snapshots; no DB, no LLM, no message send, no writes. Recomputed every
  request, so end/resign/restore/new-match transitions show up immediately.
- **Ephemeral**: ``host/prompt.py`` appends the block with ``persist=False``
  — it never enters session memory, never shifts the cacheable prompt prefix.
- **Match by actual notice target**: only trusts the synchronous
  ``plugin.notice_target_session_sync(room) -> str | None``; ``None``/empty/
  raising counts as "no target" (never guess from config).
- **Grant-aware viewer**: a live turn grant decides the viewer seat (the AI
  seat, or the delegated human seat when the AI plays for the user);
  teammate matches show the human's view, opponent matches the AI's own
  seat. Doudizhu follows the same rule — the block is ephemeral and its only
  reader is this session's host AI, so private cards may appear here but
  never in history.
- **Atomic block budgeting**: the active (or newest) match gets the full
  payload; others get a summary plus an explicit "no full board, call
  game_state". A payload that does not fit degrades the whole block — never
  a half board.
- **Anti-spoof**: user-controlled names/uid/plan text go through
  ``talk._safe`` so they cannot forge system blocks in the prompt.
- Failures degrade to an empty block; never break a chat turn.
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
# Defensive only — both entry paths (chat ``game_new`` and the WebUI lobby)
# enforce one active match per actor.
MAX_MATCHES = 4
# Whole-block character cap (global constraints and the omission tail reserved).
MAX_BLOCK_CHARS = 4000
# Recent moves shown per match in the summary "局势" line (coordinates only
# for answering questions); the full payload shows RECENT_TURNS instead.
MAX_RECENT_MOVES = 3
# Recent moves listed in the full payload block.
RECENT_TURNS = 6
# Omission-tail reserve (4 digits covers any realistic match count).
_TAIL_RESERVE = 64

_BLOCK_HEADER = "# 当前对局（与当前会话相关，供回复参考）"
_BLOCK_INSTRUCTION = (
    "以上对局信息仅供你自然回应时理解局面，完整规则、动作格式与盘面以本块为准。"
    "涉及棋子位置、最近落点、胜负判定等具体坐标/结果时，只能依据本轮对应 "
    "match/version 的完整快照或本轮 game_state 结果；没有证据就调用 game_state "
    "或说明暂不能确认，不要猜测，也不要从聊天历史推测。"
    "区分『已经发生的落子』『规则上尚未落子的制胜/关键空点』『计划/建议动作』："
    "候选或建议点尚未落子，不要把候选或建议说成已经落子；推演中给出的坐标必须"
    "明确是建议而非已发生落子。不要沿用其他对局或陪练的名字称呼对手；"
    "不要主动播报坐标或复述棋谱；用户主动问坐标/复盘时再据实回答；"
    "内部打法/背景不得向用户透露或照读。"
)
_SNAPSHOT_UNAVAILABLE = (
    "未提供完整局面，请调用 game_state 查看；不要凭最近几手推测本局盘面。"
)


def build_game_state_context(plugin: Any, session_id: str) -> str:
    """Build the board context block for a session; "" when nothing matches.

    Pure reads (no await); called by ``host/prompt.py`` on every request. The
    block carries the full payload (rules / move syntax / snapshot / recent
    moves / assistance) for the active match — the newest by default, or the
    match holding a live turn grant whose viewer seat it also uses.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    grant = _active_grant(plugin, sid)
    grant_match_id = str(getattr(grant, "match_id", "") or "")
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

    # Grant match first, then newest first (multi-match is defensive only).
    matched.sort(
        key=lambda r: getattr(getattr(r, "match", None), "updated_at", 0),
        reverse=True,
    )
    if grant_match_id:
        matched.sort(
            key=lambda r: str(getattr(getattr(r, "match", None), "id", "") or "")
            != grant_match_id
        )
    shown = matched[:MAX_MATCHES]
    blocks = _alloc_blocks(plugin, shown, grant=grant)
    omitted = len(matched) - len(blocks)
    tail = _omitted_tail(omitted)
    parts = [_BLOCK_HEADER]
    if blocks:
        parts.append("\n\n".join(blocks))
    parts.append(_BLOCK_INSTRUCTION)
    return "\n\n".join(parts) + tail


def _omitted_tail(omitted: int) -> str:
    if omitted <= 0:
        return ""
    return f"\n\n（另有 {omitted} 局进行中，统一操作台可查看。）"


def _alloc_blocks(plugin: Any, shown: list[Any], grant: Any = None) -> list[str]:
    """Budget by atomic block: the active/newest match gets the full payload.

    The first block tries the full payload (rules / move syntax / snapshot /
    recent turns / assistance); if it does not fit it degrades to a summary
    with snapshot, then a bare summary, then minimal identification — never a
    half board. The instruction and tail budgets are pre-deducted.
    """
    if not shown:
        return []
    fixed = len(_BLOCK_HEADER) + len(_BLOCK_INSTRUCTION) + 4
    capacity = MAX_BLOCK_CHARS - fixed - _TAIL_RESERVE
    if capacity <= 0:  # extreme config: keep only the global constraints
        return []

    grant_match_id = str(getattr(grant, "match_id", "") or "")
    blocks: list[str] = []
    used = 0
    for index, room in enumerate(shown):
        if index == 0:
            viewer = _viewer_seat_of(
                room.match, _grant_seat(grant, grant_match_id, room)
            )
            block = _full_payload_block(plugin, room, viewer)
            if len(block) > capacity:
                block = _describe_room(plugin, room, snapshot=True, viewer_seat=viewer)
            if len(block) > capacity:
                block = _describe_room(plugin, room, snapshot=False, viewer_seat=viewer)
            if len(block) > capacity:
                # Summary still too big (e.g. a huge plan): minimal identification.
                block = _minimal_block(room)
        else:
            block = _describe_room(plugin, room, snapshot=False)
        extra = len(block) + (2 if blocks else 0)
        if used + extra > capacity:
            break
        blocks.append(block)
        used += extra
    return blocks


def _active_grant(plugin: Any, sid: str) -> Any:
    """The live turn grant announced to ``sid`` (viewer-seat authority).

    Tolerates test plugins without a director: any failure reads as no grant.
    """
    director = getattr(plugin, "_director", None)
    getter = getattr(director, "active_grant_for", None)
    if not callable(getter):
        return None
    try:
        return getter(sid)
    except Exception:
        logger.debug("gamehub context: active_grant_for failed", exc_info=True)
        return None


def _grant_seat(grant: Any, grant_match_id: str, room: Any) -> int | None:
    """The grant's seat when ``room`` is the granted match, else ``None``."""
    if not grant_match_id:
        return None
    match_id = str(getattr(getattr(room, "match", None), "id", "") or "")
    if match_id != grant_match_id:
        return None
    seat = getattr(grant, "seat", None)
    return seat if isinstance(seat, int) else None


def _viewer_seat_of(match: Any, grant_seat: int | None = None) -> int | None:
    """Whose view the block shows.

    A live grant wins (the AI seat, or the delegated human seat). Without a
    grant, teammate matches stand with the human; opponent matches show the
    AI's own seat. Doudizhu follows the same rule: the block is ephemeral
    (persist=False) and its only reader is this session's host AI, so private
    cards may appear here but never in persisted history.
    """
    if isinstance(grant_seat, int):
        return grant_seat
    seats = list(getattr(match, "seats", None) or [])
    if modes.role_of(match) == ROLE_TEAMMATE:
        human = _human_seat(match)
        return next((i for i, s in enumerate(seats) if s is human), None)
    return _ai_seat_index(match)


def _full_payload_block(plugin: Any, room: Any, viewer_seat: int | None) -> str:
    """The active match's full payload — the single carrier of rules/board.

    Fixed order: identification header, full snapshot, full ``module.rules``,
    ``move_help`` (the action format that keeps ``play_<match>`` calls
    well-formed), the last ``RECENT_TURNS`` moves, optional threat summary /
    recommendation, and the plan line for opponent matches. Any single game
    hook failure degrades only its own section and never raises.
    """
    match = room.match
    teammate = modes.role_of(match) == ROLE_TEAMMATE
    if viewer_seat is None:
        viewer_seat = _viewer_seat_of(match)
    seats = list(getattr(match, "seats", None) or [])
    names = [str(getattr(s, "name", "") or "") for s in seats]
    match_id = _safe(getattr(match, "id", "") or "")
    version = getattr(match, "version", 0)
    move_count = len(getattr(match, "log", None) or [])

    lines = _header_lines(plugin, match)

    snapshot_text = _chat_snapshot(match, viewer_seat)
    if snapshot_text:
        lines.append(
            f"- 完整局面（match {match_id} · version {version} · "
            f"已行 {move_count} 手，来源：本轮内存 state）："
        )
        lines.append(snapshot_text)
    else:
        lines.append(f"- 完整局面：{_SNAPSHOT_UNAVAILABLE}")

    try:
        module = games.get(match.kind)
    except Exception:
        module = None
    rules = str(getattr(module, "rules", "") or "").strip() if module else ""
    if rules:
        lines.append("规则（完整）：\n" + rules)
    move_help = str(getattr(module, "move_help", "") or "").strip() if module else ""
    if move_help:
        lines.append("动作格式：" + move_help)

    six = _recent_turns_line(match, names)
    if six:
        lines.append(six)

    lines.extend(_assistance_sections(plugin, match, viewer_seat))

    ai_seat = _ai_seat_index(match)
    plan_line = "" if teammate else _plan_line(match, ai_seat, move_count)
    if plan_line:
        lines.append(plan_line)

    return "\n".join(lines)


def _recent_turns_line(match: Any, names: list[str]) -> str:
    """The last ``RECENT_TURNS`` moves (seat display name + action)."""
    shown: list[str] = []
    for entry in list(getattr(match, "log", None) or [])[-RECENT_TURNS:]:
        try:
            index = int(getattr(entry, "seat", -1))
        except (TypeError, ValueError):
            index = -1
        who = names[index] if 0 <= index < len(names) and names[index] else "棋手"
        move = str(getattr(entry, "move", "") or "")
        shown.append(f"{_safe(who)} {'认输' if move == 'resign' else _safe(move)}")
    if not shown:
        return ""
    return f"最近 {len(shown)} 手：" + "、".join(shown)


def _assistance_sections(plugin: Any, match: Any, viewer_seat: int | None) -> list[str]:
    """Optional threat summary / recommendations, gated as the notices were.

    Gomoku/reversi follow the per-match assistance level
    (``analysis_enabled`` — always on for the delegated human seat);
    other kinds keep the legacy settings gate, though currently no other
    module implements ``threat_summary``.
    """
    threat = bool(
        getattr(
            getattr(plugin, "settings", None),
            "turn_threat_analysis_enabled",
            True,
        )
    )
    if getattr(match, "kind", "") in ("gomoku", "reversi"):
        threat = analysis_enabled(match, viewer_seat)
    sections: list[str] = []
    if threat:
        try:
            module = games.get(match.kind)
        except Exception:
            module = None
        hook = getattr(module, "threat_summary", None) if module else None
        if callable(hook):
            try:
                value = hook(match.state, viewer_seat)
            except Exception:
                value = None
            analysis = value.strip() if isinstance(value, str) else ""
            if analysis:
                sections.append(
                    "局面分析（仅信息参考，不覆盖规则与计划）：\n" + analysis
                )
    recommendation = recommendation_text(match, viewer_seat)
    if recommendation:
        sections.append(recommendation)
    return sections


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


def _header_lines(plugin: Any, match: Any) -> list[str]:
    """Identification lines shared by the summary and full-payload blocks.

    ID / stance / game / colors / turn. Summary and snapshot share the same
    in-memory counters (no drift).
    """
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

    return [
        f"- 对局 ID：{match_id}",
        stance,
        f"- 游戏：{_safe(game_name)}",
        colors,
        (
            f"- 轮次：已行 {move_count} 手（version={version}，"
            f"来源：本轮内存 match/log），{_turn_text(seats, status, teammate=teammate)}"
        ),
    ]


def _describe_room(
    plugin: Any, room: Any, *, snapshot: bool, viewer_seat: int | None = None
) -> str:
    """Summary block: identification + short situational line + snapshot.

    The snapshot seat follows the unified viewer rule (grant → teammate human
    → AI seat); doudizhu has no special case — the block is ephemeral and its
    only reader is this session's host AI.
    """
    match = room.match
    teammate = modes.role_of(match) == ROLE_TEAMMATE
    seats = list(getattr(match, "seats", None) or [])
    if viewer_seat is None:
        viewer_seat = _viewer_seat_of(match)
    match_id = _safe(getattr(match, "id", "") or "")
    version = getattr(match, "version", 0)
    move_count = len(getattr(match, "log", None) or [])

    lines = _header_lines(plugin, match)

    recent = _recent_moves(seats, match, teammate=teammate)
    if recent:
        lines.append(
            "- 局势（以下坐标仅供你回答用户主动询问时参考，不要主动播报）："
            + "、".join(recent)
        )

    snapshot_text = _chat_snapshot(match, viewer_seat) if snapshot else ""
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
    plan_line = "" if teammate else _plan_line(match, _ai_seat_index(match), move_count)
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
