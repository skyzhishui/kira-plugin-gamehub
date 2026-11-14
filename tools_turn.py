"""Request-scoped tools for a notice-driven turn: ``play_<match>`` + ``state_<match>``.

KiraAI has no ``ctx.session_tools``; a turn grant is expressed as a
``BaseTool`` pair added to ``req.tool_set`` for one request only
(``main.py``'s ``@on.llm_request`` handler calls
``req.tool_set.add(*build_turn_tools(plugin, grant))``).

- ``play_<match_id>`` — the actual move of this turn. Bound to one grant
  (match / seat / grant key), never registered globally, never reused across
  turns. Parameters are ``move`` (required) plus an optional short ``say``
  line (truncated to ``settings.max_say_len``). A successful move calls
  ``event.stop()`` so the same reply does not repeat itself (the finalize
  stage still saves memory) and returns a short confirmation; an illegal move
  or a stale/revoked grant always reports failure.
- ``state_<match_id>`` — read-only seat-scoped view of the bound match.

Discipline:

- tools are built per turn (grant captured at construction time) and dropped
  by simply not adding them to the next request;
- ``event.extra["gamehub_turn"]`` is the bound context key
  (``{"match_id", "seat", "kind", "grant"}``), produced by
  :func:`build_turn_context`;
- the move is applied through ``plugin._director.submit_play`` when the turn
  director is present (it owns the grant lifecycle) and otherwise through the
  room layer (``apply_ai_move`` / ``apply_delegated_move``) with the same
  seat/version checks, so a stale grant can never take a later turn.
"""

from __future__ import annotations

from typing import Any

try:  # production: plugin package (``<plugin>.tools_turn``)
    from .tools import BaseTool, GameStateTool, NOTICE_ACTOR, TURN_KEY
except ImportError:  # standalone tests import this module as top-level
    from tools import BaseTool, GameStateTool, NOTICE_ACTOR, TURN_KEY

_LEGACY_SUCCESS_PREFIXES = ("已落子", "已执行动作", "已完成动作")
_NO_GRANT = "本回合没有有效的落子授权（可能已被停止或已结束）。"


def build_turn_context(grant: Any) -> dict[str, Any]:
    """Bound turn context injected as ``event.extra["gamehub_turn"]``."""
    return {
        "match_id": str(grant.match_id),
        "seat": int(grant.seat),
        "kind": str(grant.kind),
        "grant": str(grant.key),
    }


def _module_of(match_kind: str) -> Any:
    if not match_kind:
        return None
    try:
        try:
            from .engine import games as _games
        except ImportError:
            from engine import games as _games

        return _games.get(match_kind)
    except Exception:  # noqa: BLE001 - unknown game falls back to a generic def
        return None


def _safe_id(match_id: str) -> str:
    return "".join(ch for ch in str(match_id or "") if ch.isalnum())[:24]


def play_tool_name(match_id: str) -> str:
    """Turn-scoped move tool name: ``play_<match_id>`` (per-match disambiguation).

    Several matches may share one target session, so the name carries the
    match id: a bare ``play`` would silently replace another match's tool in
    the request tool set. Match ids are hex, hence safe as a suffix.
    """
    return f"play_{_safe_id(match_id) or 'turn'}"


def state_tool_name(match_id: str) -> str:
    return f"state_{_safe_id(match_id) or 'turn'}"


def _play_description(move_help: str) -> str:
    if move_help:
        return (
            "在当前对局执行一步。move 的格式由本局规则决定"
            f"（{move_help}），可配合文本输出一起使用。"
        )
    return "在当前对局执行一步。move 的格式由本局规则决定，可配合文本输出一起使用。"


def _play_parameters(move_help: str) -> dict[str, Any]:
    move_description = (
        f"游戏动作字符串；本局格式：{move_help}。"
        if move_help
        else "游戏动作字符串；具体格式以本局规则/move_help 为准。"
    )
    return {
        "type": "object",
        "properties": {
            "move": {"type": "string", "description": move_description},
            "say": {
                "type": "string",
                "description": "可选：本轮同回合说的一句短话（超长会被截断）。",
            },
        },
        "required": ["move"],
        "additionalProperties": False,
    }


def _truncate_say(plugin: Any, say: Any) -> str:
    """Cut the optional table-talk line to the configured limit."""
    text = str(say or "").strip()
    if not text:
        return ""
    settings = getattr(plugin, "settings", None)
    try:
        limit = int(getattr(settings, "max_say_len", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    return text[:limit] if limit > 0 else text


def _applied_move(text: str, fallback: str) -> str:
    """Read the actually applied move out of ``已落子 X。...`` (else fallback)."""
    body = str(text or "").strip()
    for prefix in _LEGACY_SUCCESS_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):].lstrip()
            break
    else:
        return fallback
    for sep in ("。", "，", " ", "\n"):
        index = body.find(sep)
        if index > 0:
            body = body[:index]
            break
    body = body.strip().rstrip("。")
    return body or fallback


def _parse_result(result: Any, fallback_move: str) -> tuple[str, bool, str, str]:
    """Normalise a director result to ``(text, ok, applied_move, label)``.

    Accepts both the legacy ``(text, terminal)`` tuple and the typed result
    (``ok`` / ``status`` / ``text`` / ``error`` / ``move``). Success is only
    reported from an explicit ok/status or a legacy success prefix — never
    guessed from arbitrary text.
    """
    if isinstance(result, tuple):
        text = str(result[0] if result else "")
        terminal = bool(result[1]) if len(result) > 1 else False
        label = next(
            (p for p in _LEGACY_SUCCESS_PREFIXES if text.startswith(p)), "已落子"
        )
        ok = terminal and text.startswith(_LEGACY_SUCCESS_PREFIXES)
        applied = _applied_move(text, fallback_move) if ok else fallback_move
        return text, ok, applied, label

    ok = bool(getattr(result, "ok", False))
    status = str(getattr(result, "status", "") or "")
    text = str(getattr(result, "text", "") or getattr(result, "error", "") or "")
    applied = str(getattr(result, "move", "") or "") or fallback_move
    return text, bool(ok and status in ("", "played")), applied, "已落子"


def _finish_turn(event: Any, text: str) -> str:
    """Stop the current reply after a successful move (memory is still saved)."""
    stop = getattr(event, "stop", None)
    if callable(stop):
        stop()
    return text


class PlayTool(BaseTool):
    """``play_<match_id>``: submit this turn's move for the bound grant."""

    def __init__(
        self,
        plugin: Any,
        grant: Any,
        *,
        match_kind: str = "",
        ctx: Any = None,
    ) -> None:
        super().__init__(ctx=ctx)
        self._plugin = plugin
        self._grant = grant
        self._bound = build_turn_context(grant)
        kind = (
            match_kind
            or str(getattr(grant, "match_kind", "") or "")
            or str(getattr(grant, "kind", "") or "")
        )
        module = _module_of(kind)
        move_help = str(getattr(module, "move_help", "") or "").strip()
        self.name = play_tool_name(grant.match_id)
        self.description = _play_description(move_help)
        self.parameters = _play_parameters(move_help)

    def get_schema(self) -> dict[str, Any]:
        """Per-instance schema: the tool name carries the match id.

        ``BaseTool.get_schema`` is a classmethod reading class attributes, but
        turn tools are bound per grant, so the instance values must win — the
        host builds ``request.tools`` from ``ToolSet.to_list()``.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    # -- validation ----------------------------------------------------
    def _bound_of(self, event: Any) -> dict[str, Any] | None:
        """Bound context of this call; must still match the captured grant."""
        bound = None
        extra = getattr(event, "extra", None)
        if isinstance(extra, dict):
            candidate = extra.get(TURN_KEY)
            if isinstance(candidate, dict):
                bound = candidate
        if bound is None:
            bound = self._bound
        if not bound:
            return None
        if str(bound.get("match_id") or "") != str(self._grant.match_id):
            return None
        if str(bound.get("grant") or "") != str(self._grant.key):
            return None
        return bound

    async def _via_rooms(
        self, bound: dict[str, Any], move: str, say: str
    ) -> tuple[str, bool]:
        """Fallback path: apply through the room layer with grant checks.

        Used when no turn director is attached (e.g. the watchdog-free setup):
        seat kind, waiting seat and move count are validated in the room lock,
        so a stale or revoked grant cannot slip a move into a later turn.
        """
        rooms = self._plugin.rooms
        match_id = str(bound.get("match_id") or "")
        loader = getattr(rooms, "get_or_load", None)
        room = await loader(match_id) if callable(loader) else None
        if room is None:
            return "通知回合绑定的对局不存在或已结束。", False
        match = room.match
        seat = int(bound.get("seat") or -1)
        if not 0 <= seat < len(match.seats):
            return "没有这个座位。", False
        delegated = str(bound.get("kind") or "") == "delegated_human"
        expect_kind = "human" if delegated else "ai"
        if match.seats[seat].kind != expect_kind:
            return "本回合的行动座位与对局不一致，本手作废。", False
        expected = getattr(self._grant, "move_count", None)
        try:
            expected = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected = None
        if delegated:
            applied_match, error = await room.apply_delegated_move(
                seat,
                move,
                expected_move_count=expected,
                grant_key=str(bound.get("grant") or ""),
                say=say or None,
            )
        else:
            applied_match, error = await room.apply_ai_move(
                seat,
                move,
                expected_move_count=expected,
                say=say or None,
            )
        if applied_match is None:
            return f"落子 {move} 失败：{error}", False
        return f"已落子 {move}。", True

    # -- execution -----------------------------------------------------
    async def execute(
        self, event: Any = None, move: str = "", say: str = "", **kwargs: Any
    ) -> str:
        bound = self._bound_of(event)
        if bound is None:
            return _NO_GRANT
        move = str(move or "").strip()
        if not move:
            return "没有写这步棋。"
        # A hallucinated extra say from older prompts must never break the move.
        say = _truncate_say(self._plugin, say)
        match_id = str(bound.get("match_id") or "")
        grant_key = str(bound.get("grant") or "")

        director = getattr(self._plugin, "_director", None)
        submit = getattr(director, "submit_play", None)
        if callable(submit):
            if say:
                result = await submit(match_id, grant_key, move, say)
            else:
                result = await submit(match_id, grant_key, move)
            text, ok, applied, label = _parse_result(result, move)
        else:
            text, ok = await self._via_rooms(bound, move, say)
            applied, label = move, "已落子"

        if not ok:
            return text or "这步没有走成。"
        return _finish_turn(
            event,
            f"{label} {applied}。本次行动授权已完成，"
            "请勿重复调用本局落子或状态工具。",
        )


class BoundStateTool(GameStateTool):
    """``state_<match_id>``: seat-scoped view bound to one grant.

    The match id keeps two concurrent matches in one session from replacing
    each other's tool (same reasoning as ``play_<match_id>``).
    """

    def __init__(self, plugin: Any, grant: Any, *, ctx: Any = None) -> None:
        bound = build_turn_context(grant)
        super().__init__(plugin, bound=bound, ctx=ctx)
        self.name = state_tool_name(str(bound.get("match_id") or ""))

    def get_schema(self) -> dict[str, Any]:
        """Per-instance schema (the bound name differs from ``game_state``)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    async def execute(
        self, event: Any = None, match_id: str | None = None, **kwargs: Any
    ) -> str:
        # Bound view: the grant decides the seat, the caller cannot retarget it.
        return await super().execute(event, match_id, **kwargs)


def build_turn_tools(
    plugin: Any, grant: Any, *, match_kind: str = ""
) -> list[BaseTool]:
    """Tools visible for one notice turn: ``[play_<id>, state_<id>]``.

    ``match_kind`` only refines the ``play`` schema (move format of the bound
    game); omitting it keeps a generic move description.
    """
    return [
        PlayTool(plugin, grant, match_kind=match_kind),
        BoundStateTool(plugin, grant),
    ]


__all__ = [
    "NOTICE_ACTOR",
    "BoundStateTool",
    "PlayTool",
    "build_turn_context",
    "build_turn_tools",
    "play_tool_name",
    "state_tool_name",
]