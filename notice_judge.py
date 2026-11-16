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
  trigger (opponent matches, turn-based games only);
- **No network**: System1 was removed; ``judge`` tier is purely local. No
  score/probability/gray-zone logic remains.
- **Event snapshot**: judge only on ``event["match"]`` / ``event["before_state"]``,
  never on the possibly-advanced ``room.match``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # production: plugin package (``plugins.<plugin>.notice_judge``)
    from . import modes
    from .engine import games
    from .engine.match import status_of
    from .plan import ai_seat_index
except ImportError:  # standalone tests import modules as top-level
    import modes
    from engine import games
    from engine.match import status_of
    from plan import ai_seat_index

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

_MAX_PENDING = 4

# Judgement-log rotation: one previous generation (`*.jsonl.1`) beyond the
# size cap, so an always-on judge log cannot grow without bound (P2).
_JSONL_MAX_BYTES = 2 * 1024 * 1024
_JSONL_LOCK = threading.Lock()


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
        # Empty string means "unset" and must behave like the shipped default
        # (off): `or "key"` here turned an explicitly cleared setting into the
        # most talkative fast channel (fail-open, P2).
        mode = str(_cfg(self._plugin, "notice_judge_mode", "off") or "off").strip()
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
        # Teammate matches never reach the judge: TableTalk's on_event only
        # forwards judge-tier moves for opponent matches, so the wanted
        # trigger here is effectively "the human moved".
        if not modes.is_wanted_trigger(match, seat):
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
    """Append one JSONL entry; rotate once past the size cap (bounded disk)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JSONL_LOCK:
        try:
            if path.exists() and path.stat().st_size > _JSONL_MAX_BYTES:
                rotated = path.with_name(path.name + ".1")
                with contextlib.suppress(OSError):
                    path.replace(rotated)  # drop the previous .1
        except OSError:
            pass  # stat/rotate failures must never block the write
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
