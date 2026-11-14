"""Table talk: turn match events into in-character player-voice notices.

Pipeline:

    match event (start / move / over / resign)
      -> tier + cooldown/dedup gate in this module
      -> render the ``table_talk_prompt_template`` into a system notice
      -> ``host.notices.send_notice`` publishes it into the game session
      -> the host chat pipeline replies inside that session

Design constraints (unchanged from nori):

- Prompt guidance only, no code gate on what the reply says;
- One serial worker: finish a reply round before dispatching the next notice;
- Cooldown is tracked per match (``room.last_talk_ts``), not globally;
- ``judge`` tier delegates "speak or stay quiet" to ``notice_judge`` (LOCAL
  key-event rules only, no network); ``moves`` = every move with cooldown;
  ``key`` = every N moves + start/end; ``off`` = silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import TYPE_CHECKING, Any

try:  # production: plugin package (``plugins.<plugin>.talk``)
    from . import modes
    from .engine.match import status_of
    from .host.notices import send_notice
    from .modes import ROLE_TEAMMATE
    from .targets import session_type_of
except ImportError:  # standalone tests import modules as top-level
    import modes
    from engine.match import status_of
    from modes import ROLE_TEAMMATE
    from targets import session_type_of

    try:
        from host.notices import send_notice
    except ImportError:  # pragma: no cover - host adapter absent in tests
        send_notice = None  # type: ignore[assignment]

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - type hints only
    from .notice_judge import Judgment, NoticeJudge


DEFAULT_LANE_HINT = "只用一两句玩家口吻的话回应，别做别的事。"
# Reserved notice sender id: keeps table-talk notices distinguishable from real
# users if a renderer surfaces the source id.
_NOTICE_USER_ID = "nori_notice"
_QUEUE_MAX = 4
_SEEN_TTL_S = 600.0
_SEEN_MAX = 256

# Control markers / XML-ish tags stripped from untrusted event text so a name
# or plan cannot forge a system block inside the notice prompt.
_CONTROL_TAG_RE = re.compile(
    r"</?\s*(?:system|assistant|user|tool|instruction|prompt)\b[^>]*>",
    re.IGNORECASE,
)
_CONTROL_BRACKET_RE = re.compile(r"\[\s*/?\s*system\s*:[^\]]*\]", re.IGNORECASE)


def neutralize_notice_packet_keys(value: str) -> str:
    """Strip system-control markers and XML-ish tags from untrusted text.

    Local, minimal replacement for nori core's helper: event text (names, move
    strings, plan snippets) must never be able to open a ``[System: ...]``
    block or an ``<instruction>`` tag inside the notice.
    """
    text = str(value or "")
    text = _CONTROL_BRACKET_RE.sub("", text)
    text = _CONTROL_TAG_RE.sub("", text)
    return text


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _safe(value: Any) -> str:
    """Isolate notice text from host packet keys / system block separators."""
    text = str(value or "")
    return (
        neutralize_notice_packet_keys(text)
        .replace("userid:", "userid：")
        .replace("[System:", "［System：")
        .replace("]", "］")
    )


def _bot_nickname(plugin: Any) -> str:
    """Display name of the host agent that speaks the table talk."""
    ctx = getattr(plugin, "ctx", None)
    bot_config = getattr(ctx, "bot_config", None)
    bot = getattr(bot_config, "bot", None)
    for candidate in (
        getattr(plugin, "host_agent_name", ""),
        getattr(bot, "nickname", ""),
        getattr(plugin, "bot_nickname", ""),
    ):
        name = str(candidate or "").strip()
        if name:
            return name
    return "对手"


def build_prompt(
    template: str,
    *,
    opponent: str,
    game: str,
    seat: str,
    event: str,
    board: str,
    plan: str,
    lane_hint: str,
) -> str:
    """Render the notice body from a template (pure; unknown keys -> "")."""
    try:
        return str(template).format_map(
            _SafeDict(
                opponent=_safe(opponent),
                game=_safe(game),
                seat=_safe(seat),
                event=_safe(event),
                board=_safe(board),
                plan=_safe(plan),
                lane_hint=_safe(lane_hint),
            )
        ).strip()
    except Exception:
        logger.warning("table_talk prompt template render failed, using default", exc_info=True)
        return (
            f"[System: 你正和 {_safe(opponent)} 下{_safe(game)}，你是执{_safe(seat)}的一方，"
            f"这是你们的对局，不是解说。背景（不要照读）：刚刚：{_safe(event)}；"
            f"当前局面：{_safe(board)}。像坐在对面的朋友那样自然说一两句短句，"
            f"不要主动报棋盘坐标或复述棋谱；玩家主动问坐标或复盘时可如实回答。"
            f"{_safe(lane_hint)}]"
        )


def should_talk(
    mode: str,
    event_type: str,
    *,
    move_count: int,
    key_every_n: int,
    over: bool,
) -> bool:
    """Tier gate: off = silent; key = start/end/first move/every N moves."""
    if mode == "off":
        return False
    if event_type == "start":
        return True
    if over or event_type in ("over", "resign"):
        return True
    if event_type != "move":
        return False
    if mode == "moves":
        return True
    return move_count <= 1 or (key_every_n > 0 and move_count % key_every_n == 0)


class TableTalk:
    """Single-worker event -> notice sender."""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._queue: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._task: asyncio.Task[Any] | None = None
        self._stopping = False
        # Minimum gap per target (also applies across matches).
        self._last_target: dict[str, float] = {}
        # Serialize the shared delivery path: judge worker and talk worker may
        # both dispatch concurrently.
        self._deliver_lock = asyncio.Lock()
        # Dedup key -> timestamp (terminal move + over for one version once).
        self._seen: dict[tuple[Any, ...], float] = {}
        # Local notice judge (wired by runtime; None => judge tier falls back
        # to the legacy key behaviour).
        self._judge: NoticeJudge | None = None

    def attach_judge(self, judge: NoticeJudge) -> None:
        self._judge = judge

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._worker(), name="gamehub-table-talk")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        # A reply round may swallow cancellation: give it 5s, never block shutdown.
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("table_talk stop error", exc_info=True)

    # ------------------------------------------------------------------
    #  Entry point (GameRoom event hook; sync, never blocks the match lock)
    # ------------------------------------------------------------------
    def on_event(self, room: Any, event: dict[str, Any]) -> None:
        match = event.get("match") or getattr(room, "match", None)
        if self._stopping or not self._enabled(match):
            return
        if event.get("type") not in ("start", "move", "over", "resign"):
            return
        if match is None:
            return
        # Turn-director-owned matches publish their own action notices;
        # table talk must yield so the same stage is not announced twice.
        if self._director_owns(match):
            return
        mode = self._mode_for(match)
        event_type = str(event.get("type") or "")
        over = self._event_over(match, event)
        if event_type == "move" and not over:
            role = modes.role_of(match)
            mover = modes.trigger_seat(match, event)
            if role == ROLE_TEAMMATE and not modes.is_wanted_trigger(match, mover):
                # Teammate matches: comment only on the opponent's reply.
                return
            if mode == "judge" and role != ROLE_TEAMMATE:
                if not modes.is_wanted_trigger(match, mover):
                    # Existing behaviour: judge tier stays silent on AI replies.
                    return
                judge = self._judge
                if judge is not None:
                    judge.on_event(room, event)
                    return
        try:
            self._put_item(("event", room, dict(event)))
        except Exception:
            logger.warning("table_talk event enqueue failed", exc_info=True)

    def _director_stage(self, match: Any, event: dict[str, Any]) -> bool:
        """Whether start/end of a turn-driven match is owned by the director."""
        helper = getattr(self._plugin, "turn_owns", None)
        if not callable(helper) or getattr(self._plugin, "_director", None) is None:
            return False
        if modes.options_of(match).get("driver") != modes.DRIVER_TURN:
            return False
        event_type = str(event.get("type") or "")
        if event_type == "start":
            return True
        return event_type in ("over", "resign") or bool(event.get("over"))

    def _director_owns(self, match: Any) -> bool:
        """Whether the turn director owns this match's action notices."""
        helper = getattr(self._plugin, "turn_owns", None)
        if callable(helper):
            try:
                return bool(helper(match))
            except Exception:
                return False
        return modes.is_directed(match) or modes.is_delegated(match)

    def _put_item(self, item: tuple[Any, ...]) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Drop the oldest: the player only cares about the latest event.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(item)

    def _mode_for(self, match: Any) -> str:
        """Infer this match's notice tier from its source (page/group/private)."""
        cfg = self._plugin.settings
        is_page = getattr(self._plugin, "is_page_match", None)
        if callable(is_page):
            page_match = bool(is_page(match))
        else:  # lenient fallback: origin marker, then session shape
            origin = getattr(self._plugin, "match_origin", None)
            origin_kind = ""
            if callable(origin):
                try:
                    origin_kind = str((origin(match) or {}).get("kind") or "")
                except Exception:
                    origin_kind = ""
            human = next((s for s in match.seats if s.kind == "human"), None)
            sid = str(getattr(human, "session_id", "") or "") if human else ""
            page_session = getattr(self._plugin, "page_session_id", None)
            page_match = origin_kind == "webui" or (
                not origin_kind
                and (
                    not sid
                    or sid in ("webui", "page")
                    or (page_session is not None and sid == page_session)
                )
            )
        if page_match:
            return str(getattr(cfg, "page_chat_talk_default", "key") or "key")
        human = next((s for s in match.seats if s.kind == "human"), None)
        sid = str(getattr(human, "session_id", "") or "") if human else ""
        if session_type_of(sid) == "group":
            return str(getattr(cfg, "table_talk_group_default", "key") or "key")
        return str(getattr(cfg, "table_talk_default", "moves") or "moves")

    @staticmethod
    def _event_over(match: Any, event: dict[str, Any]) -> bool:
        if bool(event.get("over")):
            return True
        try:
            return status_of(match).outcome is not None
        except Exception:
            return False

    @staticmethod
    def _room_over(room: Any) -> bool:
        if str(getattr(room, "status", "") or "") == "over":
            return True
        current = getattr(room, "match", None)
        if current is None:
            return False
        try:
            return status_of(current).outcome is not None
        except Exception:
            return False

    def _enabled(self, match: Any = None) -> bool:
        cfg = self._plugin.settings
        # Notices are useless without a publish path (host.notices).
        ctx = getattr(self._plugin, "ctx", None)
        if not callable(getattr(ctx, "publish_notice", None)):
            return False
        # When the page switch is off, an explicit external override still
        # allows companion notices; the page drawer itself is separate.
        if getattr(cfg, "page_chat_enabled", True):
            return True
        if str(getattr(cfg, "notice_target_session_id", "") or "").strip():
            return True
        # Chat-originated matches deliver to their origin session regardless of
        # the page switch: do not silence private/group matches.
        if match is not None:
            origin = getattr(self._plugin, "match_origin", None)
            if callable(origin):
                try:
                    return str(origin(match).get("kind") or "") == "chat"
                except Exception:
                    logger.debug("table_talk origin inference failed", exc_info=True)
        return False

    # ------------------------------------------------------------------
    #  Worker
    # ------------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                if item[0] == "text":
                    _kind, session_id, text, room, meta = item
                    # The room may have ended between the gate and the send:
                    # do not replay a stale non-terminal board after the end.
                    if not bool(meta.get("over")) and self._room_over(room):
                        logger.info(
                            "table_talk dropped stale queued notice: match=%s v=%s",
                            meta.get("match_id"),
                            meta.get("version"),
                        )
                        continue
                    await self._send(str(session_id), str(text))
                else:
                    await self._dispatch(item[1], item[2])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("table_talk send failed", exc_info=True)

    async def on_judged_event(
        self, room: Any, event: dict[str, Any], judgment: Judgment
    ) -> str:
        """The judge's only delivery path: shared cooldown/dedup, no direct reply.

        Only does non-network gating and text synthesis, then queues the ready
        text into the existing talk queue (sent by the talk worker). This keeps
        the judge worker from blocking on notice generation/delivery.
        """
        return await self._dispatch(room, event, judgment=judgment, enqueue=True)

    async def _dispatch(
        self,
        room: Any,
        event: dict[str, Any],
        *,
        judgment: Judgment | None = None,
        enqueue: bool = False,
    ) -> str:
        """Shared exit: return a delivery result (judge logs cooldown/dedup)."""
        cfg = self._plugin.settings
        if self._stopping or getattr(self._plugin, "stopping", False):
            return "stopping"
        match = event.get("match") or getattr(room, "match", None)
        if match is None:
            return "no_match"
        role = modes.role_of(match)
        if role != ROLE_TEAMMATE and not any(s.kind == "ai" for s in match.seats):
            return "no_ai_seat"

        target = await self._plugin.notice_target(room)
        if not target:
            return "no_target"
        session_id, _is_page = target

        # Tier follows the match's own source (page/group/private), not the
        # delivery platform: a page match overridden to live2d still uses
        # page_chat_talk_default.
        mode = self._mode_for(match)
        status = status_of(match)
        over = status.outcome is not None
        event_type = str(event.get("type") or "")
        mover = modes.trigger_seat(match, event)

        # A late judge result may land after the room ended: never queue a
        # stale non-terminal board behind the end notice.
        if judgment is not None and not over and self._room_over(room):
            return "stale"

        if (
            role == ROLE_TEAMMATE
            and event_type == "move"
            and not over
            and not modes.is_wanted_trigger(match, mover)
        ):
            # Teammate matches: never self-trigger on our own side's move.
            return "self_trigger"

        if (
            mode == "judge"
            and event_type == "move"
            and not over
            and not modes.is_wanted_trigger(match, mover)
        ):
            # Role-aware: judge tier is silent on AI/bot replies in opponent
            # matches; teammate matches only comment on the opponent's reply.
            return "ai_silent"

        # Teammate matches: say something on EVERY opponent reply (explicit
        # requirement), bypassing judge/tier/cooldown but still deduped per
        # (match, version, seat); our own side never self-triggers.
        teammate_each = (
            role == ROLE_TEAMMATE
            and event_type == "move"
            and not over
            and modes.is_wanted_trigger(match, mover)
        )

        if event_type == "start" and not bool(getattr(cfg, "talk_on_start", True)):
            return "disabled_start"
        if (over or event_type in ("over", "resign")) and not bool(
            getattr(cfg, "talk_on_over", True)
        ):
            return "disabled_over"
        # Turn-driven matches own their start/end notices via the director.
        if self._director_stage(match, event):
            return "director_stage"
        if mode == "off" and judgment is None and not teammate_each:
            # off tier: start/end also silent; teammate each-reply is exempt.
            return "mode_off"

        # -- Speak or stay quiet -------------------------------------------
        if teammate_each or event_type == "start" or over or event_type in ("over", "resign"):
            priority, fallback = False, False
        elif judgment is not None and not judgment.is_fallback:
            priority, fallback = bool(judgment.priority), False
            if not judgment.speak:
                return "judge_skip"
        else:
            fallback = judgment is not None and judgment.is_fallback
            eff_mode = "key" if (mode == "judge" or fallback) else mode
            priority = False
            if not should_talk(
                eff_mode,
                event_type,
                move_count=len(match.log),
                key_every_n=int(getattr(cfg, "talk_key_every_n", 5)),
                over=over,
            ):
                return "fallback_skip" if fallback else "mode_skip"

        # -- Cooldown / dedup (shared exit; judge and legacy never double-send)
        now = time.time()
        judged = judgment is not None and not judgment.is_fallback
        dkey = self._dedup_key(match, event, event_type, mover, over)
        async with self._deliver_lock:
            self._prune_seen(now)
            if dkey in self._seen:
                return "dedup"
            if not teammate_each and not over:
                min_interval = float(getattr(cfg, "talk_min_interval_s", 15.0))
                if judged:
                    # Judge cooldown is per match (independent matches sharing
                    # one page session must not suppress each other); high
                    # priority may drop to notice_key_min_interval_s.
                    if priority:
                        min_interval = min(
                            min_interval,
                            float(getattr(cfg, "notice_key_min_interval_s", 8.0)),
                        )
                    last_talk = float(getattr(room, "last_talk_ts", 0.0) or 0.0)
                    if now - last_talk < min_interval:
                        return "cooldown"
                elif now - self._last_target.get(session_id, 0.0) < min_interval:
                    return "cooldown"
            if not teammate_each and event_type == "move" and not over:
                plain = judgment is None or judgment.is_fallback
                cooldown = float(getattr(cfg, "talk_cooldown_s", 30.0))
                if plain and now - float(getattr(room, "last_talk_ts", 0.0) or 0.0) < cooldown:
                    return "cooldown30"
            self._seen[dkey] = now
            if not judged:
                # start/end/legacy keep the same-target guard; judge does not
                # update it so independent matches stay unblocked.
                self._last_target[session_id] = now
            room.last_talk_ts = now

        fields = self._fields(match, event, status)
        template = str(getattr(cfg, "table_talk_prompt_template", "") or "")
        extra = str(getattr(cfg, "table_talk_prompt_extra", "") or "").strip()
        lane_hint = DEFAULT_LANE_HINT + (f" {extra}" if extra else "")
        text = build_prompt(template, **fields, lane_hint=lane_hint)
        if not text:
            return "empty_prompt"

        if enqueue:
            self._put_item(
                (
                    "text",
                    session_id,
                    text,
                    room,
                    {
                        "match_id": getattr(match, "id", ""),
                        "version": getattr(match, "version", 0),
                        "over": bool(over),
                    },
                )
            )
            return "queued"

        try:
            await self._send(session_id, text)
        except Exception:
            logger.warning("table_talk send failed", exc_info=True)
            return "send_failed"
        return "sent"

    @staticmethod
    def _dedup_key(
        match: Any,
        event: dict[str, Any],
        event_type: str,
        seat: int,
        over: bool,
    ) -> tuple[Any, ...]:
        if over or event_type in ("over", "resign"):
            return ("end", getattr(match, "id", ""), getattr(match, "version", 0))
        if event_type == "start":
            return ("start", getattr(match, "id", ""), getattr(match, "version", 0))
        return ("move", getattr(match, "id", ""), getattr(match, "version", 0), seat)

    def _prune_seen(self, now: float) -> None:
        if not self._seen:
            return
        stale = [key for key, ts in self._seen.items() if now - ts > _SEEN_TTL_S]
        for key in stale:
            self._seen.pop(key, None)
        while len(self._seen) > _SEEN_MAX:
            self._seen.pop(next(iter(self._seen)), None)

    def _fields(self, match: Any, event: dict[str, Any], status: Any) -> dict[str, str]:
        try:  # production: plugin package
            from .engine import games
        except ImportError:  # standalone tests
            from engine import games

        mod = games.get(match.kind)
        if match.kind == "doudizhu":
            human = modes.human_seat_index(match)
            if human is None:
                human = 0
            names = [s.name for s in match.seats]
            mover = modes.last_mover(match, event)
            return {
                "opponent": "、".join(names[i] for i in range(3) if i != human),
                "game": "斗地主", "seat": f"{names[human]} 的场边队友（不占席位）",
                "event": f"{names[mover]} 完成公开动作" if 0 <= mover < 3 else "对局开始",
                "board": mod.describe(match.state, None, names), "plan": "未设定",
            }
        labels = mod.seat_labels(match.state)
        role = modes.role_of(match)
        teammate = role == ROLE_TEAMMATE
        nori_seat = next(
            (i for i, s in enumerate(match.seats) if s.kind == "ai"), 0
        )
        human_seat = next(
            (i for i, s in enumerate(match.seats) if s.kind == "human"), 0
        )
        opponent_seat = modes.opponent_seat_index(match)
        if opponent_seat is None:
            opponent_seat = 1 if human_seat == 0 else 0
        human = match.seats[human_seat]
        rival = match.seats[opponent_seat]
        and_move = str(event.get("move") or "").strip()
        event_type = str(event.get("type") or "")
        mover = modes.last_mover(match, event)
        if event_type == "start":
            if teammate:
                # Teammate role is explained once in the seat field; the start
                # event does not repeat "which side / only cheers".
                event_text = (
                    f"开局了，{human.name} 执{self._label(labels, human_seat)}先行，"
                    f"对手 {rival.name} 执{self._label(labels, opponent_seat)}。"
                )
            else:
                event_text = (
                    f"开局了，{human.name} 执{self._label(labels, human_seat)}先行，"
                    f"你执{self._label(labels, nori_seat)}。"
                )
        elif status.outcome is not None:
            event_text = f"对局结束：{status.result_text or status.outcome.text}。"
        elif event_type == "resign":
            event_text = f"{human.name} 认输了。"
        elif teammate:
            if mover == human_seat:
                event_text = f"{human.name} 落子 {and_move or '一手'}。"
            else:
                event_text = (
                    f"对手 {rival.name} 落子 {and_move or '一手'}，"
                    f"轮到 {human.name} 了。"
                )
        elif int(event.get("seat", -1)) == human_seat:
            event_text = f"{human.name} 刚落子 {and_move or '一手'}。"
        else:
            event_text = f"你刚落子 {and_move or '一手'}。"

        plan_seat = nori_seat if not teammate else None
        plan = (
            match.seats[plan_seat].plan
            if plan_seat is not None and plan_seat < len(match.seats)
            else None
        )
        plan_text = ""
        if isinstance(plan, dict):
            plan_text = str(plan.get("text") or plan.get("goal") or "").strip()
        elif plan:
            plan_text = str(plan).strip()

        if teammate:
            seat_field = (
                f"{self._label(labels, human_seat)}"
                f"（{human.name} 的队友，只帮腔不落子）"
            )
        else:
            seat_field = self._label(labels, nori_seat)
        return {
            "opponent": rival.name if teammate else human.name,
            "game": mod.name["zh"],
            "seat": seat_field,
            "event": event_text,
            "board": self._board_summary(match, labels),
            # Teammate matches carry no plan: any seat plan belongs to the
            # opponent's seat and must not be fed to our own context.
            "plan": (plan_text or "未设定") if not teammate else "未设定",
        }

    @staticmethod
    def _label(labels: list[str], seat: int) -> str:
        return labels[seat] if 0 <= seat < len(labels) and labels[seat] else "棋手"

    @staticmethod
    def _board_summary(match: Any, labels: list[str]) -> str:
        status = status_of(match)
        names = [s.name for s in match.seats]
        parts = [f"第 {len(match.log)} 手"]
        recent = match.log[-6:]
        if recent:
            shown = []
            for entry in recent:
                move = "认输" if entry.move == "resign" else entry.move
                who = names[entry.seat] if entry.seat < len(names) else "棋手"
                shown.append(f"{who} {move}")
            parts.append("最近：" + "、".join(shown))
        if status.outcome is not None:
            parts.append("已结束")
        elif status.waiting_on:
            waiting = [names[s] for s in status.waiting_on if s < len(names)]
            if waiting:
                parts.append("轮到 " + "、".join(waiting))
        return "；".join(parts)

    async def _send(self, session_id: str, text: str) -> None:
        """Publish one notice through the host adapter (``host.notices``)."""
        sender = send_notice
        if sender is None:  # pragma: no cover - host adapter absent in tests
            try:  # production: plugin package
                from .host.notices import send_notice as host_send
            except ImportError:  # standalone tests import modules as top-level
                from host.notices import send_notice as host_send
            sender = host_send
        ok = await sender(self._plugin, session_id, text)
        if not ok:
            raise RuntimeError(f"table_talk: notice publish failed for {session_id!r}")
        logger.info("table_talk published notice session=%s", session_id)
