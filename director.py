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
- **Identity** comes from ``plugin.settings.agent_name`` (default ``nori``);
  no host bot nickname is required.

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
import inspect
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
    from .tools_turn import build_turn_tools, play_tool_name
except ImportError:  # standalone tests import these modules as top-level
    import modes
    from engine import games
    from engine.match import status_of
    from engine.types import GameModule
    from tools import NOTICE_ACTOR
    from tools_turn import build_turn_tools, play_tool_name

try:  # production: plugin package
    from . import context as action_context
except ImportError:  # standalone tests import modules as top-level
    import context as action_context

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
    # Monotonic hard deadline (``turn_play_timeout_s``).
    deadline: float = 0.0
    # Last observed host-side progress (step/tool results); 0 = never.
    last_progress: float = 0.0
    reason: str = ""
    created: float = field(default_factory=time.monotonic)
    state: str = ST_ACTIVE
    resolved_at: float = 0.0
    notice_sent: bool = False
    notice_session_id: str = ""
    turn_task: asyncio.Task[Any] | None = None
    watchdog: asyncio.Task[Any] | None = None

    @property
    def expires_at(self) -> float:
        """nori spelling kept for readers that still use it."""
        return self.deadline


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


def _rival_seat(match: Any, seat: int) -> int | None:
    """Opponent seat of ``seat``: the first other seat, no count assumption."""
    seats = list(getattr(match, "seats", ()) or ())
    for index in range(len(seats)):
        if index != seat:
            return index
    return None


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
    threat_analysis: bool | None = None,
) -> str:
    """Action-turn notice: rules + full board + recent moves, never the plan.

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
            f"[System: 斗地主行动回合：{players}。{identity}。地主独战、农民同队；"
            f"叫分前阵营待定。请在本轮回复里调用 {tool_name} 提交本座位的动作"
            "（叫分用 bid 1 / bid 2 / bid 3 或 pass；出牌用牌面字符串或 pass），"
            "一次回复只调用一次；同一次回复里可以聊公开牌型、调侃与局势气氛，"
            "但禁止透露自己的未出手牌或私有策略。"
            "不得代替其他席位行动，不得沿用其他局的名字。注意你是玩家，不是解说。]\n\n"
            + action_context.build_action_notice_context(
                plugin, match, seat, threat_analysis=False
            )
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
    rival = _rival_seat(match, seat)
    rival_name = names[rival] if rival is not None and rival < len(names) else "对手"
    rival_label = _label(labels, rival, "对方颜色")
    rival_ref = f"座位 {rival} 的" if rival is not None else ""
    rival_line = (
        f"本局对手是{rival_ref}「{rival_name}」（执{rival_label}）；"
        "不要沿用其他对局或陪练的名字称呼对手。"
    )
    same_seat = mover == seat
    auto_pass = bool((getattr(match, "state", None) or {}).get("passed"))
    continuation = (
        "对手无合法落点，已自动过手，现在仍轮到你" if auto_pass else "现在仍轮到你"
    )
    identity_line = "注意你是棋手，不是解说。"
    call_line = (
        f"请在本轮回复里调用 {tool_name} 工具落子，一次回复只调用一次；"
        "同一次回复可以照常说一两句玩家口吻的短话，"
        "关键赛点/高光时刻/情绪激动时可搭配表情包、语音等非文本消息。"
    )

    if kind == DELEGATED:
        human_name = names[seat] if 0 <= seat < len(names) else "玩家"
        if same_seat:
            head = (
                f"[System: 对局通知 · {move_count} 手：你（{agent}，代 {human_name} "
                f"执{seat_label}）刚落子 {move or '一手'}；{continuation}，"
                "这一手仍由你代下。"
                f"{rival_line}"
                f"{call_line}"
                f"{identity_line}"
                "不要代替对手落子。]"
            )
        else:
            head = (
                f"[System: 对局通知 · {move_count} 手：对手 {mover_name} 刚落子 "
                f"{move or '一手'}。{human_name} 已把这一局的人类座位委托给你"
                f"（{agent}），这一手由你代下（执{seat_label}）。"
                f"{rival_line}"
                f"{call_line}"
                f"{identity_line}"
                "不要代替对手落子。]"
            )
    elif same_seat:
        head = (
            f"[System: 对局通知 · {move_count} 手：你（{agent}）刚落子 "
            f"{move or '一手'}（执{seat_label}）；{continuation}。"
            f"{rival_line}"
            f"这是你的对局回合：{call_line}"
            f"{identity_line}]"
        )
    else:
        head = (
            f"[System: 对局通知 · {move_count} 手：{mover_name} 刚落子 "
            f"{move or '一手'}，现在轮到你（{agent}，执{seat_label}）。"
            f"{rival_line}"
            f"这是你的对局回合：{call_line}"
            f"{identity_line}]"
        )

    body = action_context.build_action_notice_context(
        plugin, match, seat, threat_analysis=threat_analysis
    )
    lines = [head, ""]
    if body:
        lines.append(body)
    else:  # the helper is never empty, but never let a turn lose its header
        lines.append("未提供完整局面：落子前不要凭最近几手或聊天历史猜测盘面。")
    if status.outcome is not None:
        lines.append(f"对局已结束：{status.result_text or status.outcome.text}")
    return "\n".join(lines)


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

    # ------------------------------------------------------------------
    #  Queries
    # ------------------------------------------------------------------
    def grant_of(self, match_id: str) -> TurnGrant | None:
        return self._grants.get(str(match_id or ""))

    def tools_for(self, sid: str) -> list[Any]:
        """Request-scoped tools for the active grant announced to ``sid``.

        Returns ``[]`` when this session holds no live grant, which is also how
        a grant is revoked: the tools are simply not added to the next request.
        """
        target = str(sid or "").strip()
        if not target or self._stopping:
            return []
        for grant in list(self._grants.values()):
            if grant.state != ST_ACTIVE:
                continue
            if str(grant.session_id or "") != target:
                continue
            return build_turn_tools(
                self._plugin, grant, match_kind=self._match_kind(grant.match_id)
            )
        return []

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
            grant.deadline = max(grant.deadline, now + max(0.5, timeout_s))
            grant.last_progress = now

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
        """Build and publish the mandatory action notice (no gate, no cooldown)."""
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
        """
        try:
            await self._wait_deadline(grant)
            if not self._is_current(grant):
                return
            if self._stopping or getattr(self._plugin, "stopping", False):
                return
            if await self._turn_in_flight(grant):
                # Progress observed: extend the silence window and keep waiting.
                grant.deadline = time.monotonic() + float(
                    _cfg(self._plugin, "turn_play_timeout_s", 90.0)
                )
                logger.info(
                    "turn director: turn still in flight, extending (%s seat %s)",
                    grant.match_id, grant.seat,
                )
                return await self._watchdog(grant, room)
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
        - the host pipeline still reports the notice turn as running
          (``ctx.session_mgr.is_busy(sid)`` when the host exposes it).

        A turn that is visibly progressing must never be overwritten by a bot
        move; only real silence (no step, no tool, no busy flag) counts.
        """
        window = float(_cfg(self._plugin, "turn_play_timeout_s", 90.0))
        last = float(getattr(grant, "last_progress", 0.0) or 0.0)
        if last and time.monotonic() - last < window:
            return True
        sid = str(getattr(grant, "session_id", "") or "")
        if not sid:
            return False
        ctx = getattr(self._plugin, "ctx", None)
        if ctx is None:
            return False
        manager = getattr(ctx, "session_mgr", None)
        checker = getattr(manager, "is_busy", None)
        if callable(checker):
            try:
                busy = checker(sid)
                if inspect.isawaitable(busy):
                    busy = await busy
            except Exception:  # noqa: BLE001 - optional host capability
                busy = False
            if busy is True:
                return True
        return False

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
        watchdog = grant.watchdog
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()

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
