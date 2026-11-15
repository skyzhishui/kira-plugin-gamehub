"""Dedicated game session provisioning.

All notice-driven turns and page matches use one internal session,
``gamehub:gm:room``, backed by the plugin-owned ``gamehub`` adapter. Keeping
it separate from the real WebChat session prevents board notices from
polluting the user's normal chat history.
"""

from __future__ import annotations

GAME_ADAPTER_NAME = "gamehub"
GAME_SID = "gamehub:gm:room"
PAGE_USER_ID = "admin"


def adapter_info():
    """Build the AdapterInfo used to instantiate the internal adapter."""
    from core.adapter.adapter_info import AdapterInfo

    return AdapterInfo(
        enabled=True,
        adapter_id=GAME_ADAPTER_NAME,
        name=GAME_ADAPTER_NAME,
        platform=GAME_ADAPTER_NAME,
        description="Internal game hub session; not a chat platform",
    )


async def ensure_game_session(ctx) -> None:
    """Create the dedicated session row if it does not exist yet."""
    from core.plugin import logger

    await ctx.db.save_chat_session(
        GAME_SID,
        kind="im",
        title="游戏中心",
        description="Game Hub internal session for notice-driven matches",
        data={"gamehub": True},
    )
    logger.info(f"Game Hub session ready: {GAME_SID}")
