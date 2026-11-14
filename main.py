"""KiraAI Game Hub plugin — gomoku, reversi and doudizhu.

The game engine lives in ``engine/`` (host-independent). This module only wires
KiraAI extension points:

- ``@register.page`` / ``@register.api`` for the lobby and match pages;
- ``@register.tool`` for the four chat tools;
- ``@on.llm_request`` for per-turn board context and request-scoped turn tools;
- ``@on.im_message``-free notice-driven turns through ``ctx.publish_notice``.

The runtime (rooms, table talk, judge, turn director) is assembled in
``host/runtime.py``; the dedicated ``gamehub:dm:admin`` session is provisioned
here because adapter registration resolves the calling plugin module.
"""

from __future__ import annotations

from typing import Any

from core.plugin import BasePlugin, PageMenu, PluginPage, Priority, on, register, logger

from . import modes
from . import tools as game_tools
from .host.settings import Settings

PLUGIN_ID = "kira-plugin-gamehub"

# Defaults mirrored from schema.json. Used only when the config form has not
# been persisted yet, so initialization never crashes on a missing key.
_DEFAULT_AGENT_NAME = "nori"
_DEFAULT_AGENT_PERSONA = "调皮爱吐槽的猫娘，玩游戏时会认真+毒舌"
_DEFAULT_AI_MODE = "agent"

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

GAME_NEW_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": "gomoku（五子棋，默认）、reversi（黑白棋）、doudizhu（斗地主）",
        },
        "mode": {
            "type": "string",
            "enum": [modes.MODE_NORI, modes.MODE_BOT],
            "description": "nori=与宿主 AI 对战（默认，由通知回合驱动）；bot=规则陪练",
        },
        "role": {
            "type": "string",
            "enum": [modes.ROLE_OPPONENT, modes.ROLE_TEAMMATE],
            "description": "opponent=对手；teammate=宿主 AI 做你的队友（可代打）",
        },
        "opponent": {
            "type": "string",
            "enum": ["ai", "bot", "nori"],
            "description": "ai/nori=宿主 AI 本人（默认）；bot=规则陪练",
        },
        "difficulty": {
            "type": "string",
            "enum": ["easy", "advanced"],
            "description": "仅 opponent=bot 时有效：easy=规则陪练；advanced=game_agent 作为对手",
        },
        "name": {"type": "string", "description": "用户在这一局的显示名（可选）"},
        "lineup": {
            "type": "string",
            "enum": list(modes.LINEUPS),
            "description": "斗地主阵容：nori_agent（默认）/nori_bot/agent_bot/bot_bot",
        },
        "hands": {"type": "integer", "enum": [1, 3, 6], "description": "斗地主局数，默认 1"},
        "level": {
            "type": "string",
            "enum": ["beginner", "intermediate", "advanced"],
            "description": "五子棋/黑白棋难度：初级无分析，中级带坐标分析（默认），高级另加推荐",
        },
    },
    "additionalProperties": False,
}

GAME_STATE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match_id": {
            "type": "string",
            "description": "对局 ID（可选；省略时查看当前会话的对局）",
        },
    },
    "additionalProperties": False,
}

GAME_RESIGN_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match_id": {
            "type": "string",
            "description": "对局 ID（可选；省略时认输当前会话中 AI 自己的座位）",
        },
    },
    "additionalProperties": False,
}


class GamehubPlugin(BasePlugin):
    """Game Hub entry point: lobby page in the WebUI, matches in chat."""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self._agent_name = _DEFAULT_AGENT_NAME
        self._agent_persona = _DEFAULT_AGENT_PERSONA
        self._ai_mode = _DEFAULT_AI_MODE
        self._ai_models: list[str] = []
        self.settings = Settings(cfg)
        self.data_dir = None
        self.runtime = None
        # Set only after a successful runtime start; the hooks below read it
        # via getattr, so None (runtime failed to start) degrades safely.
        self._director = None
        self._rooms = None
        self._api = None
        self._adapter_ready = False
        self._ready = False

    @property
    def rooms(self):
        """RoomManager exposed for the ported tools/context/director modules."""
        return self._rooms

    async def initialize(self) -> None:
        """Read config, provision the internal session and start the runtime."""
        cfg: dict[str, Any] = self.plugin_cfg if isinstance(self.plugin_cfg, dict) else {}
        self.settings = Settings(cfg)
        self.data_dir = self.ctx.get_plugin_data_dir()

        # Section fields are persisted as nested dicts; plain fields sit at the
        # top level. Read both shapes so a hand-edited config still loads.
        identity = cfg.get("section_identity")
        identity = identity if isinstance(identity, dict) else {}
        ai = cfg.get("section_ai")
        ai = ai if isinstance(ai, dict) else {}

        agent_name = identity.get("agent_name", cfg.get("agent_name", _DEFAULT_AGENT_NAME))
        self._agent_name = agent_name.strip() if isinstance(agent_name, str) and agent_name.strip() else _DEFAULT_AGENT_NAME

        persona = identity.get("agent_persona", cfg.get("agent_persona", _DEFAULT_AGENT_PERSONA))
        self._agent_persona = persona if isinstance(persona, str) else _DEFAULT_AGENT_PERSONA

        ai_mode = ai.get("ai_mode", cfg.get("ai_mode", _DEFAULT_AI_MODE))
        self._ai_mode = ai_mode if ai_mode in ("agent", "bot") else _DEFAULT_AI_MODE

        models = ai.get("ai_models", cfg.get("ai_models", []))
        self._ai_models = [str(m) for m in models if m] if isinstance(models, list) else []

        await self._provision_adapter()
        await self._start_runtime()

        self._ready = True
        logger.info(
            f"{PLUGIN_ID} loaded: agent={self._agent_name}, "
            f"mode={self._ai_mode}, models={len(self._ai_models)}, "
            f"adapter={self._adapter_ready}"
        )

    async def _provision_adapter(self) -> None:
        """Register the internal gamehub adapter and create its session.

        Adapter provisioning is fail-soft: chat tools still work against the
        caller's session if the internal session cannot be created, and the
        failure is visible in the log.
        """
        from .host.session import GAME_ADAPTER_NAME, adapter_info, ensure_game_session

        adapter_mgr = self.ctx.adapter_mgr
        existing = adapter_mgr.get_adapter_class(GAME_ADAPTER_NAME)
        if existing is not None:
            # A reload leaves the previous class registered under the same
            # platform; drop it so the freshly imported class is used.
            try:
                await adapter_mgr.stop_adapter_instances_by_platform(GAME_ADAPTER_NAME)
                adapter_mgr.unregister_adapter_type(GAME_ADAPTER_NAME, existing)
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                logger.warning(f"{PLUGIN_ID}: stale adapter cleanup failed: {e}")

        try:
            await self.ctx.register_adapter("./adapter")
            await adapter_mgr.register_adapter(adapter_info())
            await ensure_game_session(self.ctx)
            self._adapter_ready = True
        except Exception as e:  # noqa: BLE001 - keep the plugin loadable
            self._adapter_ready = False
            logger.error(f"{PLUGIN_ID}: failed to provision game adapter/session: {e}")

    async def _start_runtime(self) -> None:
        """Assemble rooms/talk/judge/director; fail-soft like the adapter."""
        runtime = None
        try:
            from .host.runtime import GamehubRuntime

            runtime = GamehubRuntime(self)
            await runtime.start()
            self.runtime = runtime
            self._rooms = runtime.rooms
        except Exception as e:  # noqa: BLE001 - a broken runtime must not block boot
            self.runtime = None
            self._rooms = None
            if runtime is not None:
                # A partially started runtime may already hold the SQLite
                # engine; dispose it so nothing keeps a second writer away
                # (the WebUI chat endpoints lazily open their own store).
                try:
                    await runtime.stop()
                except Exception:  # noqa: BLE001 - cleanup best effort
                    pass
            logger.error(f"{PLUGIN_ID}: runtime start failed: {e}", exc_info=True)

    async def terminate(self) -> None:
        """Stop accepting new work and release resources."""
        self._ready = False
        if self.runtime is not None:
            try:
                await self.runtime.stop()
            except Exception as e:  # noqa: BLE001 - teardown must not raise
                logger.warning(f"{PLUGIN_ID}: runtime stop failed: {e}")
            self.runtime = None
        self._rooms = None

        from .host.session import GAME_ADAPTER_NAME

        try:
            await self.ctx.unregister_adapter(GAME_ADAPTER_NAME)
        except Exception as e:  # noqa: BLE001 - teardown must not raise
            logger.warning(f"{PLUGIN_ID}: failed to unregister game adapter: {e}")
        self._adapter_ready = False
        logger.info(f"{PLUGIN_ID} terminated")

    # ------------------------------------------------------------------
    #  Chat tools
    # ------------------------------------------------------------------

    @register.tool(
        name="game_list",
        description=(
            "列出可玩的游戏（五子棋、黑白棋、斗地主）与走子写法。"
            "用户问「能玩什么」「有哪些游戏」「怎么走子」时使用。"
        ),
        params=_EMPTY_PARAMS,
    )
    async def tool_game_list(self, event, **kwargs):
        return await game_tools.game_list(self, event, **kwargs)

    @register.tool(
        name="game_new",
        description=(
            "开始一局游戏（五子棋、黑白棋、斗地主）。用户说「来一局」「下棋」「斗地主」"
            "时使用。默认五子棋，也可用 kind=reversi 开黑白棋。默认与当前宿主 AI 对战"
            "（她的走子由行动通知回合驱动）；opponent=bot 是规则陪练；"
            "difficulty=advanced 会让插件 game_agent 作为对手。同一时间只能有一局未结束；"
            "若已有对局会直接返回该局，不会新建。"
        ),
        params=GAME_NEW_PARAMS,
    )
    async def tool_game_new(self, event, **kwargs):
        return await game_tools.game_new(self, event, **kwargs)

    @register.tool(
        name="game_state",
        description=(
            "查看当前对局的完整局面：棋盘/手牌、已发生落子、轮到谁、你的执色与可用走法。"
            "用户问「现在什么局面」「轮到谁了」时使用；回答具体落点或复盘前先调用本工具，"
            "不要凭聊天历史猜测盘面。"
        ),
        params=GAME_STATE_PARAMS,
    )
    async def tool_game_state(self, event, **kwargs):
        return await game_tools.game_state(self, event, **kwargs)

    @register.tool(
        name="game_resign",
        description=(
            "让宿主 AI 认输并结束当前对局（只作用于她本人的座位，不会代用户或陪练认输；"
            "队友模式下她没有自己的座位，会明确拒绝）。只有确实想认输时才调用。"
        ),
        params=GAME_RESIGN_PARAMS,
    )
    async def tool_game_resign(self, event, **kwargs):
        return await game_tools.game_resign(self, event, **kwargs)

    # ------------------------------------------------------------------
    #  Per-turn injection
    # ------------------------------------------------------------------

    @on.llm_request(priority=Priority.MEDIUM)
    async def inject_game_context(self, event, request, tag_set=None):
        """Inject the board block and request-scoped turn tools."""
        from .host.prompt import inject_into_request

        inject_into_request(self, event, request, tag_set)
        # F3: the run is now open for this session; until its first response
        # lands, the turn watchdog must treat the session as busy.
        sid = str(getattr(event, "sid", "") or "")
        if sid:
            director = self._director
            marker = getattr(director, "note_llm_request", None)
            if callable(marker):
                marker(sid)

    @on.llm_response(priority=Priority.LOW)
    async def note_turn_llm_response(self, event, response=None, *args, **kwargs):
        """A model step finished: the notice turn is making real progress.

        ``ON_STEP_RESULT`` only fires after a text delivery, so a tool-only
        step would otherwise look like silence. A *completed* response also
        feeds F1: if the reply came back text-only, the director sends a
        corrective nudge with a shortened deadline instead of idling out the
        full silence window.
        """
        director = self._director
        sid = str(getattr(event, "sid", "") or "")
        if not sid:
            return
        responder = getattr(director, "note_response", None)
        if callable(responder):
            responder(sid)
            return
        note = getattr(director, "note_progress", None)
        if callable(note):
            note(sid)

    @on.step_result(priority=Priority.LOW)
    async def note_turn_progress(self, event, step_result=None, *args, **kwargs):
        """Tell the turn director the host is still working on the notice turn.

        Without this, a slow model or a multi-step turn can look "silent" to the
        turn watchdog, which then overwrites the host's move with a rule-bot
        move. The hook is a cheap no-op when no grant is live for the session.
        """
        director = self._director
        note = getattr(director, "note_progress", None)
        if not callable(note):
            return
        sid = str(getattr(event, "sid", "") or "")
        if sid:
            note(sid)

    @on.tool_result(priority=Priority.LOW)
    async def note_turn_tool_progress(self, event, *args, **kwargs):
        """Tool stage: real tool activity (progress + the F1 arming guard).

        Marks both channels: the silence window (a running tool is progress)
        and the tool-activity timestamp that stands a pending corrective
        nudge down (a mid multi-step turn is not "only talking").
        """
        director = self._director
        sid = str(getattr(event, "sid", "") or "")
        if not sid:
            return
        note = getattr(director, "note_progress", None)
        if callable(note):
            note(sid)
        tool_note = getattr(director, "note_tool_progress", None)
        if callable(tool_note):
            tool_note(sid)

    # ------------------------------------------------------------------
    #  WebUI
    # ------------------------------------------------------------------

    @register.page(
        "/hub",
        auth=True,
        menu=PageMenu(
            label={"zh": "游戏中心", "en": "Game Hub"},
            icon="Collection",
            order=90,
        ),
    )
    def hub_page(self):
        return PluginPage.from_folder("./web")

    @register.api(method="GET", path="/ping")
    async def ping(self):
        return {
            "ok": True,
            "plugin": PLUGIN_ID,
            "ready": self._ready,
            "adapter_ready": self._adapter_ready,
            "agent_name": self._agent_name,
        }

    # ------------------------------------------------------------------
    #  WebUI REST/WS
    #
    #  The logic lives in ``host/web.py`` so it stays unit-testable; these
    #  wrappers only translate between FastAPI and the plugin API.
    # ------------------------------------------------------------------

    @property
    def api(self):
        """Lazily built REST/WS assembler (bound to this plugin instance)."""
        if self._api is None:
            from .host.web import GamehubApi

            self._api = GamehubApi(self)
        return self._api

    async def _call(self, coro) -> Any:
        """Await an API coroutine and render failures as an error envelope."""
        from .host.web import ApiError, error_envelope

        try:
            return await coro
        except ApiError as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=exc.status, content=error_envelope(exc))
        except Exception as e:  # noqa: BLE001 - never leak a 500 stack trace
            logger.error(f"{PLUGIN_ID}: api failure: {e}", exc_info=True)
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=500,
                content={"ok": False, "data": None, "error": "插件内部错误", "code": "internal"},
            )

    @register.api(method="GET", path="/page-session")
    async def api_page_session(self):
        return await self._call(self.api.page_session())

    @register.api(method="GET", path="/identity")
    async def api_get_identity(self):
        return await self._call(self.api.get_identity())

    @register.api(method="POST", path="/identity")
    async def api_set_identity(self, body: dict[str, Any] | None = None):
        return await self._call(self.api.set_identity(body))

    @register.api(method="GET", path="/ui-config")
    async def api_ui_config(self, match_id: str = ""):
        return await self._call(self.api.ui_config(match_id))

    @register.api(method="POST", path="/notice-test")
    async def api_notice_test(self):
        return await self._call(self.api.notice_test())

    @register.api(method="GET", path="/games")
    async def api_games(self):
        return await self._call(self.api.games())

    @register.api(method="POST", path="/matches")
    async def api_create_match(self, body: dict[str, Any] | None = None):
        return await self._call(self.api.create_match(body))

    @register.api(method="GET", path="/matches/{match_id}")
    async def api_get_match(self, match_id: str, t: str = ""):
        # The seat token travels as ``?t=`` (never in the Authorization header).
        return await self._call(self.api.get_match(match_id, t))

    @register.api(method="GET", path="/matches/{match_id}/control")
    async def api_control(self, match_id: str, seat: int | None = None):
        return await self._call(self.api.control(match_id, seat))

    @register.api(method="POST", path="/matches/{match_id}/actions")
    async def api_actions(
        self,
        match_id: str,
        t: str = "",
        body: dict[str, Any] | None = None,
    ):
        return await self._call(self.api.actions(match_id, t, body))

    @register.api(method="GET", path="/matches/{match_id}/mode")
    async def api_get_mode(self, match_id: str):
        return await self._call(self.api.get_mode(match_id))

    @register.api(method="POST", path="/matches/{match_id}/mode")
    async def api_set_mode(self, match_id: str, body: dict[str, Any] | None = None):
        return await self._call(self.api.set_mode(match_id, body))

    @register.api(method="POST", path="/matches/{match_id}/delegation")
    async def api_set_delegation(
        self, match_id: str, body: dict[str, Any] | None = None
    ):
        return await self._call(self.api.set_delegation(match_id, body))

    @register.api(method="POST", path="/agent-avatar")
    async def api_set_avatar(self, body: dict[str, Any] | None = None):
        """Upload (or reset) the ``game_agent`` avatar."""
        return await self._call(self.api.set_avatar(body))

    @register.api(method="GET", path="/agent-avatar")
    async def api_get_avatar(self):
        """Serve the effective ``game_agent`` avatar image."""
        from fastapi.responses import FileResponse

        from .host.avatar import avatar_path

        path = avatar_path(self)
        if path is None or not path.is_file():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="头像不存在")
        return FileResponse(str(path))

    @register.api(method="GET", path="/host-avatar")
    async def api_get_host_avatar(self):
        """Serve the host agent's avatar (active persona selfie reference).

        404 when the active persona has no reference image — the front end
        then renders the seat's name initial rather than another identity's
        picture.
        """
        from fastapi.responses import FileResponse

        from .host.avatar import host_avatar_path

        path = await host_avatar_path(self)
        if path is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="宿主没有自拍参考图")
        return FileResponse(str(path))

    @register.api(method="GET", path="/chat")
    async def api_chat_history(self, limit: int = 100):
        """Match-page chat window: recent lines + enabled flag."""
        return await self._call(self.api.chat_history(limit))

    @register.api(method="POST", path="/chat")
    async def api_chat_send(self, body: dict[str, Any] | None = None):
        """Match-page chat window: publish one player line to the game session."""
        return await self._call(self.api.chat_send(body))

    @register.ws(path="/matches/{match_id}/ws")
    async def match_ws(self, websocket):
        """Live match channel. The handler only reads the seat token ``?t=``; the host JWT travels in ``?token=`` and is checked by the host framework during the WS handshake (``auth=True``)."""
        from .host.web import serve_ws

        match_id = str(websocket.path_params.get("match_id") or "")
        token = str(websocket.query_params.get("t") or "")
        try:
            await websocket.accept()
        except Exception as e:  # noqa: BLE001 - the client may have vanished
            logger.warning(f"{PLUGIN_ID}: websocket accept failed: {e}")
            return
        await serve_ws(self, websocket, match_id, token)
