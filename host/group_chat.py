"""Native group messages and deterministic routing for the shared game room."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .session import GAME_SID


def _build_event(
    plugin, uid: str, name: str, text: str, *, role: str, match_id: str = "",
    is_notice: bool = False,
):
    """Build the native group event for one line (no dispatch).

    ``is_notice`` builds a system notice (mentioned, ``system`` sender) that
    carries the same ``extra`` contract as a player/spectator line, so the
    reply it triggers can be attributed to its match.
    """
    from core.chat.message_elements import Text
    from core.chat.message_utils import KiraIMMessage, KiraMessageEvent, MessageChain
    from core.chat.session import Group, User

    ctx = plugin.ctx
    adapter = ctx.adapter_mgr.get_adapter("gamehub")
    if adapter is None:
        raise RuntimeError("Game Hub adapter is unavailable")
    now = int(time.time())
    return KiraMessageEvent(
        adapter=adapter.info, supported_elements=["text"], timestamp=now,
        message=KiraIMMessage(
            message_id=uuid.uuid4().hex, self_id="gamehub-ai", timestamp=now,
            sender=User(user_id=uid, nickname=name),
            group=Group(group_id="room", group_name="游戏房间"),
            chain=MessageChain([Text(text)]),
            is_notice=is_notice,
            is_mentioned=is_notice or role == "player",
            extra={"gamehub_role": role, "match_id": match_id},
        ),
    )


async def _buffer_event(plugin, event) -> None:
    """Add one event to the shared session buffer, trimming the oldest lines."""
    buffer = plugin.ctx.get_buffer(GAME_SID)
    async with buffer.lock:
        buffer.add(event)
        if buffer.get_length() > 100:
            buffer.pop(buffer.get_length() - 100)


async def build_notice_event(plugin, text: str, *, match_id: str = ""):
    """Build a notice for the shared game session, carrying its match in ``extra``.

    Mirrors ``ctx.publish_notice`` (system sender, mentioned, notice flag) but
    keeps ``extra["match_id"]`` on the message so the reply can be attributed.
    """
    return _build_event(
        plugin, "unknown", "system", text, role="notice",
        match_id=match_id, is_notice=True,
    )


async def publish_chat(plugin, uid: str, name: str, text: str, *, role: str, match_id: str = ""):
    event = _build_event(plugin, uid, name, text, role=role, match_id=match_id)
    await plugin.ctx.event_bus.publish(event)
    return event


async def publish_agent_say(plugin, uid: str, name: str, text: str, *, match_id: str = ""):
    """Buffer an agent line before publishing it, never triggering a reply.

    The line is placed in the shared ``GAME_SID`` buffer synchronously (under
    the buffer lock) before this function returns, so a caller can guarantee it
    precedes a director notice for the same move. It is then published as a
    stopped spectator event: the normal route still sees it (and leaves it
    buffered) but can never flush a host reply or trigger the main AI.
    """
    event = _build_event(plugin, uid, name, text, role="spectator", match_id=match_id)
    # The route handler must not append the same line twice.
    event.extra["gamehub_prebuffered"] = True
    await _buffer_event(plugin, event)
    event.stop()
    await plugin.ctx.event_bus.publish(event)
    return event


def _match_of(item: Any) -> str:
    """Match id carried by one buffered event or message ('' when unscoped)."""
    extra = getattr(item, "extra", None) or {}
    return str(extra.get("match_id") or "") if isinstance(extra, dict) else ""


def is_trigger(item: Any) -> bool:
    """Whether one event/message is a reply trigger (a notice or a player line)."""
    extra = getattr(item, "extra", None) or {}
    role = extra.get("gamehub_role") if isinstance(extra, dict) else None
    return bool(getattr(item, "is_notice", False)) or role == "player"


def trigger_match_id(batch: Any) -> str:
    """Match a reply batch belongs to, or ``""`` when it cannot be attributed.

    Only triggers count (notices and player lines). The reply belongs to a
    match only when every trigger in the batch is scoped to that same match.
    An unscoped trigger, or triggers from two matches, yield ``""``: the reply
    is then not written to any window. Spectator context and stopped agent
    says never pick the target.
    """
    messages = getattr(batch, "messages", None)
    if messages is None:
        messages = [batch]
    triggers = [m for m in messages if is_trigger(m)]
    matches = {_match_of(m) for m in triggers}
    if not triggers or len(matches) != 1:
        return ""
    return matches.pop()


def _same_room_filter(match_id: str):
    """Flush predicate that keeps one batch to one owner.

    - A scoped trigger (``match_id`` set) drains only its own room's events.
      Unscoped events are never swept into a match's reply batch, and other
      rooms' lines stay buffered for their own triggers.
    - An unscoped trigger (a diagnostic or legacy notice) drains only unscoped
      events. It never takes a scoped player or notice line of any room.

    Spectator context follows the same rule: it is drained only together with
    its own room's trigger, so it never leaks into another room's batch.
    """
    wanted = str(match_id or "")

    def keep(item: Any) -> bool:
        return _match_of(item) == wanted
    return keep


async def route_group_message(plugin, event):
    """Players/notices always flush; spectators never arm a reply task.

    Player identity is assigned by the authenticated server transport, not by
    message text or a guest-supplied role. Every player message is a strong
    signal: no probability, wake-word, activity or scoring gate is consulted.
    A trigger flushes only events with the same match scope; unscoped
    triggers form their own batches.
    """
    if str(event.session) != GAME_SID:
        return
    extra = event.extra or {}
    trigger = bool(event.is_notice or extra.get("gamehub_role") == "player")
    # Agent lines are buffered by ``publish_agent_say`` before publication, so
    # the route only stops them and never adds them a second time.
    if not extra.get("gamehub_prebuffered"):
        buffer = plugin.ctx.get_buffer(GAME_SID)
        async with buffer.lock:
            buffer.add(event)
            if buffer.get_length() > 100:
                buffer.pop(buffer.get_length() - 100)
    event.stop()
    if trigger:
        await plugin.ctx.flush_session_messages(
            GAME_SID, filter_fn=_same_room_filter(str(extra.get("match_id") or "")),
        )
