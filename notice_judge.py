"""Notice judge: decide "speak or stay quiet" from LOCAL key-event rules only.

Behaviour (the single source of truth for constants):

- **Notice/log only, never moves**: produces no candidates, never touches the
  board, never blocks the room lock;
- **Human-move triggered only** in opponent matches; AI replies are silenced by
  ``TableTalk`` before reaching here; teammate matches judge the opponent's
  reply (``modes.is_wanted_trigger``);
- **Fast channel**: the game module's optional ``key_events`` pure-rule hook
  (gomoku: win / block / open-four / four / double-three) yields
  ``speak=True`` with no network call, still subject to ``TableTalk``'s shared
  cooldown/dedup;
- **Plan expiry**: a move that crosses the plan TTL boundary is a priority
  trigger (opponent matches only);
- **No network**: System1 was removed; ``judge`` tier is purely local. No
  score/probability/gray-zone logic remains.
- **Event snapshot**: judge only on ``event["match"]`` / ``event["before_state"]``,
  never on the possibly-advanced ``room.match``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # production: plugin package (``plugins.<plugin>.notice_judge``)
    from . import modes
    from .engine import games
    from .engine.match import status_of
    from .plan import ai_seat_index, plan_remaining
except ImportError:  # standalone tests import modules as top-level
    import modes
    from engine import games
    from engine.match import status_of
    from plan import ai_seat_index, plan_remaining

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

_MAX_PENDING = 4


@dataclass(frozen=True)
class Judgment:
    """One notice judgment (decides whether to speak, never produces lines)."""

    speak: bool
    priority: bool = False
    reason: str = ""
    fast: bool = False
    fallback: str = ""  # "" = normal; "off" = judge tier disabled (TableTalk -> key)
    key_flags: tuple[str, ...] = ()

    @property
    def is_fallback(self) -> bool:
        return bool(self.fallback)


def _cfg(plugin: Any, name: str, default: Any) -> Any:
    value = getattr(plugin.settings, name, default)
    return default if value is None else value


def _label_of(match: Any, seat: int) -> str:
    try:
        labels = list(games.get(match.kind).seat_labels(match.state) or [])
    except Exception:
        labels = []
    return labels[seat] if 0 <= seat < len(labels) and labels[seat] else "棋手"


def _module_chat_context(match: Any, seat: int, names: list[str]) -> str:
    """Optional module snapshot (board/coords/immediate threats); "" on failure.

    Reuses the public ``chat_context`` hook instead of hard-coding any game.
    """
    try:
        module = games.get(match.kind)
    except Exception:
        return ""
    hook = getattr(module, "chat_context", None)
    if not callable(hook):
        return ""
    try:
        try:
            value = hook(match.state, seat, names)
        except TypeError:
            value = hook(match.state, seat)
    except Exception:
        logger.debug("notice judge snapshot failed, continuing without", exc_info=True)
        return ""
    return value.strip() if isinstance(value, str) else ""


def _module_key_events(match: Any, seat: int, event: dict[str, Any]) -> dict[str, Any]:
    """Optional module fast-channel signals; unknown game/failure -> {}.

    Aligned with ``GameModule.key_events(state, *, before, seat)``; the ``key``
    entry is the summary bit, other keys are module-defined. The judge only
    passes names through to logs, never interprets game-specific semantics.
    """
    try:
        module = games.get(match.kind)
    except Exception:
        return {}
    hook = getattr(module, "key_events", None)
    if not callable(hook):
        return {}
    try:
        flags = hook(
            match.state,
            before=event.get("before_state"),
            seat=seat,
        )
    except Exception:
        logger.debug("notice judge key_events failed", exc_info=True)
        return {}
    return dict(flags) if isinstance(flags, dict) else {}


def _opponent_seat(match: Any, seat: int) -> int:
    return next((i for i in range(len(match.seats)) if i != seat), 1 - seat)


def _state_text(
    match: Any,
    *,
    seat: int,
    event: dict[str, Any],
    last_talk_s: float | None,
) -> str:
    """Compact board text for logs/diagnostics (no tokens or full chat).

    Chat context only uses the last 3 entries of ``match.chat`` so "what I just
    said" does not fight this move; no cross-session history is read.
    """
    if getattr(match, "kind", "") == "doudizhu":
        # The mover is not an observer; the judge must not read the hand.
        return games.get(match.kind).describe(match.state, None, [s.name for s in match.seats])
    status = status_of(match)
    names = [s.name for s in match.seats]
    mover = names[seat] if 0 <= seat < len(names) else "对手"
    move = str(event.get("move") or "").strip() or "一手"
    teammate = modes.role_of(match) == modes.ROLE_TEAMMATE
    if teammate:
        # Teammate view: we stand with the human; "our side" = human seat.
        opponent = modes.human_seat_index(match)
        if opponent is None or opponent == seat:
            opponent = _opponent_seat(match, seat)
        rival_seat = seat
    else:
        opponent = _opponent_seat(match, seat)
        rival_seat = seat
    lines = [
        (
            f"{games.get(match.kind).name['zh']} · 第 {len(match.log)} 手 · "
            f"{mover} 刚落子 {move}。"
        ),
    ]
    if teammate:
        human_name = names[opponent] if 0 <= opponent < len(names) else "玩家"
        lines.append(
            f"你是 {human_name} 的队友（{human_name} 执"
            f"{_label_of(match, opponent)}），对手 {mover} 执"
            f"{_label_of(match, rival_seat)}；只帮腔出主意，不代为落子。"
        )
    else:
        lines.append(
            f"你执{_label_of(match, opponent)}，对手 {mover} 执{_label_of(match, seat)}。"
        )
    if status.outcome is not None:
        lines.append(f"对局已结束：{status.result_text or status.outcome.text}。")
    snapshot = _module_chat_context(match, seat, names)
    if snapshot:
        lines.append(snapshot)
    recent = match.log[-6:]
    if recent:
        shown = [
            f"{names[entry.seat] if entry.seat < len(names) else '棋手'} "
            f"{'认输' if entry.move == 'resign' else entry.move}"
            for entry in recent
        ]
        lines.append("最近棋：" + "、".join(shown))
    chat = getattr(match, "chat", ()) or ()
    if chat:
        recent_chat = chat[-3:]
        lines.append(
            "最近对局聊天："
            + " / ".join(
                f"{names[c.seat] if c.seat < len(names) else '棋手'}："
                f"{str(c.text or '')[:60]}"
                for c in recent_chat
            )
        )
    if teammate:
        lines.append("你是场边队友视角，不设定打法。")
    else:
        plan_seat = ai_seat_index(match)
        plan = match.seats[plan_seat].plan if plan_seat is not None else None
        plan_text = ""
        if isinstance(plan, dict):
            plan_text = str(plan.get("text") or "").strip()
        if plan_text:
            tempo = "turn"
            try:
                tempo = str(games.get(match.kind).tempo or "turn")
            except Exception:
                logger.debug("tempo read failed, treating as turn-based", exc_info=True)
            ttl = plan_remaining(
                plan, tempo=tempo, move_count=len(match.log), now=int(time.time() * 1000)
            )
            state = "生效中" if ttl.get("active") else "已过期"
            lines.append(f"你当前的打法（{state}）：{plan_text}")
        else:
            lines.append("你当前没有设定打法。")
    if last_talk_s is not None:
        lines.append(f"距你上次发言：{int(max(0.0, last_talk_s))} 秒。")
    return "\n".join(lines)


class NoticeJudge:
    """Single-worker, bounded, per-match-latest human-move judge (local rules)."""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._task: asyncio.Task[Any] | None = None
        self._stopping = False
        self._pending: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._worker(), name="gamehub-notice-judge")

    async def stop(self) -> None:
        self._stopping = True
        self._pending.clear()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.debug("notice_judge stop error", exc_info=True)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    #  Entry point (called synchronously by TableTalk; never blocks the lock)
    # ------------------------------------------------------------------
    def on_event(self, room: Any, event: dict[str, Any]) -> None:
        if self._stopping or getattr(self._plugin, "stopping", False):
            return
        match = event.get("match") or getattr(room, "match", None)
        mid = str(
            event.get("match_id") or getattr(match, "id", "") or id(room)
        )
        # Keep only the latest move per match: older events are meaningless
        # and would pile up unbounded.
        if mid in self._pending:
            self._pending[mid] = (room, dict(event))
            self._wake.set()
            return
        if len(self._pending) >= _MAX_PENDING:
            self._pending.pop(next(iter(self._pending)), None)
        self._pending[mid] = (room, dict(event))
        self._wake.set()

    # ------------------------------------------------------------------
    #  Worker
    # ------------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            try:
                await self._wake.wait()
            except asyncio.CancelledError:
                raise
            self._wake.clear()
            while self._pending and not self._stopping:
                mid, (room, event) = next(iter(self._pending.items()))
                del self._pending[mid]
                try:
                    await self._process(room, event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("notice_judge judgment failed", exc_info=True)

    async def _process(self, room: Any, event: dict[str, Any]) -> None:
        started = time.monotonic()
        match = event.get("match") or getattr(room, "match", None)
        if match is None:
            return
        seat = int(event.get("seat", -1))
        base = {
            "t": int(time.time() * 1000),
            "match_id": str(getattr(match, "id", "")),
            "version": int(getattr(match, "version", 0) or 0),
            "move_count": len(getattr(match, "log", ()) or ()),
            "seat": seat,
            "move": str(event.get("move") or ""),
        }
        judgment = await self._evaluate(room, event)
        if self._stopping or getattr(self._plugin, "stopping", False):
            return
        status = "skip"
        talk = getattr(self._plugin, "_talk", None)
        if talk is not None:
            status = await talk.on_judged_event(room, event, judgment)
        # Priority with an existing plan: mark review under the lock (no
        # seq/version bump). Teammate matches skip (any seat plan belongs to
        # the opponent's seat).
        if (
            judgment.priority
            and status != "stale"
            and modes.role_of(match) != modes.ROLE_TEAMMATE
        ):
            plan_seat = ai_seat_index(match)
            marker = getattr(room, "mark_plan_review", None)
            if plan_seat is not None and callable(marker):
                try:
                    await marker(plan_seat)
                except Exception:
                    logger.debug("plan review marking failed", exc_info=True)
        await self._log(
            {
                **base,
                "fast": bool(judgment.fast),
                "key_events": list(judgment.key_flags),
                "priority": bool(judgment.priority),
                "speak": bool(judgment.speak),
                "fallback": judgment.fallback,
                "reason": judgment.reason,
                "status": status,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

    async def _evaluate(self, room: Any, event: dict[str, Any]) -> Judgment:
        match = event.get("match") or getattr(room, "match", None)
        if match is None:
            return Judgment(False, reason="no_match")
        mode = str(_cfg(self._plugin, "notice_judge_mode", "key") or "key")
        if getattr(self._plugin, "stopping", False):
            return Judgment(False, reason="stopping")
        if mode == "off":
            # Judge disabled: if the page still selects the judge tier,
            # TableTalk falls back to the legacy key behaviour.
            return Judgment(False, reason="mode_off", fallback="off")
        status = status_of(match)
        if status.outcome is not None or event.get("type") != "move":
            return Judgment(False, reason="not_live_move")
        seat = modes.trigger_seat(match, event)
        if not (0 <= seat < len(match.seats)):
            return Judgment(False, reason="bad_seat")
        if not modes.is_wanted_trigger(match, seat):
            # Role-aware: opponent matches only judge human moves (AI replies
            # silent); teammate matches only judge the opponent's reply.
            return Judgment(False, reason="ai_move")

        flags = _module_key_events(match, seat, event)
        key_names = tuple(
            name for name, value in flags.items() if name != "key" and bool(value)
        )
        if flags.get("key"):
            return Judgment(
                True,
                priority=True,
                reason="key",
                fast=True,
                key_flags=key_names,
            )
        if _plan_expired(match) and modes.role_of(match) != modes.ROLE_TEAMMATE:
            return Judgment(
                True,
                priority=True,
                reason="plan_expired",
                fast=True,
                key_flags=key_names,
            )
        # Local rules only: no key signal => stay quiet.
        return Judgment(False, reason="no_key_event", key_flags=key_names)

    # ------------------------------------------------------------------
    #  Logging (one line per human move; no tokens or full chat)
    # ------------------------------------------------------------------
    async def _log(self, entry: dict[str, Any]) -> None:
        if not bool(_cfg(self._plugin, "notice_judge_log", False)):
            return
        path = self._log_path()
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            await asyncio.to_thread(_append_line, path, line)
        except Exception:
            logger.warning("notice_judge log write failed: %s", path, exc_info=True)

    def _log_path(self) -> Path:
        base = getattr(self._plugin, "data_dir", None)
        return Path(base or ".") / "notice_judge_log.jsonl"


def _plan_expired(match: Any) -> bool:
    """Whether this move crosses the plan's turn TTL boundary (fires once)."""
    seat = ai_seat_index(match)
    if seat is None:
        return False
    plan = match.seats[seat].plan
    if not isinstance(plan, dict) or not str(plan.get("text") or "").strip():
        return False
    try:
        tempo = str(games.get(match.kind).tempo or "turn")
    except Exception:
        tempo = "turn"
    if tempo != "turn":
        return False
    try:
        boundary = int(plan.get("created_move") or 0) + int(
            plan.get("ttl_moves") or 0
        )
    except (TypeError, ValueError):
        return False
    return len(getattr(match, "log", ()) or ()) == boundary


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
