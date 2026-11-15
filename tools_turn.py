"""Request-scoped turn surface: the ``play_<match>`` tag + ``state_<match>`` tool.

KiraAI has no ``ctx.session_tools``; a turn grant is expressed per request by
``main.py``'s ``@on.llm_request`` handler (``host/prompt.py``):

- ``state_<match>`` joins ``req.tool_set`` for one request (``build_turn_tools``);
- ``play_<match>`` is a **root control tag** registered on the per-request
  ``tag_set`` (``PlayTurnTag``), submitted by ``submit_play_from_actions``
  during ``AFTER_XML_PARSE``.

Why the move rides a tag instead of a native tool: glm-tier models imitate
whatever structured emission already sits in the session history. Kira's own
output format teaches a ``<msg>`` XML envelope with escaped inner tags, and
one text-form ``<tool_call>`` reply poisons the native tool channel for the
whole session (the model then writes escaped ``<tool_call>`` blocks that no
executor reads). The tag channel has no second channel to slip into: the move
travels in the reply text itself, parsed by the same machinery that reads
``<msg>``/``<emoji>``, and the model already follows those envelope
conventions reliably.

Discipline:

- both surfaces are built per turn (grant captured at construction time) and
  dropped by simply not adding them to the next request;
- the move is applied through ``plugin._director.submit_play`` — the same
  grant-bound, race-checked path the old tool used, so a stale or revoked
  grant can never land a move into a later turn;
- the table talk stays in the reply's ``<msg>`` lane (never a tag attribute:
  one stray quote there would fail the whole reply's XML parse) and is
  attached to the move as the atomic ``say``.
"""

from __future__ import annotations

from typing import Any

try:  # production: the host tag machinery (request-scoped tag_set dispatch)
    from core.tag import BaseTag as TagBase
except ImportError:  # standalone tests import this module as top-level
    class TagBase:  # type: ignore[no-redef]
        """Minimal duck-typed stand-in for ``core.tag.BaseTag``."""

        parent: str | None = None

try:  # production: plugin package (``<plugin>.tools_turn``)
    from .tools import BaseTool, GameStateTool, NOTICE_ACTOR, TURN_KEY
except ImportError:  # standalone tests import this module as top-level
    from tools import BaseTool, GameStateTool, NOTICE_ACTOR, TURN_KEY

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)


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
    """Turn-scoped move surface name: ``play_<match_id>``.

    Several matches may share one target session, so the name carries the
    match id: a bare ``play`` would collide across concurrent matches (the
    name is shared by the old native tool and today's root tag).
    """
    return f"play_{_safe_id(match_id) or 'turn'}"


def state_tool_name(match_id: str) -> str:
    return f"state_{_safe_id(match_id) or 'turn'}"


def _play_tag_description(move_help: str) -> str:
    body = "标签内填本手动作字符串"
    if move_help:
        body += f"（本局格式：{move_help}）"
    else:
        body += "，格式以本局规则为准"
    return (
        "落子控制标签，与<msg>同级（放在回复末尾，不要放进<msg>内）。"
        f"{body}。轮到你行动的回合必须输出一次本标签完成落子；"
        "只在正文里写出动作不算落子。"
    )


def _truncate_say(plugin: Any, say: Any) -> str:
    """Cut the table-talk line to the configured limit."""
    text = str(say or "").strip()
    if not text:
        return ""
    settings = getattr(plugin, "settings", None)
    try:
        limit = int(getattr(settings, "max_say_len", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    return text[:limit] if limit > 0 else text


class PlayTurnTag(TagBase):
    """``play_<match_id>`` root control tag: the tag-channel move submission.

    Scoping is request-level, mirroring ``tools_for``: ``host/prompt.py``
    registers the tag on the per-request ``tag_set`` while the session holds
    a live grant. ``parse_xml`` recognises the element only through that same
    ``tag_set``, so other sessions can neither see nor execute it, and a
    resolved grant simply stops registering it.

    ``handle`` is a deliberate no-op: the real submission happens earlier,
    during ``AFTER_XML_PARSE`` (``submit_play_from_actions``), where the
    reply's ``<msg>`` texts are still in reach to ride as the atomic ``say``.
    The tag element itself never becomes a message.
    """

    parent = None  # root-level control tag, outside <msg>

    def __init__(self, plugin: Any, grant: Any, *, match_kind: str = "") -> None:
        self._plugin = plugin
        self._grant = grant
        module = _module_of(str(match_kind or ""))
        move_help = str(getattr(module, "move_help", "") or "").strip()
        self.name = play_tool_name(grant.match_id)
        self.description = _play_tag_description(move_help)

    def handle(self, value: str = "", **kwargs: Any) -> None:
        """No-op by contract — see ``submit_play_from_actions``."""
        return None


def _action_is_play_tag(action: Any, tag_name: str) -> bool:
    tag = getattr(action, "tag", None)
    return str(getattr(tag, "name", "") or "") == tag_name


def _chain_texts(actions: Any) -> str:
    """The reply's ``<msg>`` texts, joined (the tag-channel ``say`` source).

    Duck-typed on ``MessageChain.message_list``; only ``Text`` elements carry
    a ``.text`` string, so emoji/media elements never leak into the line.
    """
    parts: list[str] = []
    for action in actions or ():
        for element in getattr(action, "message_list", ()) or ():
            text = getattr(element, "text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return " ".join(parts)


async def submit_play_from_actions(
    plugin: Any,
    director: Any,
    grant: Any,
    actions: Any,
    sid: str,
) -> bool:
    """Submit the move a ``play_<match>`` root tag carries (AFTER_XML_PARSE).

    Called from the plugin's ``@on.after_xml_parse`` hook — before any
    message of this reply is delivered — so the atomic move+say lands before
    the table talk reaches the chat window. ``grant`` is the session's live
    grant (the hook resolves it per ``sid``); a resolved or replaced grant
    makes ``submit_play`` refuse, so a late or duplicate tag can never land
    in a later turn — the same race-safety the old native tool had.

    An ``invalid`` result (illegal move / empty action) re-opens the feedback
    loop through ``director.note_tag_rejected`` — the tag channel has no
    tool-result lane, so without it a mistyped action would sit silently
    until the watchdog fill.
    """
    tag_name = play_tool_name(grant.match_id)
    move = ""
    for action in actions or ():
        if _action_is_play_tag(action, tag_name):
            move = str(getattr(action, "value", "") or "").strip()
            break
    if not move:
        return False
    say = _truncate_say(plugin, _chain_texts(actions))
    note = getattr(director, "note_tool_progress", None)
    if callable(note):
        note(str(sid or ""))  # stand down a pending corrective nudge
    submit = getattr(director, "submit_play", None)
    if not callable(submit):
        return True
    if say:
        result = await submit(grant.match_id, grant.key, move, say, persist_say=True)
    else:
        result = await submit(grant.match_id, grant.key, move)
    if bool(getattr(result, "ok", False)):
        logger.info(
            "turn director: play tag submitted (%s seat %s <- %s)",
            grant.match_id, grant.seat, move,
        )
    else:
        status = str(getattr(result, "status", "") or "")
        error = str(getattr(result, "error", "") or "")
        logger.warning(
            "turn director: play tag rejected (%s seat %s <- %s: %s)",
            grant.match_id, grant.seat, move, status or error or "unknown",
        )
        if status == "invalid":
            reject_note = getattr(director, "note_tag_rejected", None)
            if callable(reject_note):
                await reject_note(str(sid or ""), grant, move, error)
    return True


class BoundStateTool(GameStateTool):
    """``state_<match_id>``: seat-scoped view bound to one grant.

    The match id keeps two concurrent matches in one session from replacing
    each other's tool (same reasoning as ``play_<match_id>``). Unlike the
    move, a read query needs a request/response round trip, so this stays a
    native tool next to the tag channel.
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
    """Tools visible for one notice turn: ``[state_<id>]``.

    The move no longer rides a native tool (see ``PlayTurnTag``);
    ``match_kind`` is accepted for signature compatibility and unused here —
    the move-format hint now lives in the tag description
    (``PlayTurnTag(match_kind=...)``).
    """
    return [
        BoundStateTool(plugin, grant),
    ]


def build_play_tag(
    plugin: Any, grant: Any, *, match_kind: str = ""
) -> PlayTurnTag:
    """The tag-channel counterpart of ``build_turn_tools`` for the move."""
    return PlayTurnTag(plugin, grant, match_kind=match_kind)


__all__ = [
    "NOTICE_ACTOR",
    "BoundStateTool",
    "PlayTurnTag",
    "build_play_tag",
    "build_turn_context",
    "build_turn_tools",
    "play_tool_name",
    "state_tool_name",
    "submit_play_from_actions",
]
