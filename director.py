"""TurnDirector: owner of notice-driven action turns (KiraAI port).

A "directed" match (``modes.driver == "turn"``, or a teammate match whose
human seat is under persistent delegation) has its move notice, its temporary
``play_<match>`` grant and its watchdog fallback owned by this module. Table
talk / the local judge / the plugin game agent all step aside.

What changed from nori:

- **Grants are request-scoped.** KiraAI has no ``ctx.session_tools``; the
  grant only remembers the session it was announced to and
  :meth:`TurnDirector.tools_for` hands ``play_<match>`` / ``state_<match>`` to
  the next request of that session (``host/prompt.py`` calls it). Revocation
  is implicit: no tools are added on the following request.
- **Notices go through ``host.notices.send_notice``** (``ctx.publish_notice``),
  so there is no dispatcher to wait for: the notice is published and the host
  chat pipeline replies on its own.
- **Identity**: the host persona name wins (``plugin.host_agent_name``, read
  from the active persona by the runtime); ``plugin.settings.agent_name``
  (default ``nori``, the game_agent identifier) is only the fallback.

Discipline (unchanged):

- ``on_event`` only schedules work; it never blocks the room lock;
- one in-flight grant per match; the watchdog re-reads the seat snapshot
  before every fill so a late or duplicate move can never land;
- a grant is bound to ``move_count``; a manual move that wins the race makes
  the grant stale instead of replaying it into a later turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

try:  # production: plugin package (``<plugin>.director``)
    from . import modes
    from .engine import games
    from .engine.match import status_of
    from .engine.types import GameModule
    from .tools import NOTICE_ACTOR
    from .tools_turn import build_turn_tools, play_tool_name, state_tool_name
except ImportError:  # standalone tests import these modules as top-level
    import modes
    from engine import games
    from engine.match import status_of
    from engine.types import GameModule
    from tools import NOTICE_ACTOR
    from tools_turn import build_turn_tools, play_tool_name, state_tool_name

try:  # production: plugin package
    from .host import notices as notice_channel
except ImportError:  # standalone tests: host adapter may be absent
    try:
        from host import notices as notice_channel
    except ImportError:  # pragma: no cover - host-less unit tests
        notice_channel = None  # type: ignore[assignment]

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

# Grant states.
ST_ACTIVE = "active"
ST_PLAYED = "played"
ST_FILLED = "filled"  # the watchdog filled the turn with the rule bot
ST_STALE = "stale"  # the position moved on; the grant is void
ST_CANCELLED = "cancelled"

_RESOLVED = (ST_PLAYED, ST_FILLED, ST_STALE, ST_CANCELLED)

# Grant kinds (also stored in ``event.extra["gamehub_turn"]["kind"]``).
NORI_TURN = "nori_turn"
DELEGATED = "delegated_human"

# Retention of resolved grants in memory (no scheduler; normal path replaces
# them on the next event).
_GRANT_RETENTION_S = 300.0
# A request mark older than this without any response counts as a dead run
# (failed/aborted) and stops marking the session busy (F3 TTL).
_LLM_RUN_TTL_S = 300.0
_STAGE_SEEN_LIMIT = 2048

DEFAULT_AGENT_NAME = "nori"


@dataclass
class PlayResult:
    """Typed result of :meth:`TurnDirector.submit_play`.

    - ``ok``: the move was legal and applied (the only success state);
    - ``terminal``: this turn's reply should end (success / lost grant /
      stale); ``False`` for a correctable mistake (illegal move / empty move);
    - ``status``: ``played`` / ``invalid`` / ``stale`` / ``gone`` /
      ``no_grant`` / ``no_room``;
    - ``text`` / ``error``: model-facing text (``error`` only when ``ok`` is
      false);
    - ``move``: the move that was actually applied ("" on failure);
    - ``say``: transport-only table talk; never set on a failed play.

    Legacy unpacking still works: ``text, terminal = result``.
    """

    ok: bool
    terminal: bool
    status: str
    text: str = ""
    error: str = ""
    move: str = ""
    say: str = ""

    def __iter__(self):
        """Backwards compatible ``text, terminal = await submit_play(...)``."""
        yield self.text
        yield self.terminal


@dataclass
class TurnGrant:
    """One action-turn grant (at most one in flight per match)."""

    match_id: str
    seat: int
    key: str
    kind: str = NORI_TURN
    # Session the notice was announced to ("" = no usable target → watchdog).
    session_id: str = ""
    # Synthetic author of the notice turn; matches ``tools.NOTICE_ACTOR``.
    actor: str = NOTICE_ACTOR
    move_count: int = 0
    # Monotonic silence deadline (``turn_play_timeout_s`` window; extended
    # by host progress, capped in F1 short-window mode).
    deadline: float = 0.0
    # Last observed host-side progress (step/tool results); 0 = never.
    last_progress: float = 0.0
    # Last observed *tool* result for the session; distinguishes a mid
    # multi-step turn from a finished text-only reply (F1 arming guard).
    last_tool_progress: float = 0.0
    # When the action notice was delivered (monotonic); responses belonging
    # to runs started before this never saw the notice (F1 arming guard).
    notice_sent_at: float = 0.0
    reason: str = ""
    created: float = field(default_factory=time.monotonic)
    state: str = ST_ACTIVE
    resolved_at: float = 0.0
    notice_sent: bool = False
    notice_session_id: str = ""
    # One explicit "you have not acted yet" reminder before the bot fill.
    nudged: bool = False
    # F1: corrective nudges for text-only replies (capped, shortened window).
    text_nudges: int = 0
    text_task: asyncio.Task[Any] | None = None
    # F1 short-window mode: once a corrective nudge fired, progress signals
    # can no longer push the deadline past ``short_deadline`` — only an open
    # LLM run still can, and never past ``short_ceiling``. 0 = normal mode.
    short_deadline: float = 0.0
    short_ceiling: float = 0.0
    turn_task: asyncio.Task[Any] | None = None
    watchdog: asyncio.Task[Any] | None = None


def _cfg(plugin: Any, name: str, default: Any) -> Any:
    settings = getattr(plugin, "settings", None)
    value = getattr(settings, name, default)
    return default if value is None else value


def _agent_name(plugin: Any) -> str:
    """Display name of whoever is being driven by the notice.

    Turn notices go to the **host** agent, so its persona name wins; the
    plugin-owned ``game_agent`` name is only the fallback (host unavailable).
    """
    for candidate in (
        getattr(plugin, "host_agent_name", ""),
        getattr(getattr(plugin, "settings", None), "agent_name", ""),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return DEFAULT_AGENT_NAME


def _seat_name(seat: Any, index: int, agent: str) -> str:
    name = str(getattr(seat, "name", "") or "").strip()
    if name:
        return name
    if getattr(seat, "kind", "") == "ai":
        return agent
    return f"座位{index}"


def _rooms_of(plugin: Any) -> Any:
    """The room manager, tolerant of host wiring order.

    ``host/runtime.py`` assigns the ``plugin.rooms`` property after
    ``restore_active()`` runs, so ``plugin._rooms`` and the runtime object are
    checked as well; a plugin assembled by tests only has ``rooms``.
    """
    for holder in (plugin, getattr(plugin, "runtime", None)):
        if holder is None:
            continue
        rooms = getattr(holder, "rooms", None)
        if rooms is not None:
            return rooms
        rooms = getattr(holder, "_rooms", None)
        if rooms is not None:
            return rooms
    return None


def _notice_target_of(plugin: Any) -> Any:
    """The async notice-target resolver, tolerant of host wiring order."""
    for holder in (plugin, getattr(plugin, "runtime", None)):
        if holder is None:
            continue
        resolver = getattr(holder, "notice_target", None)
        if callable(resolver):
            return resolver
    return None


def _mover_of(event: dict[str, Any], match: Any) -> int:
    """Seat that made the last move (embedded bot replies share one apply)."""
    log = getattr(match, "log", ()) or ()
    if log:
        try:
            return int(getattr(log[-1], "seat", -1))
        except (TypeError, ValueError):
            return -1
    try:
        return int(event.get("seat", -1))
    except (TypeError, ValueError, AttributeError):
        return -1


def _label(labels: list[str], seat: int | None, fallback: str = "棋手") -> str:
    if seat is None or not 0 <= seat < len(labels) or not labels[seat]:
        return fallback
    return str(labels[seat])


def _seat_labels(match: Any) -> list[str]:
    try:
        return list(games.get(match.kind).seat_labels(match.state) or [])
    except Exception:  # noqa: BLE001 - unknown game → unnamed seats
        return []


def _module_of(kind: str) -> Any:
    try:
        return games.get(kind)
    except Exception:  # noqa: BLE001 - unknown kind → no module
        return None


async def send_notice(plugin: Any, sid: str, text: str) -> bool:
    """Publish one notice through ``host.notices`` (patchable seam)."""
    sender = getattr(notice_channel, "send_notice", None) if notice_channel else None
    if not callable(sender):
        logger.warning("turn director: notice channel unavailable (sid=%r)", sid)
        return False
    try:
        return bool(await sender(plugin, sid, text))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a failed notice must not kill the turn
        logger.warning("turn director: notice publish failed (sid=%s)", sid, exc_info=True)
        return False


def build_turn_notice(
    match: Any,
    *,
    seat: int,
    kind: str,
    move_count: int,
    tool_name: str = "play",
    plugin: Any = None,
) -> str:
    """Action-turn notice: the turn header only.

    The notice is the turn trigger and carries only per-turn instructions:
    who moved, whose turn it is, the mandatory ``play_<match>`` call and the
    behavioral boundaries. The board snapshot, full rules, move syntax,
    recent moves and threat analysis ride the per-request
    ``gamehub_context`` block instead (``context.build_game_state_context``),
    so the persisted session history never accumulates stale boards or
    private cards. The header ends with a fallback pointer: if the injected
    block is missing, the model must call ``state_<match>`` before acting.

    - ``nori_turn``: the AI's own seat ("the opponent just played X; you are
      up");
    - ``delegated_human``: the delegated human seat ("the opponent answered
      X; you play this one for <human>");
    - a same-seat continuation (reversi auto-pass) says "you just played X;
      the opponent has no legal move" instead of blaming the opponent.

    The guidance asks for **one** ``play_<match>`` call in this reply and
    explicitly allows talking in the same reply (KiraAI has no separate
    envelope lane), so the model never invents an extra "say" tool call.
    """
    agent = _agent_name(plugin)
    state_tool = state_tool_name(str(getattr(match, "id", "") or ""))
    fallback_line = (
        "盘面与规则见本轮 gamehub_context 注入块；"
        f"若缺失，先调用 {state_tool} 查看局面再行动。"
    )

    if getattr(match, "kind", "") == "doudizhu":
        seats = list(getattr(match, "seats", ()) or ())
        names = [_seat_name(s, i, agent) for i, s in enumerate(seats)]
        labels = _seat_labels(match)
        players = "，".join(
            f"座位{i}「{name}」（{_label(labels, i)}）" for i, name in enumerate(names)
        )
        human_name = names[seat] if 0 <= seat < len(names) else "玩家"
        identity = (
            f"你（{agent}）正在代{human_name}操作座位{seat}"
            if kind == DELEGATED
            else f"你是座位{seat}「{human_name}」"
        )
        return (
            f"[System: 斗地主行动回合：{players}。{identity}。"
            "这是你的对局回合：必须在本轮回复里调用 " + tool_name + " 工具提交动作"
            "——只在正文里写出牌不算数，没有这次调用就等于你没有行动，"
            "系统会替你随手打一张。"
            "一次回复只调用一次；同一次回复可以聊公开牌型、调侃与局势气氛，"
            "但禁止透露自己的未出手牌或私有策略。"
            "不得代替其他席位行动，不得沿用其他局的名字。注意你是玩家，不是解说。"
            f"{fallback_line}]"
        )

    seats = list(getattr(match, "seats", ()) or ())
    names = [_seat_name(s, i, agent) for i, s in enumerate(seats)]
    labels = _seat_labels(match)
    status = status_of(match)
    log = getattr(match, "log", ()) or ()
    mover = _mover_of({"seat": -1}, match)
    move = ""
    if log:
        move = str(getattr(log[-1], "move", "") or "")
    mover_name = names[mover] if 0 <= mover < len(names) else "对手"
    seat_label = _label(labels, seat)
    same_seat = mover == seat
    auto_pass = bool((getattr(match, "state", None) or {}).get("passed"))
    continuation = (
        "对手无合法落点，已自动过手，现在仍轮到你" if auto_pass else "现在仍轮到你"
    )
    identity_line = "注意你是棋手，不是解说。"
    call_line = (
        f"这是你的对局回合：必须在本轮回复里调用 {tool_name} 工具落子——"
        "只在正文里写出坐标不算数，没有这次调用就等于你没有落子，"
        "系统会替你随手下一手。"
        "一次回复只调用一次；同一次回复可以照常说一两句玩家口吻的短话，"
        "关键赛点/高光时刻/情绪激动时可搭配表情包、语音等非文本消息。"
    )

    if kind == DELEGATED:
        human_name = names[seat] if 0 <= seat < len(names) else "玩家"
        if same_seat:
            head = (
                f"[System: 对局通知 · {move_count} 手：你（{agent}，代 {human_name} "
                f"执{seat_label}）刚落子 {move or '一手'}；{continuation}，"
                "这一手仍由你代下。"
                f"{call_line}"
                f"{identity_line}"
                "不要代替对手落子。"
                f"{fallback_line}]"
            )
        else:
            head = (
                f"[System: 对局通知 · {move_count} 手：对手 {mover_name} 刚落子 "
                f"{move or '一手'}。{human_name} 已把这一局的人类座位委托给你"
                f"（{agent}），这一手由你代下（执{seat_label}）。"
                f"{call_line}"
                f"{identity_line}"
                "不要代替对手落子。"
                f"{fallback_line}]"
            )
    elif same_seat:
        head = (
            f"[System: 对局通知 · {move_count} 手：你（{agent}）刚落子 "
            f"{move or '一手'}（执{seat_label}）；{continuation}。"
            f"{call_line}"
            f"{identity_line}"
            f"{fallback_line}]"
        )
    else:
        head = (
            f"[System: 对局通知 · {move_count} 手：{mover_name} 刚落子 "
            f"{move or '一手'}，现在轮到你（{agent}，执{seat_label}）。"
            f"{call_line}"
            f"{identity_line}"
            f"{fallback_line}]"
        )

    if status.outcome is not None:
        head += (
            "\n对局已结束：" + str(status.result_text or status.outcome.text)
        )
    return head


def build_stage_notice(match: Any, stage: str, *, plugin: Any = None) -> str:
    """Tool-free start/end notice: result + seats, no board, no grant.

    Only used for ``driver=turn`` matches (teammate matches keep table talk).
    """
    agent = _agent_name(plugin)
    module = _module_of(getattr(match, "kind", ""))
    title = str((getattr(module, "name", None) or {}).get("zh") or match.kind)
    labels = _seat_labels(match)
    seats = list(getattr(match, "seats", ()) or ())
    names = [_seat_name(s, i, agent) for i, s in enumerate(seats)]
    nori_seat = modes.nori_seat_index(match)
    parts: list[str] = []
    for index, name in enumerate(names):
        label = _label(labels, index)
        if index == nori_seat:
            parts.append(f"你（{agent}）执{label}")
        else:
            parts.append(f"{name} 执{label}")
    seat_desc = "，".join(parts)
    if stage == "start":
        return (
            f"[System: 对局开始 · {title}：{seat_desc}。"
            "这是你们的对局，不是解说。请用一两句玩家口吻自然开场；"
            "不用调用对局工具，想表达可以配合表情或语音。]"
        )
    status = status_of(match)
    result = status.result_text or (
        status.outcome.text if status.outcome is not None else "对局结束"
    )
    return (
        f"[System: 对局结束 · {title}：{result}。{seat_desc}。"
        "请用一两句玩家口吻自然回应；不用调用对局工具。]"
    )


def _example_move(match: Any, seat: int) -> str:
    """A legal example move for the nudge's tool-call syntax line (F2).

    Prefers the rule bot's own move for the current state (always legal);
    falls back to a phase-appropriate constant. The nudge labels it as a
    format example, so the model is told *how* to call, not *what* to play.
    """
    kind = str(getattr(match, "kind", "") or "")
    state = getattr(match, "state", None)
    try:
        module = games.get(kind)
    except Exception:  # noqa: BLE001 - the example is cosmetic
        module = None
    if module is not None and state is not None:
        try:
            move = module.bot(state, seat)
        except Exception:  # noqa: BLE001
            move = ""
        if move:
            return str(move)
    if kind == "doudizhu":
        phase = str((state or {}).get("phase") or "") if isinstance(state, dict) else ""
        return "pass" if phase == "play" else "bid 1"
    return "H8"


def build_nudge_notice(
    match: Any,
    *,
    seat: int,
    tool_name: str,
    plugin: Any = None,
    text_only: bool = False,
) -> str:
    """Short reminder for a turn the host answered without acting (F2).

    The model sometimes replies to the action notice with plain text (e.g.
    "这手牌有王有对子，我抢地主。") instead of calling ``play_<match>`` — the
    decision is right, only the format is wrong, which players perceived as
    "the opponent timed out". The reminder therefore spells out the exact
    call syntax with a legal example move, so even a failover-tier model
    cannot misread *how* to submit.

    ``text_only=True`` is the immediate corrective variant (F1): sent right
    after a completed reply that did not submit, with a shortened deadline.
    """
    agent = _agent_name(plugin)
    labels = _seat_labels(match)
    seat_label = _label(labels, seat)
    example = _example_move(match, seat)
    opening = (
        f"[System: 你（{agent}，座位{seat}，{seat_label}）刚才只发了文字，"
        "没有调用工具提交动作。"
        if text_only
        else f"[System: 你（{agent}，座位{seat}，{seat_label}）还没有提交动作。"
    )
    return (
        opening
        + f"请立刻在这一次回复里调用 {tool_name} 工具提交，"
        f'调用格式示例：move 参数填 "{example}"'
        "（示例只展示格式，具体动作请按你的手牌/当前盘面决定）。"
        "只在正文里写结果不算行动。如果这次仍不调用，系统会替你随手出一手。]"
    )


class TurnDirector:
    """Grant table + watchdog + notice delivery for one plugin instance.

    Never holds the room lock across network/notice work: every room read goes
    through ``turn_snapshot`` / ``apply_*`` which lock internally.
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._grants: dict[str, TurnGrant] = {}
        # Per-match reservation: closes the check → await snapshot → insert
        # window so two concurrent events cannot mint two grants.
        self._reservations: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False
        # Start/end notices: once per (match, stage); inflight prevents
        # concurrent duplicates, and a failed send can be retried.
        self._stage_seen: dict[tuple[str, str], None] = {}
        self._stage_inflight: set[tuple[str, str]] = set()
        # F3: per-session LLM run tracking (request vs last response) so the
        # watchdog can tell "a run is in flight" without any host-side busy
        # API (the core SessionManager exposes none).
        self._llm_req: dict[str, float] = {}
        self._llm_resp: dict[str, float] = {}
        # F4: sid -> match_id of the grant whose play tools were offered on
        # the last request for that session (only that grant can be nudged).
        self._tools_grant: dict[str, str] = {}

    # ------------------------------------------------------------------
    #  Queries
    # ------------------------------------------------------------------
    def grant_of(self, match_id: str) -> TurnGrant | None:
        return self._grants.get(str(match_id or ""))

    def tools_for(self, sid: str) -> list[Any]:
        """Request-scoped tools for the active grant announced to ``sid``.

        Returns ``[]`` when this session holds no live grant, which is also how
        a grant is revoked: the tools are simply not added to the next request.
        When several grants share a session only the first one's tools are
        offered — the mapping is remembered (``_tools_grant``) so F1 corrective
        nudges never blame a grant whose tool the model cannot even call.
        """
        target = str(sid or "").strip()
        if not target or self._stopping:
            return []
        for grant in list(self._grants.values()):
            if grant.state != ST_ACTIVE:
                continue
            if str(grant.session_id or "") != target:
                continue
            self._tools_grant[target] = grant.match_id
            if len(self._tools_grant) > 64:  # hygiene, same fail-open idea
                self._tools_grant.pop(next(iter(self._tools_grant)), None)
            return build_turn_tools(
                self._plugin, grant, match_kind=self._match_kind(grant.match_id)
            )
        return []

    def active_grant_for(self, sid: str) -> "TurnGrant | None":
        """The live grant announced to ``sid`` (context-block viewer authority).

        Read-only helper for ``context.build_game_state_context``: the granted
        match gets the full payload and its seat decides the block's viewer.
        ``None`` when this session holds no live grant.
        """
        target = str(sid or "").strip()
        if not target or self._stopping:
            return None
        for grant in list(self._grants.values()):
            if grant.state == ST_ACTIVE and str(grant.session_id or "") == target:
                return grant
        return None

    def owns(self, match: Any) -> bool:
        """Whether talk/judge must yield this match to the turn director."""
        grant = self._grants.get(str(getattr(match, "id", "") or ""))
        if grant is not None and grant.state == ST_ACTIVE:
            return True
        return modes.is_directed(match) or modes.is_delegated(match)

    def _is_current(self, grant: TurnGrant) -> bool:
        return (
            self._grants.get(grant.match_id) is grant
            and grant.state == ST_ACTIVE
        )

    def _match_kind(self, match_id: str) -> str:
        rooms = _rooms_of(self._plugin)
        getter = getattr(rooms, "get_room", None)
        room = getter(match_id) if callable(getter) else None
        match = getattr(room, "match", None)
        return str(getattr(match, "kind", "") or "")

    def prune_grants(
        self, *, now: float | None = None, retention_s: float = _GRANT_RETENTION_S
    ) -> list[str]:
        """Drop resolved grants older than ``retention_s`` (active never go)."""
        current = time.monotonic() if now is None else float(now)
        removed: list[str] = []
        for match_id, grant in list(self._grants.items()):
            if grant.state == ST_ACTIVE:
                continue
            resolved = grant.resolved_at or grant.created
            if current - resolved >= max(0.0, float(retention_s)):
                self._grants.pop(match_id, None)
                removed.append(match_id)
        return removed

    # ------------------------------------------------------------------
    #  Entry point (room event hook; sync, never blocks the room lock)
    # ------------------------------------------------------------------
    def on_event(self, room: Any, event: dict[str, Any]) -> None:
        if self._stopping or getattr(self._plugin, "stopping", False):
            return
        event_type = str(event.get("type") or "")
        if event_type not in ("start", "move", "over", "resign"):
            return
        match_id = str(getattr(getattr(room, "match", None), "id", "") or "?")
        task = asyncio.create_task(
            self._handle_event(room, event),
            name=f"gamehub-turn-event-{match_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def note_progress(self, sid: str) -> None:
        """Record host-side activity for a session holding a live grant.

        Called from the plugin's ``@on.step_result`` / ``@on.tool_result`` hooks:
        the host is demonstrably working on the notice turn, so the watchdog
        must not treat the turn as stalled. Cheap, synchronous, never raises.
        """
        target = str(sid or "").strip()
        if not target or self._stopping:
            return
        now = time.monotonic()
        for grant in list(self._grants.values()):
            if grant.state != ST_ACTIVE or str(grant.session_id or "") != target:
                continue
            timeout_s = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
            candidate = now + max(0.5, timeout_s)
            if grant.short_deadline > 0:
                # F1 short-window mode: progress may still re-arm the watchdog
                # (steps keep arriving), but never past the corrective nudge's
                # shortened deadline — otherwise a text-only reply would
                # re-earn the full silence window and the fast fill is lost.
                candidate = min(candidate, grant.short_deadline)
            grant.deadline = max(grant.deadline, candidate)
            grant.last_progress = now

    def note_llm_request(self, sid: str) -> None:
        """Mark an LLM run as started for ``sid`` (F3 in-flight signal).

        Paired with :meth:`note_response`; between the two, the watchdog
        treats the session as busy (the model has not even answered yet, so
        "silence" proves nothing about the turn).
        """
        target = str(sid or "").strip()
        if not target:
            return
        self._llm_req[target] = time.monotonic()
        self._prune_llm_marks()

    def note_tool_progress(self, sid: str) -> None:
        """Record real tool activity (``@on.tool_result``) for ``sid``.

        Distinguished from text-delivery progress: a tool result between two
        model steps proves the model is mid multi-step turn, so the
        corrective-nudge arming from the previous response must stand down
        (see :meth:`_text_nudge`).
        """
        target = str(sid or "").strip()
        if not target or self._stopping:
            return
        now = time.monotonic()
        for grant in list(self._grants.values()):
            if grant.state != ST_ACTIVE or str(grant.session_id or "") != target:
                continue
            grant.last_tool_progress = now

    def note_response(self, sid: str) -> None:
        """A full LLM response completed for ``sid`` (F1 entry point).

        First the ordinary progress bookkeeping (the host demonstrably
        replied, so the watchdog must not fire mid-flight), then — when the
        turn grant is still unresolved a short grace later — a corrective
        nudge with a shortened deadline. The grace window lets a response
        that *contains* the ``play_<match>`` call submit first (tool results
        land after the response hook), which cancels the nudge naturally.

        Arming is conservative (a false "you only talked" nudge wastes the
        budget and can preempt a slow-but-correct model):

        - the completed run must have *started after* the action notice was
          published — otherwise the response belongs to some earlier message
          (the incident's opening-line run) and the model has not even seen
          the notice yet;
        - when several grants share the session, only the grant whose
          ``play`` tool was actually offered on the last request can be
          nudged — the others cannot submit at all (``tools_for`` exposes one
          grant's tools per request).
        """
        target = str(sid or "").strip()
        if not target or self._stopping:
            return
        now = time.monotonic()
        self._llm_resp[target] = now
        self._prune_llm_marks()
        self.note_progress(target)
        cap = int(_cfg(self._plugin, "turn_text_retry_max", 2))
        run_started_after_notice = self._llm_req.get(target, 0.0)
        for grant in list(self._grants.values()):
            if grant.state != ST_ACTIVE or str(grant.session_id or "") != target:
                continue
            if not grant.notice_sent or grant.text_nudges >= max(0, cap):
                continue
            if run_started_after_notice <= grant.notice_sent_at:
                continue  # the response predates the notice's own run
            if self._tools_grant.get(target, grant.match_id) != grant.match_id:
                continue  # this grant's play tool was not offered
            if grant.text_task is not None and not grant.text_task.done():
                continue  # one corrective in flight is enough
            grant.text_task = asyncio.create_task(
                self._text_nudge(grant),
                name=f"gamehub-turn-textnudge-{grant.match_id}",
            )
            self._tasks.add(grant.text_task)
            grant.text_task.add_done_callback(self._tasks.discard)

    def _prune_llm_marks(self, *, keep: int = 64) -> None:
        """Bound the request/response maps (long-lived process hygiene).

        Evicting a live mark fails *open* (the run reads as idle, so the
        watchdog fills instead of wedging) — the safe direction. ``keep``
        comfortably exceeds the realistic concurrent-session count.
        """
        if len(self._llm_req) <= keep and len(self._llm_resp) <= keep:
            return
        for marks in (self._llm_req, self._llm_resp):
            if len(marks) <= keep:
                continue
            for sid in sorted(marks, key=marks.get)[: len(marks) - keep]:
                marks.pop(sid, None)

    async def _text_nudge(self, grant: TurnGrant) -> None:
        """Corrective nudge after a text-only reply (F1); shortens the window.

        Aborts (without consuming the budget) when the turn resolved during
        the grace period, the grant went stale, real tool work started (the
        model is mid multi-step turn, not "only talking"), or a newer
        turn-level run opened (e.g. the model already answering the notice).
        On send it cuts ``grant.deadline`` down to ``turn_text_retry_s`` so a
        still-silent model is filled quickly instead of waiting out the full
        turn timeout; an open run can still extend afterwards, but never
        past ``short_ceiling`` (corrective time + full silence window).
        """
        spawned_at = time.monotonic()
        sid = str(grant.session_id or "")
        grace = max(0.0, float(_cfg(self._plugin, "turn_text_grace_s", 4.0)))
        if grace:
            await asyncio.sleep(grace)
        if self._stopping or not self._is_current(grant):
            return
        if grant.last_tool_progress > spawned_at:
            return  # real tool work since the reply: a multi-step turn
        if self._llm_req.get(sid, 0.0) > spawned_at:
            return  # a newer run opened; its own response re-arms if needed
        rooms = _rooms_of(self._plugin)
        getter = getattr(rooms, "get_room", None)
        room = getter(grant.match_id) if callable(getter) else None
        match = getattr(room, "match", None)
        if match is None:
            return
        try:
            text = build_nudge_notice(
                match,
                seat=grant.seat,
                tool_name=play_tool_name(grant.match_id),
                plugin=self._plugin,
                text_only=True,
            )
        except Exception:  # noqa: BLE001 - a failed nudge must not block the fill
            logger.warning(
                "turn director: corrective nudge build failed (%s seat %s)",
                grant.match_id, grant.seat, exc_info=True,
            )
            return
        if not await send_notice(self._plugin, grant.session_id, text):
            return
        if not self._is_current(grant):  # resolved while the notice was in flight
            return
        grant.text_nudges += 1
        # NOT setting last_progress here on purpose: a fresh last_progress
        # would make _turn_in_flight true for a full timeout window and the
        # watchdog would extend right past the shortened deadline. The nudge
        # itself triggers a new run, which registers progress on its own
        # (note_llm_request / note_response) if the model actually answers.
        # ``nudged`` is consumed too: this IS the grant's one reminder, so the
        # shortened deadline must lead straight to the fill, not to a second
        # generic reminder that re-arms the full window.
        grant.nudged = True
        now = time.monotonic()
        retry_s = max(0.5, float(_cfg(self._plugin, "turn_text_retry_s", 25.0)))
        timeout_s = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
        grant.short_deadline = now + retry_s
        grant.short_ceiling = now + max(retry_s, timeout_s)
        grant.deadline = min(grant.deadline, grant.short_deadline)
        logger.info(
            "turn director: text-only reply, corrective nudge sent (%s seat %s, %s/%s)",
            grant.match_id,
            grant.seat,
            grant.text_nudges,
            int(_cfg(self._plugin, "turn_text_retry_max", 2)),
        )

    async def _handle_event(self, room: Any, event: dict[str, Any]) -> None:
        match = event.get("match") or getattr(room, "match", None)
        if match is None:
            return
        match_id = str(getattr(match, "id", "") or "")
        event_type = str(event.get("type") or "")
        self.prune_grants()
        try:
            status = status_of(match)
        except Exception:  # noqa: BLE001 - unknown/broken match: nothing to own
            logger.debug("turn director: status unavailable (%s)", match_id, exc_info=True)
            return
        options = modes.options_of(match)
        grant = self._grants.get(match_id)
        # A finish closes the grant first (its tools disappear with it), then
        # the result notice goes out — never a notice next to a live grant.
        if (
            grant is not None
            and grant.state == ST_ACTIVE
            and (event_type in ("over", "resign") or status.outcome is not None)
        ):
            self._finish(grant, ST_CANCELLED, "match_over")
            self._grants.pop(match_id, None)
            grant = None
        await self._stage_notice(room, match, event_type, status, options)

        if grant is not None and grant.state == ST_ACTIVE:
            # In-flight grant: no stacked duplicates.
            return

        seat, kind = self._pending_seat(match, options)
        if seat is None:
            return
        if grant is not None:
            self._grants.pop(match_id, None)
        await self.grant_turn(room, seat, kind, reason=f"event:{event_type}")

    async def _stage_notice(
        self,
        room: Any,
        match: Any,
        event_type: str,
        status: Any,
        options: dict[str, Any],
    ) -> None:
        """One tool-free notice per (match, start|over) for directed matches."""
        if options.get("driver") != modes.DRIVER_TURN:
            return
        if event_type == "start":
            stage = "start"
        elif event_type in ("over", "resign") or status.outcome is not None:
            stage = "over"
        else:
            return
        match_id = str(getattr(match, "id", "") or "")
        key = (match_id, stage)
        if key in self._stage_seen or key in self._stage_inflight:
            return
        self._stage_inflight.add(key)
        try:
            try:
                text = build_stage_notice(match, stage, plugin=self._plugin)
            except Exception:  # noqa: BLE001 - losing one notice is acceptable
                logger.warning(
                    "turn director: stage notice build failed (%s)", match_id,
                    exc_info=True,
                )
                return
            session_id = await self._resolve_session(room, match_id)
            if not session_id:
                logger.info("turn director: no target for %s notice (%s)", stage, match_id)
                return
            if not await send_notice(self._plugin, session_id, text):
                return
            self._stage_seen[key] = None
            while len(self._stage_seen) > _STAGE_SEEN_LIMIT:
                self._stage_seen.pop(next(iter(self._stage_seen)))
        finally:
            self._stage_inflight.discard(key)

    def _pending_seat(
        self, match: Any, options: dict[str, Any]
    ) -> tuple[int | None, str]:
        """Which seat owes a move now, and which grant kind it needs."""
        try:
            status = status_of(match)
        except Exception:  # noqa: BLE001 - broken view: no pending seat
            return None, ""
        if status.outcome is not None:
            return None, ""
        waiting = set(status.waiting_on or [])
        if options.get("driver") == modes.DRIVER_TURN:
            seat = modes.nori_seat_index(match)
            if seat is not None and seat in waiting:
                return seat, NORI_TURN
            return None, ""
        if (
            options.get("mode") == modes.MODE_BOT
            and options.get("role") == modes.ROLE_TEAMMATE
            and options.get("delegation")
        ):
            seat = modes.human_seat_index(match)
            if seat is not None and seat in waiting:
                return seat, DELEGATED
        return None, ""

    # ------------------------------------------------------------------
    #  Notice target
    # ------------------------------------------------------------------
    async def _resolve_session(self, room: Any, match_id: str = "") -> str:
        """Resolve the notice target; "" when there is no usable session.

        ``plugin.notice_target(room) -> (session_id, is_page) | None`` is the
        runtime's contract; ``is_page`` only decides page vs chat delivery on
        the host side and is not needed here.
        """
        resolver = _notice_target_of(self._plugin)
        if not callable(resolver):
            logger.warning("turn director: plugin.notice_target missing (%s)", match_id)
            return ""
        try:
            target = resolver(room)
            if asyncio.iscoroutine(target) or asyncio.isfuture(target):
                target = await target
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - unresolvable target = no notice
            logger.warning(
                "turn director: notice target resolution failed (%s)", match_id,
                exc_info=True,
            )
            return ""
        if not target:
            return ""
        try:
            session_id, _is_page = target
        except (TypeError, ValueError):
            return ""
        return str(session_id or "").strip()

    # ------------------------------------------------------------------
    #  Grant lifecycle
    # ------------------------------------------------------------------
    async def grant_turn(
        self,
        room: Any,
        seat: int,
        kind: str,
        *,
        reason: str = "",
    ) -> TurnGrant | None:
        """Mint one grant and announce it (no target → watchdog only)."""
        if self._stopping or getattr(self._plugin, "stopping", False):
            return None
        match_id = str(getattr(getattr(room, "match", None), "id", "") or "")
        if not match_id:
            return None
        existing = self._grants.get(match_id)
        if match_id in self._reservations or (
            existing is not None and existing.state == ST_ACTIVE
        ):
            return None
        # The reservation is synchronous (atomic on the event loop): every
        # await below is guarded against a second mint.
        self._reservations.add(match_id)
        try:
            if existing is not None:
                self._grants.pop(match_id, None)
            snap = await room.turn_snapshot(seat)
            if not snap.get("active"):
                return None
            if match_id in self._grants:  # defensive re-check after the await
                return None
            timeout_s = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
            grant = TurnGrant(
                match_id=match_id,
                seat=int(seat),
                key=secrets.token_hex(8),
                kind=kind,
                move_count=int(snap.get("move_count", 0) or 0),
                reason=reason,
                deadline=time.monotonic() + max(0.5, timeout_s),
            )
            grant.session_id = await self._resolve_session(room, match_id)
            self._grants[match_id] = grant
            grant.watchdog = asyncio.create_task(
                self._watchdog(grant, room),
                name=f"gamehub-turn-watchdog-{match_id}",
            )
            self._tasks.add(grant.watchdog)
            grant.watchdog.add_done_callback(self._tasks.discard)
            grant.turn_task = asyncio.create_task(
                self._run_turn(grant, room),
                name=f"gamehub-turn-notice-{match_id}",
            )
            self._tasks.add(grant.turn_task)
            grant.turn_task.add_done_callback(self._tasks.discard)
            return grant
        finally:
            self._reservations.discard(match_id)

    async def _run_turn(self, grant: TurnGrant, room: Any) -> None:
        """Build and publish the mandatory action notice (no gate, no cooldown).

        The notice is header-only: rules / board / move syntax ride the
        per-request ``gamehub_context`` block
        (``context.build_game_state_context``), so persisted history never
        accumulates stale boards or private cards. A failed send leaves the
        grant to the watchdog.
        """
        match = getattr(room, "match", None)
        try:
            text = build_turn_notice(
                match,
                seat=grant.seat,
                kind=grant.kind,
                move_count=grant.move_count,
                tool_name=play_tool_name(grant.match_id),
                plugin=self._plugin,
            )
        except Exception:  # noqa: BLE001 - no text → the watchdog still fills
            logger.warning(
                "turn director: action notice build failed (%s seat %s)",
                grant.match_id, grant.seat, exc_info=True,
            )
            return
        if not self._is_current(grant):
            return
        if not grant.session_id:
            logger.warning(
                "turn director: no target for %s; the watchdog will fill",
                grant.match_id,
            )
            return
        grant.notice_sent = True
        grant.notice_session_id = grant.session_id
        if not await send_notice(self._plugin, grant.session_id, text):
            # The grant stays: the watchdog is the documented fallback.
            logger.warning(
                "turn director: action notice delivery failed (%s → %s)",
                grant.match_id, grant.session_id,
            )
            return
        grant.notice_sent_at = time.monotonic()

    # ------------------------------------------------------------------
    #  play submission (the only backend of the request-scoped play tool)
    # ------------------------------------------------------------------
    async def submit_play(
        self,
        match_id: str,
        grant_key: str,
        move: str,
        say: str | None = None,
        *,
        persist_say: bool = False,
    ) -> PlayResult:
        """Submit one move inside the grant (typed result, legacy unpackable).

        - legal move → ``ok=True, terminal=True, status="played"``;
        - illegal / empty → ``ok=False, terminal=False, status="invalid"`` (the
          model may correct itself; the watchdog is still armed);
        - missing/finished grant, or a position that moved on (a late or
          manually-played move won the race) → ``terminal=True``, no move.
        """
        grant = self._grants.get(str(match_id or ""))
        if grant is None or grant.key != str(grant_key or ""):
            return PlayResult(
                ok=False,
                terminal=True,
                status="no_grant",
                text="本回合没有有效的落子授权（可能已被停止或已结束）。",
            )
        if not self._is_current(grant):
            return PlayResult(
                ok=False,
                terminal=True,
                status="no_grant",
                text="这一手的授权已经结束，不能再落子。",
            )
        text = str(move or "").strip()
        if not text:
            return PlayResult(
                ok=False,
                terminal=False,
                status="invalid",
                text="缺少 move 参数；请按本局 move_help 给出动作字符串。",
            )
        rooms = _rooms_of(self._plugin)
        loader = getattr(rooms, "get_or_load", None)
        room = await loader(grant.match_id) if callable(loader) else None
        if room is None:
            self._finish(grant, ST_CANCELLED, "no_room")
            return PlayResult(
                ok=False,
                terminal=True,
                status="no_room",
                text="对局已不存在，本轮结束。",
            )
        if not self._is_current(grant):
            return PlayResult(
                ok=False,
                terminal=True,
                status="no_grant",
                text="这一手的授权已经结束，不能再落子。",
            )
        snap = await room.turn_snapshot(grant.seat)
        if not self._is_current(grant):
            return PlayResult(
                ok=False,
                terminal=True,
                status="no_grant",
                text="这一手的授权已经结束，不能再落子。",
            )
        if not snap.get("active") or int(snap.get("move_count", -1)) != grant.move_count:
            self._finish(grant, ST_STALE, "stale_snapshot")
            return PlayResult(
                ok=False,
                terminal=True,
                status="stale",
                text="局面已经变化（等待期间已有人落子），本手作废。",
            )
        match = snap.get("match")
        seat_kind = self._seat_kind(match, grant.seat)
        if seat_kind == "":
            self._finish(grant, ST_CANCELLED, "bad_seat")
            return PlayResult(
                ok=False,
                terminal=True,
                status="gone",
                text="没有这个座位。本轮结束。",
                error="没有这个座位",
            )
        say_text = str(say or "").strip()
        if seat_kind == "human":
            apply_move = getattr(room, "apply_delegated_move", None)
            if not callable(apply_move):
                self._finish(grant, ST_CANCELLED, "no_delegated_path")
                return PlayResult(
                    ok=False,
                    terminal=True,
                    status="gone",
                    text="当前对局不能代下，本轮结束。",
                )
            applied, error = await apply_move(
                grant.seat,
                text,
                expected_move_count=grant.move_count,
                grant_key=grant.key,
                say=say_text or None,
                persist_say=persist_say,
            )
        else:
            apply_move = getattr(room, "apply_ai_move", None)
            if not callable(apply_move):
                self._finish(grant, ST_CANCELLED, "no_ai_path")
                return PlayResult(
                    ok=False,
                    terminal=True,
                    status="gone",
                    text="当前对局不能落子，本轮结束。",
                )
            applied, error = await apply_move(
                grant.seat,
                text,
                expected_move_count=grant.move_count,
                say=say_text or None,
                persist_say=persist_say,
            )
        if applied is None:
            if grant.state != ST_ACTIVE:
                return PlayResult(
                    ok=False,
                    terminal=True,
                    status="no_grant",
                    text="这一手的授权已经结束，不能再落子。",
                )
            # The seat name comes from the match host; keep the shape-based
            # check rather than hard-coding a persona name.
            gone_error = error in (
                "对局已经结束",
                "没有这个座位",
                "这不是可以代下的人类座位",
            ) or (error.startswith("这不是") and error.endswith("的座位"))
            if gone_error:
                self._finish(grant, ST_CANCELLED, "gone")
                return PlayResult(
                    ok=False,
                    terminal=True,
                    status="gone",
                    text=f"落子失败：{error}。本轮结束。",
                    error=error,
                )
            if "局面已经变化" in error or "还没轮到" in error or "过期" in error:
                self._finish(grant, ST_STALE, "stale_apply")
                return PlayResult(
                    ok=False,
                    terminal=True,
                    status="stale",
                    text=f"落子失败：{error}。本轮结束。",
                    error=error,
                )
            return PlayResult(
                ok=False,
                terminal=False,
                status="invalid",
                text=f"落子 {text} 失败：{error}。不要重复同样的非法落子。",
                error=error,
            )
        log = getattr(applied, "log", None) or ()
        applied_move = str(getattr(log[-1], "move", "") or text) if log else text
        self._finish(grant, ST_PLAYED, "played")
        return PlayResult(
            ok=True,
            terminal=True,
            status="played",
            text=f"已落子 {applied_move}。本回合完成。",
            move=applied_move,
            say=say_text,
        )

    @staticmethod
    def _seat_kind(match: Any, seat: int) -> str:
        seats = list(getattr(match, "seats", None) or ())
        if not isinstance(seat, int) or not 0 <= seat < len(seats):
            return ""
        return str(getattr(seats[seat], "kind", "") or "")

    # ------------------------------------------------------------------
    #  Watchdog (rule-bot fallback)
    # ------------------------------------------------------------------
    async def _nudge(self, grant: TurnGrant, match: Any) -> bool:
        """Send one "you have not acted yet" reminder; ``False`` if impossible."""
        sid = str(getattr(grant, "session_id", "") or "")
        if not sid or not grant.notice_sent:
            return False
        if not self._is_current(grant):
            return False
        try:
            text = build_nudge_notice(
                match,
                seat=grant.seat,
                tool_name=play_tool_name(grant.match_id),
                plugin=self._plugin,
            )
        except Exception:  # noqa: BLE001 - a failed reminder must not block the fill
            logger.warning(
                "turn director: nudge build failed (%s seat %s)",
                grant.match_id, grant.seat, exc_info=True,
            )
            return False
        return await send_notice(self._plugin, sid, text)

    async def _recover_without_bot(self, room: Any, grant: TurnGrant) -> None:
        """Manual recovery when the game has no rule bot to fill with.

        Delegated seats get their delegation switched off (back to manual
        play); a nori turn just closes (the next human move re-enters the
        normal flow).
        """
        if grant.kind != DELEGATED:
            return
        disable = getattr(room, "disable_delegation", None)
        if not callable(disable):
            return
        try:
            changed = await disable(reason="no_bot_fallback")
        except Exception:  # noqa: BLE001 - the grant is already closed
            logger.warning(
                "turn director: disabling delegation failed (%s)",
                grant.match_id, exc_info=True,
            )
            return
        if changed:
            logger.warning(
                "turn director: no rule bot — delegation disabled (%s seat %s)",
                grant.match_id, grant.seat,
            )

    async def _watchdog(self, grant: TurnGrant, room: Any) -> None:
        """Wait for the turn to resolve; fill with the rule bot only if it stalls.

        The deadline is a **silence timeout**, not a hard cap: a turn that is
        visibly progressing (the host is still streaming, tools are running)
        is not a stalled turn and must never be overwritten by a bot move.
        Iterative (not recursive) so a long match cannot grow the stack.
        """
        try:
            while True:
                await self._wait_deadline(grant)
                if not self._is_current(grant):
                    return
                if self._stopping or getattr(self._plugin, "stopping", False):
                    return
                in_flight = await self._turn_in_flight(grant)
                if in_flight:
                    # Progress observed: extend the silence window and keep
                    # waiting. In F1 short-window mode the extension is capped
                    # at the corrective nudge's ``short_deadline``; past it,
                    # only an LLM run that is still open buys more time — in
                    # ``retry_s`` steps up to ``short_ceiling`` — because the
                    # model may genuinely be mid-answer to the corrective
                    # (the incident's failover tier took 100s for 25 tokens).
                    # Real silence (no open run) fills right at short_deadline.
                    now = time.monotonic()
                    window = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
                    candidate = now + max(0.5, window)
                    exhausted = False
                    if grant.short_deadline > 0:
                        candidate = min(candidate, grant.short_deadline)
                        if candidate <= now + 0.05:
                            # Past the short deadline: only a still-open run
                            # (the strong signal — see _turn_in_flight) can
                            # buy bounded extra time for a slow answer.
                            retry_s = max(
                                0.5,
                                float(_cfg(self._plugin, "turn_text_retry_s", 25.0)),
                            )
                            candidate = min(now + retry_s, grant.short_ceiling)
                            exhausted = candidate <= now + 0.05
                    if not exhausted and candidate > now + 0.05:
                        grant.deadline = max(grant.deadline, candidate)
                        logger.info(
                            "turn director: turn still in flight, extending "
                            "(%s seat %s)",
                            grant.match_id, grant.seat,
                        )
                        continue
                    logger.info(
                        "turn director: short window exhausted despite activity "
                        "(%s seat %s)",
                        grant.match_id, grant.seat,
                    )
                snap = await room.turn_snapshot(grant.seat)
                if not self._is_current(grant):
                    return
                if not snap.get("active") or int(snap.get("move_count", -1)) != grant.move_count:
                    self._finish(grant, ST_STALE, "watchdog_stale")
                    return
                match = snap.get("match")
                module = _module_of(str(getattr(match, "kind", "") or ""))
                if module is None:
                    self._finish(grant, ST_CANCELLED, "no_module")
                    return
                # The host went quiet without acting: ask once, explicitly,
                # before the rule bot takes the move. Bounded to one reminder
                # per grant (the F1 corrective consumes it too).
                if not grant.nudged and await self._nudge(grant, match):
                    grant.nudged = True
                    window = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
                    grant.deadline = time.monotonic() + window
                    logger.info(
                        "turn director: turn stalled without a move, reminded "
                        "(%s seat %s)",
                        grant.match_id, grant.seat,
                    )
                    continue
                if type(module).bot is GameModule.bot:
                    self._finish(grant, ST_CANCELLED, "no_bot_module")
                    await self._recover_without_bot(room, grant)
                    return
                move = module.bot(getattr(match, "state", None), grant.seat)
                if not move:
                    self._finish(grant, ST_CANCELLED, "no_bot_move")
                    await self._recover_without_bot(room, grant)
                    return
                applied = await self._fill(room, grant, match, str(move))
                if applied is None:
                    self._finish(grant, ST_STALE, "watchdog_lost_race")
                    return
                self._finish(grant, ST_FILLED, "watchdog_fallback")
                logger.info(
                    "turn director: watchdog filled the turn (%s seat %s ← %s)",
                    grant.match_id, grant.seat, move,
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed fill must not stall the match
            logger.warning(
                "turn director: watchdog failed (%s seat %s)",
                grant.match_id, grant.seat, exc_info=True,
            )
            with contextlib.suppress(Exception):
                self._finish(grant, ST_CANCELLED, "watchdog_error")

    async def _turn_in_flight(self, grant: TurnGrant) -> bool:
        """True while the host is visibly still working on this turn.

        Signals, cheapest first:

        - ``grant.last_progress``: a host step/tool result was observed within
          the silence window (``note_progress`` from the plugin's hooks);
        - an LLM run for the grant's session started but has not answered
          yet (``note_llm_request`` newer than ``note_response``). This is the
          plugin's own in-flight signal (F3): the core SessionManager exposes
          no ``is_busy`` capability, so an earlier revision probed one that
          never existed and silently always returned "idle". A run mark older
          than ``_LLM_RUN_TTL_S`` (failed run that never answered) is
          ignored — otherwise one dead run would wedge the turn forever.

        Past the F1 short deadline (``short_deadline``), only the open-run
        signal counts: text deliveries keep ticking ``last_progress``, but a
        reply that keeps not submitting must not keep re-arming the watchdog.
        The watchdog turns that open-run signal into bounded extensions up to
        ``short_ceiling`` — past the ceiling the fill proceeds even mid-run
        (a late correct submission is rejected as stale by the room layer,
        which is the documented race-safe outcome).

        A turn that is visibly progressing must never be overwritten by a bot
        move; only real silence (no step, no tool, no open run) counts.
        """
        window = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
        sid = str(getattr(grant, "session_id", "") or "")
        requested = self._llm_req.get(sid)
        answered = self._llm_resp.get(sid)
        open_run = (
            requested is not None
            and (answered is None or requested > answered)
            and time.monotonic() - requested < _LLM_RUN_TTL_S
        )
        if open_run:
            return True
        last = float(getattr(grant, "last_progress", 0.0) or 0.0)
        if not last:
            return False
        if grant.short_deadline > 0 and time.monotonic() >= grant.short_deadline:
            return False  # weak signals no longer block the fill
        return time.monotonic() - last < window

    async def _wait_deadline(self, grant: TurnGrant) -> None:
        """Sleep until the turn resolves or the silence window elapses.

        ``grant.deadline`` is a *silence* deadline: ``note_progress`` pushes it
        forward whenever the host does real work on the turn, and the watchdog
        loop re-arms itself instead of filling a live turn.
        """
        while True:
            if self._stopping or getattr(self._plugin, "stopping", False):
                return
            if grant.state != ST_ACTIVE:
                return
            remaining = grant.deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 5.0))
        grace = float(_cfg(self._plugin, "turn_fallback_delay_s", 1.0))
        if grace <= 0:
            return
        end = time.monotonic() + grace
        while True:
            if self._stopping or getattr(self._plugin, "stopping", False):
                return
            if grant.state != ST_ACTIVE:
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 5.0))

    async def _fill(
        self, room: Any, grant: TurnGrant, match: Any, move: str
    ) -> Any:
        """Apply one rule-bot move for the grant seat (``None`` when it lost)."""
        seat_kind = self._seat_kind(match, grant.seat)
        if seat_kind == "human":
            apply_move = getattr(room, "apply_delegated_move", None)
            if callable(apply_move):
                applied, _error = await apply_move(
                    grant.seat,
                    move,
                    expected_move_count=grant.move_count,
                    grant_key=grant.key,
                )
                return applied
        else:
            apply_move = getattr(room, "apply_ai_move", None)
            if callable(apply_move):
                applied, _error = await apply_move(
                    grant.seat, move, expected_move_count=grant.move_count
                )
                return applied
        # Last resort: the room's own rule-bot step (re-validates under lock).
        step = getattr(room, "ai_step", None)
        if callable(step):
            return await step(grant.seat)
        return None

    # ------------------------------------------------------------------
    #  Close / restore / stop
    # ------------------------------------------------------------------
    def _finish(self, grant: TurnGrant, state: str, reason: str) -> None:
        if grant.state in _RESOLVED:
            return
        grant.state = state
        grant.resolved_at = time.monotonic()
        self._log_grant(grant, reason)
        for task in (grant.watchdog, grant.text_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    def _log_grant(self, grant: TurnGrant, reason: str) -> None:
        logger.debug(
            "turn grant %s (%s seat %s mc=%s) -> %s (%s)",
            grant.key,
            grant.match_id,
            grant.seat,
            grant.move_count,
            grant.state,
            reason,
        )

    async def restore_active(self) -> int:
        """Re-arm turns for restored matches whose turn seat is waiting."""
        rooms = _rooms_of(self._plugin)
        lister = getattr(rooms, "active_rooms", None)
        if not callable(lister):
            return 0
        self.prune_grants()
        count = 0
        for room in list(lister() or []):
            try:
                count += bool(await self.refresh_turn(room, reason="restore"))
            except Exception:  # noqa: BLE001 - one match must not block others
                logger.warning("turn director: restore failed", exc_info=True)
        return count

    async def refresh_turn(self, room: Any, *, reason: str = "refresh") -> bool:
        """Mint a turn if the pending seat owes one (delegation/mode switches)."""
        if self._stopping or getattr(self._plugin, "stopping", False):
            return False
        match = getattr(room, "match", None)
        if match is None:
            return False
        options = modes.options_of(match)
        if options.get("driver") != modes.DRIVER_TURN and not (
            options.get("mode") == modes.MODE_BOT
            and options.get("role") == modes.ROLE_TEAMMATE
            and options.get("delegation")
        ):
            return False
        seat, kind = self._pending_seat(match, options)
        if seat is None:
            return False
        grant = await self.grant_turn(room, seat, kind, reason=reason)
        return grant is not None

    def cancel_match(self, match_id: str, reason: str = "cancel") -> None:
        grant = self._grants.get(str(match_id or ""))
        if grant is None:
            return
        self._finish(grant, ST_CANCELLED, reason)

    async def stop(self) -> None:
        """Stop: cancel watchdogs/notices, close grants without playing."""
        self._stopping = True
        for grant in list(self._grants.values()):
            grant.state = ST_CANCELLED
            grant.resolved_at = time.monotonic()
        self._grants.clear()
        self._reservations.clear()
        tasks = [task for task in list(self._tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._stage_inflight.clear()


__all__ = [
    "DEFAULT_AGENT_NAME",
    "DELEGATED",
    "NORI_TURN",
    "PlayResult",
    "TurnDirector",
    "TurnGrant",
    "build_stage_notice",
    "build_turn_notice",
    "send_notice",
]
