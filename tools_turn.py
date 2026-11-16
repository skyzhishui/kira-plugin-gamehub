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

import time
from typing import Any

try:  # production: the host tag machinery (request-scoped tag_set dispatch)
    from core.tag import BaseTag as TagBase
except ImportError:  # standalone tests import this module as top-level
    class TagBase:  # type: ignore[no-redef]
        """Minimal duck-typed stand-in for ``core.tag.BaseTag``."""

        parent: str | None = None

try:  # production: plugin package (``<plugin>.tools_turn``)
    from .naming import play_tool_name, state_tool_name
    from .tools import (
        BOUND_STATE_DESCRIPTION,
        BaseTool,
        GameStateTool,
        NOTICE_ACTOR,
    )
except ImportError:  # standalone tests import this module as top-level
    from naming import play_tool_name, state_tool_name
    from tools import (
        BOUND_STATE_DESCRIPTION,
        BaseTool,
        GameStateTool,
        NOTICE_ACTOR,
    )

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)


_NO_GRANT = "本回合没有有效的落子授权（可能已被停止或已结束）。"

# Schema default for ``max_say_len`` (mirrors ``host/settings.py`` /
# ``schema.json``); used when the configured value is missing or invalid so a
# broken setting never silently disables the table-talk cap.
DEFAULT_MAX_SAY_LEN = 280


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


# ``play_tool_name`` / ``state_tool_name`` live in the dependency-free
# ``naming`` leaf module so the prompt context (``context._turn_requirements``)
# spells the surface exactly like this module registers it; they are
# re-exported here because ``director``/``host.prompt`` import them from this
# module.


def _play_tag_description(move_help: str) -> str:
    """The tag's schema description: pure format, no behaviour, no threats.

    The submission contract it teaches is the same one the ephemeral
    ``gamehub_context`` requirements section carries: root level (outside
    ``<msg>``), at the end of the reply, move as the tag BODY (never an
    attribute). No "the system plays for you" style pressure — the
    corrective loop (bounded retries + context feedback) handles misses.
    """
    body = "标签正文填本手动作字符串（不要写成标签属性）"
    if move_help:
        body += f"（本局格式：{move_help}）"
    else:
        body += "，格式以本局规则为准"
    return (
        "落子控制标签，与<msg>同级（放在回复末尾，不要放进<msg>内）。"
        f"{body}。轮到你行动的回合输出本标签完成落子。"
    )


def _truncate_say(plugin: Any, say: Any) -> str:
    """Cut the table-talk line to the configured limit.

    ``0`` means "no limit" (the schema default is 280, so a zero can only come
    from an explicit configuration choice); invalid values fall back to the
    schema default rather than silently disabling the cap.
    """
    text = str(say or "").strip()
    if not text:
        return ""
    settings = getattr(plugin, "settings", None)
    raw = getattr(settings, "max_say_len", DEFAULT_MAX_SAY_LEN)
    try:
        limit = int(raw if raw is not None else DEFAULT_MAX_SAY_LEN)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_SAY_LEN
    if limit < 0:
        limit = DEFAULT_MAX_SAY_LEN
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

    async def handle(self, value: str = "", **kwargs: Any) -> None:
        """No-op for execution; records a misplaced nested attempt.

        Root-level tags reach ``submit_play_from_actions`` through the parsed
        ``RootTagAction`` list. Core's ``parse_xml`` however also invokes
        this ``handle`` when a ``<play_…>`` element is NESTED inside
        ``<msg>`` (there it is treated as an in-message tag: the handler
        returns nothing, no message element is produced and no
        ``RootTagAction`` exists) — the move would vanish silently. Recording
        the attempt on the grant lets the AFTER_XML_PARSE path reject the
        reply with explicit format feedback instead. Must be a coroutine:
        the core awaits ``handle`` when it walks parsed actions
        (``execute.py``), so a plain ``def`` would explode into an "object
        NoneType can't be used in 'await'" error log on every reply that
        carries the tag.
        """
        grant = self._grant
        if grant is not None:
            grant.tag_misplaced = str(value or "").strip()
            grant.tag_misplaced_at = time.monotonic()
        return None


_MISPLACED_ERROR = (
    "落子标签必须放在回复末尾、与<msg>同级（不要放进<msg>内）"
)
_EMPTY_BODY_ERROR = (
    "动作必须写在标签正文里，不要写成标签属性（如 <play>pass</play>）"
)
# Freshness window for a misplaced-tag marker recorded during this reply's
# parse (``note_response`` clears stale markers at every new step too).
_MISPLACED_FRESH_S = 30.0


def _consume_misplaced(grant: Any) -> str | None:
    """A fresh misplaced-tag attempt on ``grant``; consumed on read.

    Presence is keyed on the timestamp, not the recorded body: a nested tag
    with an EMPTY body (``<play_…></play_…>`` inside ``<msg>``) is still an
    attempt and still earns the explicit format rejection — treating it as
    "no attempt" would silently fall back to the generic F1 text nudge,
    which mislabels a format mistake as "only talked". The returned body may
    be "" and is only informational; the rejection message covers placement
    and body form.
    """
    try:
        value = str(getattr(grant, "tag_misplaced", "") or "").strip()
        stamp = float(getattr(grant, "tag_misplaced_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if not stamp:
        return None
    if time.monotonic() - stamp > _MISPLACED_FRESH_S:
        return None
    grant.tag_misplaced = ""
    grant.tag_misplaced_at = 0.0
    return value


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
    event: Any = None,
) -> bool | None:
    """Submit the move a ``play_<match>`` root tag carries (AFTER_XML_PARSE).

    Called from the plugin's ``@on.after_xml_parse`` hook — before any
    message of this reply is delivered — so the atomic move+say lands before
    the table talk reaches the chat window. ``grant`` is the session's live
    grant (the hook resolves it per ``sid``); a resolved or replaced grant
    makes ``submit_play`` refuse, so a late or duplicate tag can never land
    in a later turn — the same race-safety the old native tool had.

    Format rejections are explicit, never silent (core ``parse_xml`` no-ops
    a root play tag nested inside ``<msg>`` and yields an empty body for an
    attribute-form tag — both would otherwise disappear):

    - a fresh nested-tag attempt (recorded by ``PlayTurnTag.handle`` during
      the parse) or an empty-body root tag → a bounded ``note_tag_rejected``
      format notice, and the specifics ride the context requirements
      section on the retry request;
    - no tag at all → ``None`` (ordinary text reply; the F1 corrective
      arming handles it — it now always arms, see ``director.note_response``).

    Returns ``None`` when the reply carries no play tag (the caller must not
    touch the event), ``False`` when the tag was rejected, and ``True`` when
    the move was applied. Rejections keep the reply delivering normally:
    a malformed tag (nested/misplaced or empty body) and an ``invalid`` move
    each re-open the feedback loop through a bounded
    ``director.note_tag_rejected`` notice, while a terminal/``stale`` move is
    logged and not retried (no legal retry exists for a resolved turn). On
    ``True`` the reply is terminated (``event.stop()``): the agent loop ends,
    the remaining delivery tail is skipped and ``final_result`` fires
    immediately, which releases the next turn's pending trigger seconds
    earlier. The table talk does NOT ride the aborted delivery; it goes to the
    page window through ``runtime.record_agent_line`` (same read-model row +
    WS frame the delivery path produces) and stays on the match bubble via the
    atomic ``say``. Session memory is unaffected — finalize saves it for
    stopped turns by contract.
    """
    tag_name = play_tool_name(grant.match_id)
    move = ""
    tag_seen = False
    for action in actions or ():
        if _action_is_play_tag(action, tag_name):
            move = str(getattr(action, "value", "") or "").strip()
            tag_seen = True
            break
    if tag_seen:
        # A play tag was actually parsed: stand down a pending corrective
        # nudge before any submit outcome (even a rejection gives feedback).
        note = getattr(director, "note_tool_progress", None)
        if callable(note):
            note(str(sid or ""))
    elif _consume_misplaced(grant) is not None:
        # A nested attempt this step (body may be empty — the marker is
        # keyed on the timestamp): explicit bounded format feedback instead
        # of a silent watchdog fill or a mislabeled text nudge.
        note = getattr(director, "note_tool_progress", None)
        if callable(note):
            note(str(sid or ""))
        await _reject(plugin, director, sid, grant, "格式错误", _MISPLACED_ERROR,
                      reason="format")
        return False
    else:
        return None
    if not move:
        # Attribute-form / empty body: the move must be the tag body.
        await _reject(plugin, director, sid, grant, "格式错误", _EMPTY_BODY_ERROR,
                      reason="format")
        return False
    say = _truncate_say(plugin, _chain_texts(actions))
    submit = getattr(director, "submit_play", None)
    if not callable(submit):
        return False
    if say:
        result = await submit(grant.match_id, grant.key, move, say, persist_say=True)
    else:
        result = await submit(grant.match_id, grant.key, move)
    if not bool(getattr(result, "ok", False)):
        status = str(getattr(result, "status", "") or "")
        error = str(getattr(result, "error", "") or "")
        logger.warning(
            "turn director: play tag rejected (%s seat %s <- %s: %s)",
            grant.match_id, grant.seat, move, status or error or "unknown",
        )
        if status == "invalid":
            await _reject(plugin, director, sid, grant, move, error, reason="invalid")
        return False
    grant.last_rejection = None  # the contract feedback is resolved
    grant.tag_misplaced = ""  # any stale nested-tag marker is moot now
    grant.tag_misplaced_at = 0.0
    logger.info(
        "turn director: play tag submitted (%s seat %s <- %s)",
        grant.match_id, grant.seat, move,
    )
    # The delivery tail is skipped below, so the table talk reaches the page
    # window through the read model directly (cosmetic on failure).
    runtime = getattr(plugin, "_runtime", None) or getattr(plugin, "runtime", None)
    record = getattr(runtime, "record_agent_line", None)
    if callable(record) and say:
        try:
            await record(str(sid or ""), say)
        except Exception:  # noqa: BLE001 - the window line must not block the stop
            logger.debug("gamehub: window line record failed", exc_info=True)
    # Stop the reply: the agent loop ends, the delivery tail is skipped and
    # final_result (with the memory save) fires immediately.
    stop = getattr(event, "stop", None)
    if callable(stop):
        stop()
    return True


async def _reject(
    plugin: Any,
    director: Any,
    sid: str,
    grant: Any,
    move: str,
    error: str,
    *,
    reason: str,
) -> None:
    """Route one rejection through the director's bounded notice (fail-soft).

    Single call, no compatibility retry: ``note_tag_rejected`` uniformly
    takes ``reason`` inside this project, so a TypeError here is a real bug
    and must surface (in the log) instead of being masked by a second
    invocation. The guard only keeps a feedback failure from breaking the
    reply delivery.
    """
    reject_note = getattr(director, "note_tag_rejected", None)
    if not callable(reject_note):
        return
    try:
        await reject_note(str(sid or ""), grant, move, error, reason=reason)
    except Exception:  # noqa: BLE001 - feedback must not break the reply
        logger.debug("gamehub: rejection notice failed", exc_info=True)


class BoundStateTool(GameStateTool):
    """``state_<match_id>``: seat-scoped view bound to one grant.

    The match id keeps two concurrent matches in one session from replacing
    each other's tool (same reasoning as ``play_<match_id>``). Unlike the
    move, a read query needs a request/response round trip, so this stays a
    native tool next to the tag channel.

    The description is its own (``BOUND_STATE_DESCRIPTION``): the inherited
    ``GAME_STATE_DESCRIPTION`` promises a plan/TTL section the bound path does
    not render, tells the model to "call game_state first" — which is
    explicitly rejected during a notice turn — and names ``play`` as the move
    surface instead of the ``play_<match>`` tag. Same name/parameters shape,
    different contract.
    """

    description = BOUND_STATE_DESCRIPTION

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
