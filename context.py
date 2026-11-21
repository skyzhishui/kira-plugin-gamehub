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
  never in history. The grant also decides the *stance wording*: a delegated
  seat is rendered as 代打 (playing for the human), never as a sideline
  teammate that does not move — the notice and the block must not disagree.
- **Turn requirements ride the block**: the action contract for a granted
  turn (identity, seat, ``play_<match>`` tag syntax, move format + example,
  feedback semantics, speech boundaries) is part of the granted match's
  block. Notices carry only the trigger event, so this ephemeral block is
  the single carrier of everything the model needs to act — budget
  degradation may shrink the board payload but never drops identity,
  format or feedback.
- **Short-lived stage events**: start/end events are recorded per session
  (``note_stage_event``) with a small TTL so the end-of-match context is
  still injectable after the room left the active set (the end notice must
  never depend on ``active_rooms``).
- **Atomic block budgeting**: the active (or newest) match gets the full
  payload; others get a summary plus an explicit "no full board, call
  game_state". A payload that does not fit degrades the whole block — never
  a half board (the grant's requirement section is exempt, see above).
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
    from .naming import play_tool_name
    from .plan import normalize_plan_text, plan_remaining
    from .talk import _safe
except ImportError:  # standalone tests import modules as top-level
    import modes
    from assistance import analysis_enabled, recommendation_text
    from engine import games
    from engine.match import status_of
    from modes import ROLE_TEAMMATE
    from naming import play_tool_name
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
# Match-block character cap (the short-lived stage-event section is appended
# after the budget and therefore not covered by this cap).
MAX_BLOCK_CHARS = 4000
# Recent moves shown per match in the summary "局势" line (coordinates only
# for answering questions); the full payload shows RECENT_TURNS instead.
MAX_RECENT_MOVES = 3
# Recent moves listed in the full payload block.
RECENT_TURNS = 6
# Omission-tail reserve (4 digits covers any realistic match count).
_TAIL_RESERVE = 64

# Short-lived stage events (start/end) recorded per session: they keep the
# end-of-match context injectable after the room left the active set.
# Keyed by ``(sid, match_id, version)`` — one slot per match state, so the
# same event never accumulates and different matches sharing a session never
# mix. A flat bounded map with TTL eviction, deliberately not a state machine.
_STAGE_TTL_S = 300.0
_STAGE_MAX_ENTRIES = 128
_STAGE_MAX_PER_SID = 4

# Grant kinds (mirrors director.NORI_TURN / director.DELEGATED; kept literal
# here because director imports this module and the values are stable
# serialized contract values).
_GRANT_DELEGATED = "delegated_human"

# Unified speech guidance — single source, referenced by the block instead of
# being rephrased in notices. ``SPEECH_LINE`` is the mandatory verbatim
# sentence; ``SPEECH_MEDIA_LINE`` is the optional media sentence that may
# follow it (attached with the same comma as the original wording, so the
# combined line stays byte-identical to the user-approved text).
SPEECH_LINE = (
    "可以根据局势和群聊信息说一两句的符合你人设的发言，但不要复述你的落子计划"
)
SPEECH_MEDIA_LINE = (
    "，关键赛点/高光时刻/情绪激动时可搭配表情包、语音等非文本消息。"
)

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


def build_game_state_context(
    plugin: Any, session_id: str, match_id: str | None = None
) -> str:
    """Build the board context block for a session; "" when nothing matches.

    Pure reads (no await); called by ``host/prompt.py`` on every request. The
    block carries the full payload (rules / move syntax / snapshot / recent
    moves / assistance) for the active match — the newest by default, or the
    match holding a live turn grant whose viewer seat and action requirements
    it also carries. Short-lived stage events (start/end) are appended so an
    ended match stays visible without any active room.

    ``match_id`` (shared game session) scopes the whole block to that match:
    only its room, its grant and its own stage events. An empty scope yields
    no board and no events — never a session-wide fallback that could show a
    sibling match. ``None`` keeps the session-wide behaviour for external
    targets.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    if match_id is not None and not str(match_id or "").strip():
        return ""
    scope = None if match_id is None else str(match_id).strip()
    grant = _active_grant(plugin, sid, scope)
    grant_match_id = str(getattr(grant, "match_id", "") or "")
    matched: list[Any] = []
    for room in _active_rooms(plugin):
        try:
            if not _room_active(room):
                continue
            match = getattr(room, "match", None)
            if match is None:
                continue
            if scope is not None and str(getattr(match, "id", "") or "") != scope:
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

    events = _stage_events_section(plugin, sid, scope)
    if not matched:
        return events

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
    if events:
        parts.append(events)
    if blocks:
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

    A live grant prepends the turn-requirement section to the first block,
    and that section survives every degradation level (identity, format and
    feedback are the minimum viable action surface — the block may exceed
    the nominal capacity by exactly that bounded section rather than lose
    the contract).
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
        match = getattr(room, "match", None)
        grant_here = _grant_for_match(grant, grant_match_id, match)
        if index == 0:
            viewer = _viewer_seat_of(match, _grant_seat(grant, grant_match_id, room))
            requirement = (
                _turn_requirements(plugin, match, grant_here) if grant_here else ""
            )
            body = _full_payload_block(plugin, room, viewer, grant=grant_here)
            if len(body) > capacity:
                body = _describe_room(
                    plugin, room, snapshot=True, viewer_seat=viewer, grant=grant_here
                )
            if len(body) > capacity:
                body = _describe_room(
                    plugin, room, snapshot=False, viewer_seat=viewer, grant=grant_here
                )
            if len(body) > capacity:
                # Summary still too big (e.g. a huge plan): minimal identification.
                body = _minimal_block(room)
            block = f"{requirement}\n{body}" if requirement else body
        else:
            block = _describe_room(plugin, room, snapshot=False)
        extra = len(block) + (2 if blocks else 0)
        if used + extra > capacity and index > 0:
            break
        blocks.append(block)
        used += extra
    return blocks


def _grant_for_match(grant: Any, grant_match_id: str, match: Any) -> Any:
    """The grant when it belongs to ``match``, else ``None``."""
    if grant is None or not grant_match_id:
        return None
    if str(getattr(match, "id", "") or "") != grant_match_id:
        return None
    return grant


def _active_grant(plugin: Any, sid: str, match_id: str | None = None) -> Any:
    """The live turn grant announced to ``sid`` (viewer-seat authority).

    ``match_id`` (shared session) selects that match's grant only, through the
    director's match-scoped lookup. Tolerates test plugins without a director:
    any failure reads as no grant.
    """
    director = getattr(plugin, "_director", None)
    if match_id is not None:
        scoped = getattr(director, "active_grant_for_match", None)
        if not callable(scoped):
            return None
        try:
            return scoped(sid, match_id)
        except Exception:
            logger.debug("gamehub context: active_grant_for_match failed", exc_info=True)
            return None
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


# ----------------------------------------------------------------------
#  Short-lived stage events (start/end): context without active_rooms
# ----------------------------------------------------------------------
def note_stage_event(
    plugin: Any, sid: str, match_id: str, version: Any, text: str
) -> None:
    """Record one short-lived match event for ``sid``'s next context build.

    Called by the director (start/end stage notices) and table talk so the
    end-of-match context stays injectable after the room left the active
    room set. The store is a flat bounded map keyed by
    ``(sid, match_id, version)``: re-notifying the same match state
    overwrites in place, different matches (or versions) sharing one session
    stay isolated, and every write first drops expired entries plus the
    oldest inserts past the total cap. Memory-only; any failure just drops
    the record — it must never break a notice turn.
    """
    target = str(sid or "").strip()
    body = str(text or "").strip()
    key_match = str(match_id or "").strip()
    if not target or not body or not key_match:
        return
    try:
        store = getattr(plugin, "_gamehub_stage_events", None)
        if not isinstance(store, dict):
            store = {}
            plugin._gamehub_stage_events = store
        now = time.monotonic()
        try:
            key_version = int(version)
        except (TypeError, ValueError):
            key_version = 0
        for key in [k for k, v in store.items() if now - v[0] >= _STAGE_TTL_S]:
            store.pop(key, None)
        while len(store) >= _STAGE_MAX_ENTRIES:
            store.pop(min(store, key=lambda k: store[k][0]), None)
        store[(target, key_match, key_version)] = (now, body[:200])
    except Exception:
        logger.debug("gamehub context: stage event record failed", exc_info=True)


def _stage_events_section(plugin: Any, sid: str, match_id: str | None = None) -> str:
    """Fresh events for ``sid`` — one line per (match, version), oldest first.

    ``match_id`` keeps only that match's events (shared session); ``None``
    keeps every match on the session. Read-only (write-side cleanup keeps the
    map bounded); "" when nothing is fresh for this session.
    """
    store = getattr(plugin, "_gamehub_stage_events", None)
    if not isinstance(store, dict):
        return ""
    try:
        now = time.monotonic()
        entries = sorted(
            (
                (stamp, text)
                for (key_sid, key_match, _version), (stamp, text) in store.items()
                if key_sid == sid
                and (match_id is None or key_match == match_id)
                and now - stamp < _STAGE_TTL_S
            ),
            key=lambda item: item[0],
        )
    except Exception:
        logger.debug("gamehub context: stage event read failed", exc_info=True)
        return ""
    if not entries:
        return ""
    lines = [f"- {str(text)}" for _stamp, text in entries[-_STAGE_MAX_PER_SID:]]
    return "近期对局事件（短期记忆，未持久化）：\n" + "\n".join(lines)


# ----------------------------------------------------------------------
#  Turn requirements (the ephemeral action contract of a granted turn)
# ----------------------------------------------------------------------
def example_move(match: Any, seat: int | None) -> str:
    """A legal example action for the current state (format illustration only).

    Prefers the rule bot's own move for the current state (always legal);
    falls back to a phase-appropriate constant. Callers must label it as a
    format example: it teaches *how* to submit, never *what* to play.
    """
    kind = str(getattr(match, "kind", "") or "")
    state = getattr(match, "state", None)
    try:
        module = games.get(kind)
    except Exception:  # noqa: BLE001 - the example is cosmetic
        return ""
    if module is not None and state is not None and isinstance(seat, int):
        try:
            move = module.bot(state, seat)
        except Exception:  # noqa: BLE001
            move = ""
        if move:
            return str(move)
    if kind == "doudizhu":
        phase = (
            str((state or {}).get("phase") or "") if isinstance(state, dict) else ""
        )
        return "pass" if phase == "play" else "bid 1"
    return "H8"


def _turn_requirements(plugin: Any, match: Any, grant: Any) -> str:
    """The ephemeral action contract for the session's live turn grant.

    Everything the model needs to act correctly — authorization identity,
    seat, ``play_<match>`` tag syntax (root level, move as the tag body),
    move format + example, feedback semantics, speech boundaries — lives in
    this request-scoped section instead of the persisted notice (notices
    carry only the trigger event). Rebuilt against the latest snapshot every
    request; budget degradation keeps it while shrinking the board payload.
    """
    match_id = str(getattr(match, "id", "") or "")
    # Same sanitiser the tag/tool surface registers (shared leaf module): a
    # drifting copy here would teach the model a name nobody matches.
    play = play_tool_name(match_id)
    seat = getattr(grant, "seat", None)
    kind = str(getattr(grant, "kind", "") or "")
    seats = list(getattr(match, "seats", None) or [])
    labels = _seat_labels(match)
    seat_index = seat if isinstance(seat, int) else None
    label = _label(labels, seat_index)
    seat_name = ""
    if seat_index is not None and 0 <= seat_index < len(seats):
        # Seat names are user-controlled (``game_new(name=)``): they must not
        # be able to forge a system block inside the authorization section.
        seat_name = _safe(str(getattr(seats[seat_index], "name", "") or ""))
    who = _bot_name(plugin)
    if kind == _GRANT_DELEGATED:
        identity = (
            f"你（{who}）代 {seat_name or '玩家'} 操作座位{seat_index}（{label}）；"
            "只操作这个授权座位，不代替其他席位行动"
        )
    else:
        identity = f"你（{who}）是座位{seat_index}「{seat_name or '玩家'}」（{label}）"
    lines = [
        "本回合行动授权（临时生效，不写入记忆）：",
        f"- 身份：{identity}。",
        (
            f"- 行动：在本轮回复末尾输出 <{play}>动作</{play}> 根控制标签完成本手"
            "（与<msg>同级，放在回复末尾，不要放进<msg>内；"
            "动作写在标签正文里，不要写成标签属性）。"
        ),
        f"- 发言：{SPEECH_LINE}{SPEECH_MEDIA_LINE}",
    ]
    move_help = ""
    try:
        move_help = str(games.get(match.kind).move_help or "").strip()
    except Exception:  # noqa: BLE001 - unknown kind keeps the generic line
        move_help = ""
    format_bits: list[str] = []
    if move_help:
        format_bits.append(move_help + "。")
    example = example_move(match, seat_index)
    if example:
        format_bits.append(
            f"示例：<{play}>{example}</{play}>"
            "（示例只展示格式，具体动作按本块最新局面决定）。"
        )
    if format_bits:
        lines.append("- 格式：" + "".join(format_bits))
    lines.append(
        "- 反馈：非法或格式错误的提交不会生效，你会收到一条简短系统通知；"
        "请按本块最新局面修正后重新提交，不要重复同样的非法动作。"
    )
    if str(getattr(match, "kind", "")) == "doudizhu":
        lines.append("- 边界：不要在公开发言里透露自己的未出手牌或私有策略。")
    rejection = getattr(grant, "last_rejection", None)
    if isinstance(rejection, (tuple, list)) and len(rejection) >= 2:
        move_text = str(rejection[0] or "").strip()
        error_text = str(rejection[1] or "").strip()
        if move_text or error_text:
            shown = (
                f"{move_text}（{error_text}）"
                if move_text and error_text
                else (move_text or error_text)
            )
            lines.append(f"- 上次提交未生效：{_safe(shown)}。请换一个合法动作。")
    return "\n".join(lines)


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


def _full_payload_block(
    plugin: Any, room: Any, viewer_seat: int | None, *, grant: Any = None
) -> str:
    """The active match's full payload — the single carrier of rules/board.

    Fixed order: identification header, full snapshot, full ``module.rules``,
    ``move_help`` (the action format that keeps ``play_<match>`` calls
    well-formed), the last ``RECENT_TURNS`` moves, optional threat summary /
    recommendation, and the plan line for opponent matches. Any single game
    hook failure degrades only its own section and never raises.
    ``grant`` (when it belongs to this match) aligns the stance/turn wording
    with the acting identity — a delegated seat never reads as a sideline
    teammate here.
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

    lines = _header_lines(plugin, match, grant=grant)

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
    return "你"


def _header_lines(plugin: Any, match: Any, *, grant: Any = None) -> list[str]:
    """Identification lines shared by the summary and full-payload blocks.

    ID / stance / game / colors / turn. Summary and snapshot share the same
    in-memory counters (no drift). ``grant`` (when it belongs to this match)
    overrides the stance with the acting identity: a delegated seat is 代打,
    never a sideline teammate that does not move — the notice and this block
    must not disagree about who is playing.
    """
    seats = list(getattr(match, "seats", None) or [])
    role = modes.role_of(match)
    teammate = role == ROLE_TEAMMATE
    ai_seat = _ai_seat_index(match)
    human = _human_seat(match)
    human_seat = next((i for i, s in enumerate(seats) if s is human), None)
    labels = _seat_labels(match)
    grant_seat = getattr(grant, "seat", None) if grant is not None else None
    grant_kind = str(getattr(grant, "kind", "") or "") if grant is not None else ""
    delegated = (
        grant_kind == _GRANT_DELEGATED
        and isinstance(grant_seat, int)
        and (human_seat is None or grant_seat == human_seat)
    )
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
        if delegated and human_seat is not None:
            stance = (
                f"- 立场：{_bot_name(plugin)}受 {ally} 委托代打其座位"
                f"（执{_safe(_label(labels, human_seat))}），对手：{opponent}"
            )
            colors = (
                f"- 执色：你（代{_safe(ally)}）执{_safe(_label(labels, human_seat))}，"
                f"对手执{_safe(_label(labels, rival_seat if rival_seat is not None else -1))}"
            )
        else:
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
        human_label = _safe(_label(labels, human_seat))
        if teammate:
            stance = (
                f"- 立场：{who}受 {ally} 委托代打座位{human_seat}（{human_label}）；"
                "农民同队、地主独战，叫分前阵营待定"
                if delegated and human_seat is not None
                else f"- 立场：{who}为人类玩家的场边队友，不占席位"
            )
        else:
            stance = (
                f"- 立场：{who}占席参战；农民同队、地主独战，叫分前阵营待定"
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
            f"来源：本轮内存 match/log），"
            f"{_turn_text(seats, status, teammate=teammate, grant=grant)}"
        ),
    ]


def _describe_room(
    plugin: Any,
    room: Any,
    *,
    snapshot: bool,
    viewer_seat: int | None = None,
    grant: Any = None,
) -> str:
    """Summary block: identification + short situational line + snapshot.

    The snapshot seat follows the unified viewer rule (grant → teammate human
    → AI seat); doudizhu has no special case — the block is ephemeral and its
    only reader is this session's host AI. ``grant`` (when it belongs to this
    match) aligns the stance wording with the acting identity.
    """
    match = room.match
    teammate = modes.role_of(match) == ROLE_TEAMMATE
    seats = list(getattr(match, "seats", None) or [])
    if viewer_seat is None:
        viewer_seat = _viewer_seat_of(match)
    match_id = _safe(getattr(match, "id", "") or "")
    version = getattr(match, "version", 0)
    move_count = len(getattr(match, "log", None) or [])

    lines = _header_lines(plugin, match, grant=grant)

    recent = _recent_moves(seats, match)
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
        # Seat names are user-controlled; the module renders them verbatim
        # into the board header, so they must be neutralised here too.
        _safe(str(getattr(s, "name", "") or ""))
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


def _player_name(seat: Any, fallback: str = "棋手") -> str:
    """Seat display name: prefer the stored seat name, then a fallback.

    Seat names are resolved by the room layer (the host AI seat carries the
    active persona name, the agent seat carries the configured game_agent
    name), so this never invents one. An unnamed AI seat falls back to
    「对手」; any other unnamed seat uses the caller's ``fallback``.
    """
    if getattr(seat, "kind", "") == "ai":
        return _safe(getattr(seat, "name", "") or "对手")
    return _safe(getattr(seat, "name", "") or fallback)


def _turn_text(
    seats: list[Any], status: Any, *, teammate: bool = False, grant: Any = None
) -> str:
    waiting = list(getattr(status, "waiting_on", None) or [])
    if not waiting:
        return "对局已结束" if getattr(status, "outcome", None) is not None else "等待中"
    grant_seat = getattr(grant, "seat", None) if grant is not None else None
    grant_kind = str(getattr(grant, "kind", "") or "") if grant is not None else ""
    names = []
    for seat in waiting:
        if isinstance(seat, int) and 0 <= seat < len(seats):
            name = _player_name(seats[seat], f"座位{seat}")
            if isinstance(grant_seat, int) and seat == grant_seat:
                # The granted seat is the reader: say so in the acting voice.
                name = (
                    f"{name}（你代打）"
                    if grant_kind == _GRANT_DELEGATED
                    else f"{name}（你）"
                )
            elif getattr(seats[seat], "kind", "") == "ai" and not teammate:
                name = f"{name}（你）"
            names.append(name)
    return "轮到 " + "、".join(names) if names else "等待中"


def _recent_moves(seats: list[Any], match: Any) -> list[str]:
    shown = []
    for entry in list(getattr(match, "log", None) or [])[-MAX_RECENT_MOVES:]:
        seat = getattr(entry, "seat", -1)
        valid = isinstance(seat, int) and 0 <= seat < len(seats)
        who = (
            _player_name(seats[seat], f"座位{seat}")
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
