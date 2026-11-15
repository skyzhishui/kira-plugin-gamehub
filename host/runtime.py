"""Plugin runtime: wires rooms, talk, the local judge and the turn director."""

from __future__ import annotations

import inspect
from collections import OrderedDict
from typing import Any

from core.plugin import logger

from ..engine.match import SOURCE_CHAT, SOURCE_WEBUI
from ..game_agent import GameAgent
from ..modes import DRIVER_AGENT, DRIVER_BOT, DRIVER_TURN, seat_driver_of
from ..notice_judge import NoticeJudge
from ..room import RoomManager
from ..talk import TableTalk
from .bridge import set_outbound_sink, ws_hub
from .session import GAME_SID
from .store import MatchStore

# Fixed identity of the plugin-owned game_agent inside the shared game group.
GAME_AGENT_UID = "game_agent"
# Upper bound on the move-event fingerprints remembered for say deduplication.
_AGENT_SAY_CACHE_LIMIT = 256


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` only when it is awaitable (hooks may be sync)."""
    if inspect.isawaitable(value):
        return await value
    return value


try:  # the director module is optional until the turn wave lands
    from ..director import TurnDirector
except ModuleNotFoundError as exc:  # only tolerate a missing director module
    if not (exc.name or "").endswith("director"):
        raise
    TurnDirector = None  # type: ignore[assignment]


class GamehubRuntime:
    """Owns every long-lived gamehub service for one plugin instance."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.store: MatchStore | None = None
        self.rooms: RoomManager | None = None
        self.talk: TableTalk | None = None
        self.judge: NoticeJudge | None = None
        self.director: Any = None
        self.agent: Any = None
        self._started = False
        # Move-event fingerprints whose agent say was already published, so a
        # replayed/duplicated event never posts the same line twice.
        self._agent_say_seen: OrderedDict[tuple[str, int, str], None] = OrderedDict()

    async def start(self) -> None:
        plugin = self.plugin
        settings = plugin.settings
        self.store = MatchStore(plugin.data_dir)
        await self.store.init()

        # The turn-driven seat is played by the host AI itself, so the UI must
        # be able to tell it apart from the plugin-owned game_agent identity.
        # The name is read from the host every time (never hard-coded): the
        # active persona can be switched while the plugin keeps running.
        plugin.host_agent_name = await self._read_host_name()

        # ``opponent_name`` names the agent-driven seat; keep it identical to
        # the configured game_agent name so the lobby and the seat agree.
        agent_name = str(getattr(settings, "agent_name", "") or "").strip() or "nori"
        self.rooms = RoomManager(
            repository=self.store,
            config=settings,
            opponent_name=agent_name,
            bot_name=plugin.host_agent_name,
            practice_name=settings.practice_name,
        )
        self.talk = TableTalk(plugin)
        self.judge = NoticeJudge(plugin)
        self.talk.attach_judge(self.judge)
        # The plugin-owned game_agent drives ``driver=agent`` seats; the
        # turn-driven seats belong to the director, bot seats to the rules.
        try:
            self.agent = GameAgent(plugin)
        except Exception as e:  # noqa: BLE001 - fall back to rule stepping
            logger.error(f"gamehub: game agent init failed: {e}", exc_info=True)
            self.agent = None
        if TurnDirector is not None:
            self.director = TurnDirector(plugin)

        self.rooms.event_hook = self._dispatch_event
        self.rooms.drive_ai_hook = self._drive_ai
        await self.rooms.restore()
        if self.director is not None:
            try:
                await self.director.restore_active()
            except Exception as e:  # noqa: BLE001 - restore is best-effort
                logger.warning(f"gamehub: turn restore failed: {e}")

        self.talk.start()
        self.judge.start()
        set_outbound_sink(self._on_outbound)

        plugin.stopping = False
        plugin._rooms = self.rooms
        plugin._talk = self.talk
        plugin._judge = self.judge
        plugin._director = self.director
        plugin._runtime = self
        plugin.notice_target = self.notice_target
        plugin.notice_target_session_sync = self.notice_target_session_sync
        plugin.match_origin = self.match_origin
        # talk.py probes ``plugin.turn_owns(match)``: route it to the director
        # so an *active grant* (not just the mode flags) decides ownership.
        if self.director is not None:
            plugin.turn_owns = self.director.owns
        self._started = True
        logger.info("gamehub runtime started")

    async def _read_host_name(self) -> str:
        """Active persona's display name; ``"AI"`` only when the host has none."""
        plugin = self.plugin
        try:
            persona = await plugin.ctx.persona_mgr.get_active_persona()
            name = str(getattr(persona, "name", "") or "").strip()
            if name:
                return name
        except Exception as e:  # noqa: BLE001 - persona is cosmetic
            logger.warning(f"gamehub: failed to read active persona: {e}")
        return "AI"

    async def refresh_host_name(self) -> str:
        """Re-read the host name (persona may have been switched at runtime)."""
        name = await self._read_host_name()
        self.plugin.host_agent_name = name
        rooms = getattr(self.plugin, "rooms", None) or self.rooms
        setter = getattr(rooms, "set_bot_name", None)
        if callable(setter):
            try:
                setter(name)
            except Exception:  # noqa: BLE001 - naming is cosmetic
                logger.debug("gamehub: host-name propagation failed", exc_info=True)
        return name

    async def stop(self) -> None:
        plugin = self.plugin
        plugin.stopping = True
        if self.director is not None:
            try:
                await self.director.stop()
            except Exception as e:  # noqa: BLE001 - teardown must not raise
                logger.warning(f"gamehub: director stop failed: {e}")
        if self.talk is not None:
            await self.talk.stop()
        if self.judge is not None:
            await self.judge.stop()
        if self.store is not None:
            await self.store.close()
        set_outbound_sink(None)
        self._started = False
        logger.info("gamehub runtime stopped")

    # ------------------------------------------------------------------
    #  Room event fan-out
    # ------------------------------------------------------------------

    async def _dispatch_event(self, room: Any, event: dict[str, Any]) -> Any:
        """Buffer the agent say, then director (turn notices), then table talk.

        The room dispatches events from a task, so this hook may await. The
        game_agent's own public line must reach the shared group buffer *before*
        the director's notice for the same move, otherwise the notice's turn
        would answer before the line is visible; the director/table-talk hooks
        are still fire-and-forget and can never block each other.
        """
        try:
            await self._publish_agent_say(room, event)
        except Exception as e:  # noqa: BLE001 - group chat is cosmetic
            logger.warning(f"gamehub: game_agent group say failed: {e}", exc_info=True)
        if self.director is not None:
            await _maybe_await(self.director.on_event(room, event))
        if self.talk is None:
            return None
        return await _maybe_await(self.talk.on_event(room, event))

    async def _publish_agent_say(self, room: Any, event: dict[str, Any]) -> None:
        """Mirror one successful game_agent move line into the shared group chat.

        Only a legal move by a ``DRIVER_AGENT`` seat carries a public ``say``:
        host-delegated moves, rule-bot steps and internal reasoning are never
        forwarded. The event's own ``match`` snapshot supplies the driver and
        identity, so a later ``room.match`` advance cannot misclassify the
        line. The original ``Match.chat`` bubble is left untouched; this only
        adds the spectator-role group event plus the match-scoped chat read
        model and its ``chat_message`` WS frame. Every step is best-effort and
        never blocks the director/table-talk dispatch.
        """
        if str(event.get("type") or "") != "move":
            return
        text = str(event.get("say") or "").strip()
        if not text:
            return
        seat = event.get("seat")
        match = event.get("match") or getattr(room, "match", None)
        if match is None or not isinstance(seat, int):
            return
        try:
            driver = seat_driver_of(match, seat)
        except Exception:  # noqa: BLE001 - a bad snapshot must not break dispatch
            logger.debug("gamehub: agent say driver lookup failed", exc_info=True)
            return
        if driver != DRIVER_AGENT:
            return
        match_id = str(getattr(match, "id", "") or "")
        if not match_id:
            return
        fingerprint = (match_id, int(getattr(match, "version", 0) or 0), text)
        if fingerprint in self._agent_say_seen:
            return
        self._agent_say_seen[fingerprint] = None
        self._agent_say_seen.move_to_end(fingerprint)
        while len(self._agent_say_seen) > _AGENT_SAY_CACHE_LIMIT:
            self._agent_say_seen.popitem(last=False)

        name = str(getattr(self.plugin.settings, "agent_name", "") or "").strip() or "agent"
        # 1) Shared group session: the line is buffered (spectator role only,
        #    never arming a host reply) before this call returns, so the main
        #    AI can never be triggered and the buffer precedes the director.
        try:
            from .group_chat import publish_agent_say

            await publish_agent_say(
                self.plugin, GAME_AGENT_UID, name, text, match_id=match_id,
            )
        except Exception as e:  # noqa: BLE001 - group chat is cosmetic
            logger.warning(f"gamehub: agent say publish failed ({match_id}): {e}")

        # 2) Match-page read model + WS fan-out (same read model as player
        #    lines): one stored row keyed by id/ts, then one chat_message frame.
        line: dict[str, Any] | None = None
        if self.store is not None:
            try:
                line = await self.store.chat_append(
                    "agent", name, text, match_id=match_id, uid=GAME_AGENT_UID,
                )
            except Exception:  # noqa: BLE001 - read model is cosmetic
                logger.debug("gamehub: agent say chat record failed", exc_info=True)
        payload = line or {
            "role": "agent", "sender": name, "text": text,
            "match_id": match_id, "uid": GAME_AGENT_UID,
        }
        try:
            await ws_hub.broadcast(match_id, {"type": "chat_message", **payload})
        except Exception:  # noqa: BLE001 - WS fan-out is cosmetic
            logger.debug("gamehub: agent say broadcast failed", exc_info=True)

    async def _drive_ai(self, room: Any, seat: int) -> None:
        """Route AI seats by driver: turn = notice-driven, bot = rules, agent = LLM."""
        if getattr(self.plugin, "stopping", False):
            return
        match = getattr(room, "match", None)
        driver = seat_driver_of(match, seat)
        if driver == DRIVER_TURN:
            # The turn director owns this seat; it is filled by a notice turn
            # and by the watchdog, never by automatic stepping.
            return
        if driver == DRIVER_BOT or self.agent is None:
            await room.ai_step(seat)
            return
        await self.agent.play(room, seat)

    # ------------------------------------------------------------------
    #  Outbound rendering
    # ------------------------------------------------------------------

    async def _on_outbound(self, sid: str, chain: Any) -> None:
        """Forward one agent reply from the internal session to match pages."""
        text = self._chain_text(chain)
        await self.record_agent_line(sid, text)

    async def record_agent_line(self, sid: str, text: str) -> None:
        """Write one agent line to the window read model + WS, no message pipeline.

        The stop-on-move path (``AFTER_XML_PARSE`` event.stop) skips normal
        reply delivery, so a move turn's table talk would never reach the
        page chat window through ``_on_outbound``. This is the same write
        without the pipeline: one sqlite row (id/ts keyed, so the page can
        never duplicate it against a reconnect backfill) plus the
        ``agent_message`` WS frame the window already ingests.
        """
        text = str(text or "").strip()
        if not text:
            return
        if sid != GAME_SID:
            return
        # Record the reply for the match-page chat window read model (the
        # player line is recorded by the POST /chat endpoint; this is the
        # agent side). Cosmetic on failure: the WS broadcast still goes out.
        # The frame carries the stored line's id/ts when available: the page
        # keys WS rows and reconnect backfills by the same id, so a refresh
        # after a reconnect can never duplicate a reply.
        for room in self.rooms.active_rooms() if self.rooms is not None else []:
            scoped = None
            if self.store is not None:
                try:
                    scoped = await self.store.chat_append(
                        "agent", self.plugin.host_agent_name or "AI", text,
                        match_id=room.match.id,
                    )
                except Exception:
                    logger.debug("gamehub: chat record failed", exc_info=True)
            await ws_hub.broadcast(room.match.id, {
                "type": "agent_message",
                "sid": sid,
                "text": text,
                **({"id": scoped["id"], "ts": scoped["ts"]} if scoped else {}),
            })

    @staticmethod
    def _chain_text(chain: Any) -> str:
        elements = getattr(chain, "message_list", None)
        if elements is None:
            elements = chain if isinstance(chain, (list, tuple)) else []
        parts: list[str] = []
        for element in elements:
            text = getattr(element, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts)

    # ------------------------------------------------------------------
    #  Notice target resolution (simplified from nori)
    # ------------------------------------------------------------------

    def match_origin(self, match: Any) -> dict[str, str]:
        """Chat-origin matches reply into their own session; page matches use gamehub."""
        for seat in getattr(match, "seats", []) or []:
            if getattr(seat, "source", "") == SOURCE_CHAT and getattr(seat, "session_id", ""):
                return {"kind": SOURCE_CHAT, "session_id": str(seat.session_id)}
        return {"kind": SOURCE_WEBUI, "session_id": ""}

    def _override_sid(self) -> str:
        return str(getattr(self.plugin.settings, "notice_target_session_id", "") or "").strip()

    async def notice_target(self, room: Any) -> tuple[str, bool] | None:
        """Return ``(session_id, is_page)`` for talk notices."""
        match = getattr(room, "match", None)
        origin = self.match_origin(match)
        if origin["kind"] == SOURCE_CHAT and origin["session_id"]:
            return origin["session_id"], False
        override = self._override_sid()
        if override:
            return override, True
        return GAME_SID, True

    def notice_target_session_sync(self, room: Any) -> str | None:
        """Pure-memory target lookup used by the per-turn prompt builder."""
        match = getattr(room, "match", None)
        origin = self.match_origin(match)
        if origin["kind"] == SOURCE_CHAT and origin["session_id"]:
            return origin["session_id"]
        return self._override_sid() or GAME_SID
