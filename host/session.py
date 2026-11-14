"""Dedicated game session provisioning.

All notice-driven turns and page matches use one internal session,
``gamehub:dm:admin``, backed by the plugin-owned ``gamehub`` adapter. Keeping
it separate from the real WebChat session prevents board notices from
polluting the user's normal chat history.
"""

from __future__ import annotations

from core.adapter.adapter_info import AdapterInfo
from core.plugin import logger

GAME_ADAPTER_NAME = "gamehub"
GAME_SID = "gamehub:dm:admin"
PAGE_USER_ID = "admin"


def adapter_info() -> AdapterInfo:
    """Build the AdapterInfo used to instantiate the internal adapter."""
    return AdapterInfo(
        enabled=True,
        adapter_id=GAME_ADAPTER_NAME,
        name=GAME_ADAPTER_NAME,
        platform=GAME_ADAPTER_NAME,
        description="Internal game hub session; not a chat platform",
    )


async def ensure_game_session(ctx) -> None:
    """Create the dedicated session row if it does not exist yet."""
    await ctx.db.save_chat_session(
        GAME_SID,
        kind="im",
        title="游戏中心",
        description="Game Hub internal session for notice-driven matches",
        data={"gamehub": True},
    )
    logger.info(f"Game Hub session ready: {GAME_SID}")
