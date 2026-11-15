"""Native group messages and deterministic routing for the shared game room."""

from __future__ import annotations

import time
import uuid

from .session import GAME_SID


def _build_event(plugin, uid: str, name: str, text: str, *, role: str, match_id: str = ""):
    """Build the native group event for one line (no dispatch)."""
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
            chain=MessageChain([Text(text)]), is_mentioned=role == "player",
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


async def route_group_message(plugin, event):
    """Players/notices always flush; spectators never arm a reply task.

    Player identity is assigned by the authenticated server transport, not by
    message text or a guest-supplied role. Every player message is a strong
    signal: no probability, wake-word, activity or scoring gate is consulted.
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
        await plugin.ctx.flush_session_messages(GAME_SID)
