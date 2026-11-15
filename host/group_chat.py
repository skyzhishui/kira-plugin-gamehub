"""Native group messages and deterministic routing for the shared game room."""

from __future__ import annotations

import time
import uuid

from .session import GAME_SID


async def publish_chat(plugin, uid: str, name: str, text: str, *, role: str, match_id: str = ""):
    from core.chat.message_elements import Text
    from core.chat.message_utils import KiraIMMessage, KiraMessageEvent, MessageChain
    from core.chat.session import Group, User

    ctx = plugin.ctx
    adapter = ctx.adapter_mgr.get_adapter("gamehub")
    if adapter is None:
        raise RuntimeError("Game Hub adapter is unavailable")
    now = int(time.time())
    event = KiraMessageEvent(
        adapter=adapter.info, supported_elements=["text"], timestamp=now,
        message=KiraIMMessage(
            message_id=uuid.uuid4().hex, self_id="gamehub-ai", timestamp=now,
            sender=User(user_id=uid, nickname=name),
            group=Group(group_id="room", group_name="游戏房间"),
            chain=MessageChain([Text(text)]), is_mentioned=role == "player",
            extra={"gamehub_role": role, "match_id": match_id},
        ),
    )
    await ctx.event_bus.publish(event)


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
    buffer = plugin.ctx.get_buffer(GAME_SID)
    async with buffer.lock:
        buffer.add(event)
        if buffer.get_length() > 100:
            buffer.pop(buffer.get_length() - 100)
    event.stop()
    if trigger:
        await plugin.ctx.flush_session_messages(GAME_SID)
