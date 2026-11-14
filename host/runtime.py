"""Plugin runtime: wires rooms, talk, the local judge and the turn director."""

from __future__ import annotations

from typing import Any

from core.plugin import logger

from ..engine.match import SOURCE_CHAT, SOURCE_WEBUI
from ..game_agent import GameAgent
from ..modes import DRIVER_BOT, DRIVER_TURN, seat_driver_of
from ..notice_judge import NoticeJudge
from ..room import RoomManager
from ..talk import TableTalk
from .bridge import set_outbound_sink, ws_hub
from .session import GAME_SID
from .store import MatchStore

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

    async def start(self) -> None:
        plugin = self.plugin
        settings = plugin.settings
        self.store = MatchStore(plugin.data_dir)
        await self.store.init()

        bot_name = "AI"
        try:
            persona = await plugin.ctx.persona_mgr.get_active_persona()
            bot_name = str(getattr(persona, "name", "") or "").strip() or bot_name
        except Exception as e:  # noqa: BLE001 - persona is cosmetic
            logger.warning(f"gamehub: failed to read active persona: {e}")

        self.rooms = RoomManager(
            repository=self.store,
            config=settings,
            opponent_name=settings.agent_name,
            bot_name=bot_name,
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
        self._started = True
        logger.info("gamehub runtime started")

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

    def _dispatch_event(self, room: Any, event: dict[str, Any]) -> Any:
        """Director (mandatory turn notices) first, then table talk."""
        if self.director is not None:
            self.director.on_event(room, event)
        if self.talk is None:
            return None
        return self.talk.on_event(room, event)

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
        if not text:
            return
        for room in self.rooms.active_rooms() if self.rooms is not None else []:
            await ws_hub.broadcast(room.match.id, {
                "type": "agent_message",
                "sid": sid,
                "text": text,
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
