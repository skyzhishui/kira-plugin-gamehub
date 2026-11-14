"""Chat-facing game tools: list / new / state / resign (KiraAI port).

Four plain coroutines are the whole global tool surface and ``main.py``
decorates them with ``@register.tool``::

    game_list(plugin, event, **kwargs) -> str
    game_new(plugin, event, kind, mode=None, role=None, difficulty=None,
             opponent=None, **kwargs) -> str
    game_state(plugin, event, match_id=None, **kwargs) -> str
    game_resign(plugin, event, match_id=None, **kwargs) -> str

``GameStateTool`` keeps the ``BaseTool`` shape because ``tools_turn.py``
binds the same logic per turn (``state_<match_id>``) through
``ToolSet``/``req.tool_set``.

``game_plan`` is intentionally absent (the "write a plan" behaviour is
deferred): the old tool would write strategy into ``kind == "ai"`` seats,
which are the *opponent* seat in teammate matches. Historic plan data stays
readable (``plan.py`` helpers are still used for display only).

``game_move`` / ``play`` are not global tools: human moves go through the
page/WS, and the notice-turn ``play`` is a request-scoped ``BaseTool`` built
by ``tools_turn.py`` (bound to one seat/grant, never registered globally).

Conventions carried over from nori:

- every mutation goes through ``RoomManager``; the engine is never touched
  directly;
- seats are resolved by the session id (``actor``), one active match per
  actor;
- **notice-turn safety**: a synthetic notice message (``is_notice``) or a
  bound ``gamehub_turn`` context disables ``game_new`` / ``game_resign`` and
  answers ``game_state`` explicitly instead of doing an actor lookup that
  could hit somebody else's match;
- no hard-coded game rules: seat labels / describe / rules / move help come
  from the game module.
"""

from __future__ import annotations

import time
from typing import Any

try:  # production: KiraAI host primitive
    from core.utils.tool_utils import BaseTool
except ImportError:  # standalone tests outside the host: minimal equivalent
    from abc import ABC, abstractmethod

    class BaseTool(ABC):  # type: ignore[no-redef]
        def __init__(self, *args, ctx=None, **kwargs):
            self.ctx = ctx
            self.args = args
            self.kwargs = kwargs

        name = None
        description = None
        parameters = None

        @abstractmethod
        async def execute(self, *args, **kwargs) -> str:
            ...

        @classmethod
        def get_schema(cls) -> dict[str, Any]:
            return {
                "name": cls.name,
                "description": cls.description,
                "parameters": cls.parameters,
            }


try:  # production: plugin package (``<plugin>.tools``)
    from . import modes
    from .engine import describe_match, games, status_of, view_match
    from .engine.match import SOURCE_CHAT
    from .modes import ROLE_TEAMMATE, bound_seat, role_of
    from .plan import (
        ai_seat_index,
        normalize_plan_text,
        plan_move_count,
        plan_summary,
    )
    from .targets import session_type_of
except ImportError:  # standalone tests import this module as top-level
    import modes
    from engine import describe_match, games, status_of, view_match
    from engine.match import SOURCE_CHAT
    from modes import ROLE_TEAMMATE, bound_seat, role_of
    from plan import ai_seat_index, normalize_plan_text, plan_move_count, plan_summary
    from targets import session_type_of

# Synthetic trigger of notice messages (talk / notice_judge / turn tools share
# this spelling). Kept for parity with nori even though KiraAI notices are
# detected through ``message.is_notice`` first.
NOTICE_ACTOR = "nori_notice"

# Key of the bound turn context injected into ``event.extra`` for the current
# request (see ``tools_turn.build_turn_context``).
TURN_KEY = "gamehub_turn"

# KiraAI plugin id; the match page lives under the plugin's own page route.
PLUGIN_ID = "kira-plugin-gamehub"
MATCH_PAGE_PATH = f"page/plugin/{PLUGIN_ID}/hub/match.html"

GAME_STATE_DESCRIPTION = (
    "查看当前对局的完整局面：完整盘面/棋盘、已发生落子与最近走子、"
    "轮到谁、你的执色与可用走法，以及当前打法（plan）与剩余 TTL。"
    "用户问「现在什么局面」「轮到谁了」时使用；回答具体落点、"
    "复盘或任何棋子坐标前，先调用 game_state 读取当前完整局面，"
    "不要凭聊天历史或最近几手猜测盘面。返回的是当前对局状态，"
    "具体坐标规则与可用走法以结果中的游戏说明为准。"
    "结果中的走子协议以 play 工具为准，不使用旧的文本 MOVE 行。"
)

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


# ----------------------------------------------------------------------
#  Small helpers (kept from nori)
# ----------------------------------------------------------------------
def _now() -> int:
    """Match timestamps use epoch milliseconds (same unit as engine/room)."""
    return int(time.time() * 1000)


def _is_private_session(session_id: str) -> bool:
    """Private-chat test reuses the single ``targets`` convention."""
    return session_type_of(session_id) == "private"


def _bot_name(plugin: Any) -> str:
    """Host agent display name; neutral fallback, never hard-coded."""
    ctx = getattr(plugin, "ctx", None)
    bot_config = getattr(ctx, "bot_config", None)
    bot = getattr(bot_config, "bot", None)
    settings = getattr(plugin, "settings", None)
    for candidate in (
        getattr(plugin, "host_agent_name", ""),
        getattr(bot, "nickname", ""),
        getattr(plugin, "bot_nickname", ""),
        getattr(plugin, "_agent_name", ""),
        getattr(settings, "agent_name", ""),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return "对手"


def _opponent_name(match: Any) -> str:
    """Display name of the AI/opponent seat; no persona name is hard-coded."""
    seat = ai_seat_index(match)
    if seat is None:
        return "对手"
    name = str(getattr(match.seats[seat], "name", "") or "").strip()
    return name or "对手"


def _agent_configured_name(plugin: Any) -> str:
    """Configured ``game_agent`` name (default ``nori``); never a persona guess."""
    settings = getattr(plugin, "settings", None)
    return str(getattr(settings, "agent_name", "") or "").strip() or "nori"


def _view_text(match: Any, seat: int | None) -> str:
    """Board view for a tool result, without the retired ``MOVE:`` lines."""
    if seat is None:
        return _spectator_text(match)
    text = describe_match(match, seat)
    return "\n".join(line for line in text.splitlines() if "MOVE:" not in line)


def _seat_of_actor(match: Any, actor: str) -> int | None:
    for index, seat in enumerate(match.seats):
        if seat.actor and seat.actor == actor:
            return index
    return None


def _human_seat_of(match: Any) -> int | None:
    for index, seat in enumerate(match.seats):
        if seat.kind == "human":
            return index
    return None


def _tempo(match: Any) -> str:
    try:
        return str(games.get(match.kind).tempo or "turn")
    except KeyError:
        return "turn"


def _plan_guidance(match: Any, seat: int | None) -> str:
    """Dynamic strategy guidance of the game module (optional hook)."""
    if seat is None:
        return ""
    try:
        module = games.get(match.kind)
    except KeyError:
        return ""
    prompt = getattr(module, "plan_prompt", None)
    if not callable(prompt):
        return ""
    try:
        return str(prompt(match.state, seat) or "").strip()
    except Exception:  # noqa: BLE001 - guidance is advisory only
        return ""


def _plan_lines(match: Any, *, role: str = "opponent") -> list[str]:
    """Current plan summary per seat (expired plans are shown but flagged).

    Teammate matches return nothing: a leftover plan on a seat belongs to the
    *opponent* there, so it must not be presented as the bot's own strategy.
    """
    if role == ROLE_TEAMMATE:
        return []
    lines: list[str] = []
    tempo = _tempo(match)
    now = _now()
    move_count = plan_move_count(match)
    for index, seat in enumerate(match.seats):
        plan = getattr(seat, "plan", None)
        if not isinstance(plan, dict) or not normalize_plan_text(plan.get("text")):
            continue
        label = seat.name or f"座位 {index}"
        lines.append(
            plan_summary(
                plan,
                tempo=tempo,
                move_count=move_count,
                now=now,
                label=label,
            )
        )
    return lines


def _spectator_text(match: Any) -> str:
    if match.kind == "doudizhu":
        return games.get(match.kind).describe(
            match.state, None, [s.name for s in match.seats]
        )
    view = view_match(match, None)
    status = view["status"]
    lines = [
        f"对局 {match.id}（{games.get(match.kind).name['zh']}）",
        "座位：" + "、".join(seat["name"] for seat in view["seats"]),
        f"最近走子：{len(match.log)} 手",
    ]
    if status["resultText"]:
        lines.append(f"已结束：{status['resultText']}")
    elif status["waitingOn"]:
        lines.append(
            "等待：" + "、".join(view["seats"][s]["name"] for s in status["waitingOn"])
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
#  Event / turn-context resolution
# ----------------------------------------------------------------------
def _event_sid(event: Any) -> str:
    """Current session id (the KiraAI actor key)."""
    try:
        return str(getattr(event, "sid", "") or "").strip()
    except Exception:  # noqa: BLE001 - malformed fake events fall back to ""
        return ""


def _resolve(event: Any) -> tuple[str, str]:
    """Resolve ``(actor, session_id)`` from the request event.

    KiraAI has no per-message user id in the tool signature: the session id is
    the actor key (one active match per session). Page/admin matches use the
    dedicated ``gamehub:dm:admin`` session, which therefore acts as their
    actor too.
    """
    sid = _event_sid(event)
    if not sid:
        raise ValueError("缺少会话身份，无法操作对局")
    return sid, sid


def _bound_from_event(event: Any) -> dict[str, Any] | None:
    """Bound turn context injected into ``event.extra`` (``gamehub_turn``)."""
    extra = getattr(event, "extra", None)
    if not isinstance(extra, dict):
        return None
    bound = extra.get(TURN_KEY)
    if isinstance(bound, dict) and str(bound.get("match_id") or "").strip():
        return bound
    return None


def _is_notice_event(event: Any) -> bool:
    """Whether this request was triggered by a synthetic notice message."""
    message = None
    try:
        message = getattr(event, "message", None)
    except Exception:  # noqa: BLE001 - events without messages are fine
        message = None
    if message is None:
        messages = getattr(event, "messages", None) or ()
        message = messages[-1] if messages else None
    return bool(getattr(message, "is_notice", False))


def _is_notice_turn(
    event: Any,
    *,
    actor: str = "",
    bound: dict[str, Any] | None = None,
) -> bool:
    """Is this a notice turn (bound context, notice message or notice actor)?

    A notice message's author is the synthetic trigger, so an actor lookup can
    never hit a real seat: this flag makes the tools answer explicitly and
    closes every write path that would otherwise rely on an actor lookup.
    """
    if bound is not None or _bound_from_event(event) is not None:
        return True
    if _is_notice_event(event):
        return True
    return bool(actor) and actor == NOTICE_ACTOR


def _notice_turn_reply(tool: str) -> str:
    return (
        f"当前是行动通知回合，{tool} 不可用（通知回合只处理本局行动，"
        "不做开局/认输/策略写入）。"
    )


def _entry_text(plugin: Any, match: Any, seat: int) -> str:
    """Seat link: only in private chats; groups get the bare match id."""
    settings = getattr(plugin, "settings", None)
    base = str(getattr(settings, "site_base_url", "") or "").rstrip("/")
    if not base:
        return f"对局 ID：{match.id}"
    token = match.seats[seat].token
    if not token:
        return f"对局 ID：{match.id}"
    return f"对局 ID：{match.id}\n页面：{base}/{MATCH_PAGE_PATH}?id={match.id}&t={token}"


def _session_seat(match: Any, actor: str, session_id: str) -> int | None:
    """Seat the caller sees in a normal (non-notice) turn.

    Doudizhu: with the bot playing, never expose the human opponent's private
    hand through the user's actor; teammate matches: the bot's stance is the
    human side even when the actor has no seat.
    """
    seat = _seat_of_actor(match, actor)
    if match.kind == "doudizhu":
        seat = (
            modes.nori_seat_index(match)
            if modes.mode_of(match) == modes.MODE_NORI
            else (seat if _is_private_session(session_id) else None)
        )
    if seat is None and role_of(match) == ROLE_TEAMMATE and match.kind != "doudizhu":
        seat = _human_seat_of(match)
    return seat


def _state_text(plugin: Any, match: Any, seat: int | None) -> str:
    """Full state answer: view + plan summary + module strategy guidance."""
    parts = [_view_text(match, seat)]
    plan_lines = _plan_lines(match, role=role_of(match))
    if plan_lines:
        parts.append("\n".join(plan_lines))
    if role_of(match) != ROLE_TEAMMATE:
        guidance_seat = ai_seat_index(match)
        if guidance_seat is None:
            guidance_seat = seat
        guidance = _plan_guidance(match, guidance_seat)
        if guidance:
            parts.append("当前游戏指导：" + guidance)
    return "\n\n".join(parts)


async def _bound_state_text(plugin: Any, bound: dict[str, Any]) -> str:
    """Read-only view for a bound notice turn (no actor lookup)."""
    rooms = plugin.rooms
    match_id = str(bound.get("match_id") or "")
    loader = getattr(rooms, "get_or_load", None)
    room = await loader(match_id) if callable(loader) else None
    if room is None:
        return "通知回合绑定的对局不存在或已结束。"
    match = room.match
    if match.kind == "doudizhu":
        director = getattr(plugin, "_director", None)
        grant = director.grant_of(match.id) if director is not None else None
        if (
            grant is None
            or grant.state != "active"
            or grant.key != bound.get("grant")
            or grant.seat != bound.get("seat")
        ):
            return "本局私有视角授权已结束，请等待下一次行动回合。"
    seat = bound_seat(bound, match)
    return _view_text(match, seat)


# ----------------------------------------------------------------------
#  Global tools
# ----------------------------------------------------------------------
async def game_list(plugin: Any, event: Any, **kwargs: Any) -> str:
    """List playable games with their move syntax."""
    lines = ["可玩的游戏："]
    host_name = str(getattr(plugin, "host_agent_name", "") or "").strip()
    for game in games.catalog(host_name, _agent_configured_name(plugin)):
        if not game["ready"]:
            continue
        players = game["players"]
        lines.append(
            f"- {game['kind']}：{game['name']['zh']}（{game['family']}，"
            f"{players['min']}-{players['max']} 人）{game['blurb']}"
        )
        lines.append(f"  走子写法：{game['move_help']}")
    lines.append("")
    lines.append("开局用 game_new()；走子请在对局页面进行（聊天不代你落子）。")
    return "\n".join(lines)


async def game_new(
    plugin: Any,
    event: Any,
    kind: str = "gomoku",
    mode: str | None = None,
    role: str | None = None,
    difficulty: str | None = None,
    opponent: str | None = None,
    **kwargs: Any,
) -> str:
    """Open a match for the current session (one active match per actor)."""
    bound = _bound_from_event(event)
    if _is_notice_turn(event, bound=bound):
        return _notice_turn_reply("game_new")
    actor, session_id = _resolve(event)
    rooms = plugin.rooms

    kind = str(kind or "gomoku").strip()
    opponent = str(opponent or "").strip().lower()
    mode = str(mode or "").strip().lower()
    role = str(role or "").strip().lower()
    if opponent == "nori":
        opponent = "ai"
    if not opponent:
        # Derive from the newer knobs when only mode/role were supplied.
        opponent = (
            "bot"
            if (mode == modes.MODE_BOT or role == modes.ROLE_TEAMMATE)
            else "ai"
        )
    difficulty = str(difficulty or "").strip().lower()
    if difficulty not in ("", "easy", "advanced"):
        return "difficulty 只能是 easy 或 advanced（仅对手选陪练或当前agent时有效）。"
    if opponent not in ("ai", "bot"):
        return f"对手只能是{_bot_name(plugin)}本人或陪练。"
    if opponent == "ai" and difficulty:
        return "difficulty 只在对手选择陪练或当前agent时有意义。"
    if games.maybe_get(kind) is None:
        return f"没有这个游戏：{kind}。可以用 game_list 查看。"

    existing = rooms.find_by_actor(actor)
    if existing is not None:
        seat = _seat_of_actor(existing.match, actor)
        if seat is None:
            return (
                f"你已有一局未结束（{existing.match.id}），但当前会话不在该局座位上；"
                "可用 game_state 查看。"
            )
        visible_seat = seat
        if existing.match.kind == "doudizhu":
            nori_seat = modes.nori_seat_index(existing.match)
            if nori_seat is not None:
                visible_seat = nori_seat
        text = "你已经有一局未结束了，先下完或认输吧。\n" + _view_text(
            existing.match, visible_seat
        )
        if _is_private_session(session_id):
            text += "\n" + _entry_text(plugin, existing.match, seat)
        return text

    default_name = ""
    getter = getattr(plugin, "name_for_actor", None)
    if callable(getter):
        default_name = str(getter(actor) or "").strip()

    if opponent == "bot" and difficulty == "advanced":
        mode_options: dict[str, Any] = {"mode": "bot", "difficulty": "advanced"}
    elif opponent == "bot":
        mode_options = {"mode": "bot", "difficulty": "easy"}
    else:
        # Default: play against the bot herself (notice-turn driven; persisted).
        mode_options = {"mode": "nori"}
    # Advanced practice seat display name = the configured agent name (absent
    # keeps the RoomManager default); the bot's own seat name is untouched.
    opponent_display_name = ""
    if kind in ("gomoku", "reversi"):
        level = str(kwargs.get("level") or "intermediate")
        mode_options["level"] = level
    if kind == "doudizhu":
        mode_options = {"lineup": str(kwargs.get("lineup") or "nori_agent")}
    if mode_options.get("difficulty") == "advanced" or kind == "doudizhu":
        getter = getattr(plugin, "agent_player_name", None)
        if callable(getter):
            opponent_display_name = str(getter() or "").strip()

    try:
        room, seat, _token = await rooms.create(
            kind=kind,
            actor=actor,
            session_id=session_id,
            opponent=opponent,
            name=str(kwargs.get("name") or "").strip() or default_name or None,
            source=SOURCE_CHAT,
            now=_now(),
            mode_options=mode_options,
            opponent_name=opponent_display_name or None,
            options=(
                {"hands": str(kwargs.get("hands", 1))}
                if kind == "doudizhu"
                else {}
            ),
        )
    except Exception as exc:  # noqa: BLE001 - RoomError & friends answer the model
        return f"开局失败：{getattr(exc, 'message', None) or exc}"

    module = games.get(kind)
    lines = [f"已开局：{module.name['zh']}。"]
    # Seat identity / colour / move order all come from the module
    # (seat_labels/describe); no game-specific text is hard-coded here.
    seat_label = ""
    try:
        labels = list(module.seat_labels(room.match.state) or [])
        seat_label = labels[seat] if 0 <= seat < len(labels) else ""
    except Exception:  # noqa: BLE001 - a missing label only drops one line
        seat_label = ""
    if seat_label:
        lines.append(f"你执{seat_label}。")
    if _is_private_session(session_id):
        lines.append(_entry_text(plugin, room.match, seat))
    else:
        lines.append(f"对局 ID：{room.match.id}（群聊里不发行棋链接）")
    lines.append("")

    visible_seat = seat
    if kind == "doudizhu":
        visible_seat = (
            modes.nori_seat_index(room.match)
            if modes.mode_of(room.match) == modes.MODE_NORI
            else (seat if _is_private_session(session_id) else None)
        )
    lines.append(_view_text(room.match, visible_seat))
    guidance = _plan_guidance(room.match, ai_seat_index(room.match))
    if guidance:
        lines.append("")
        lines.append(f"{_opponent_name(room.match)}的当前游戏指导：" + guidance)
    return "\n".join(lines)


async def game_state(
    plugin: Any, event: Any, match_id: str | None = None, **kwargs: Any
) -> str:
    """Full state of the current match (or of ``match_id`` when given)."""
    bound = _bound_from_event(event)
    if bound is not None:
        return await _bound_state_text(plugin, bound)
    if _is_notice_turn(event):
        return (
            "当前是行动通知回合：本回合的对局上下文已随通知给出，"
            "game_state 暂不可用（通知回合不做 actor 反查）。"
        )
    actor, session_id = _resolve(event)
    rooms = plugin.rooms

    target = str(match_id or "").strip()
    if target:
        loader = getattr(rooms, "get_or_load", None)
        room = await loader(target) if callable(loader) else None
        if room is None:
            return "找不到这局对局（可能已结束或不存在）。"
        seat = _session_seat(room.match, actor, session_id)
        return _state_text(plugin, room.match, seat)

    room = rooms.find_by_actor(actor)
    if room is None:
        return "你现在没有进行中的对局。可以用 game_new 开一局。"
    seat = _session_seat(room.match, actor, session_id)
    return _state_text(plugin, room.match, seat)


async def game_resign(
    plugin: Any, event: Any, match_id: str | None = None, **kwargs: Any
) -> str:
    """Let the bot resign her own seat (never the user's / practice seat)."""
    bound = _bound_from_event(event)
    if _is_notice_turn(event, bound=bound):
        return _notice_turn_reply("game_resign")
    actor, _session_id = _resolve(event)
    rooms = plugin.rooms
    target = str(match_id or "").strip()
    try:
        if target:
            loader = getattr(rooms, "get_or_load", None)
            room = await loader(target) if callable(loader) else None
            if room is None:
                return "找不到这局对局（可能已结束或不存在）。"
            if role_of(room.match) == ROLE_TEAMMATE:
                return (
                    f"当前对局里没有{_bot_name(plugin)}的座位"
                    f"（她是你的队友，不占座位），不能由{_bot_name(plugin)}认输。"
                )
            seat = modes.nori_seat_index(room.match)
            if seat is None:
                return (
                    f"当前对局里没有{_bot_name(plugin)}的座位"
                    "（对手是规则陪练或真人），不能由她认输。"
                )
            match = await room.apply(seat, {"type": "resign"})
        else:
            match = await rooms.resign_ai(actor)
    except Exception as exc:  # noqa: BLE001 - errors are answered, not raised
        return f"认输失败：{getattr(exc, 'message', None) or exc}"

    status = status_of(match)
    winners = [
        match.seats[i].name
        for i in (status.outcome.winners if status.outcome else [])
        if 0 <= i < len(match.seats)
    ]
    if winners:
        return f"你认输了，{'、'.join(winners)} 获胜"
    return f"你认输了。{status.result_text or ''}".strip()


# ----------------------------------------------------------------------
#  BaseTool shape (reused by tools_turn.BoundStateTool)
# ----------------------------------------------------------------------
class GameStateTool(BaseTool):
    """``game_state`` as a ``BaseTool`` (global and per-turn bound variants)."""

    name = "game_state"
    description = GAME_STATE_DESCRIPTION
    parameters = _EMPTY_PARAMS

    def __init__(
        self,
        plugin: Any = None,
        *,
        bound: dict[str, Any] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(ctx=ctx)
        self._plugin = plugin
        self._bound = bound

    async def execute(
        self, event: Any = None, match_id: str | None = None, **kwargs: Any
    ) -> str:
        plugin = self._plugin
        if plugin is None:
            return "对局服务未就绪，无法查看对局。"
        bound = _bound_from_event(event) or self._bound
        if bound is not None:
            return await _bound_state_text(plugin, bound)
        return await game_state(plugin, event, match_id, **kwargs)


__all__ = [
    "BaseTool",
    "GAME_STATE_DESCRIPTION",
    "GameStateTool",
    "MATCH_PAGE_PATH",
    "NOTICE_ACTOR",
    "PLUGIN_ID",
    "TURN_KEY",
    "game_list",
    "game_new",
    "game_resign",
    "game_state",
]
