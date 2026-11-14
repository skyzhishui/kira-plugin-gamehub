"""Notice publishing into the dedicated game session.

KiraAI injects a notice as a normal inbound IM event, which then flows through
the chat plugin buffer -> flush -> full IM pipeline. That keeps table talk and
turn notifications in the plugin-owned ``gamehub`` session instead of the
user's real chat session.

The host adapter must never raise into game logic: a missing adapter or an
unresolvable session is a warning plus ``False``.
"""

from __future__ import annotations

import inspect
from typing import Any

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

try:  # production: KiraAI message elements
    from core.chat.message_elements import Text
    from core.chat.message_utils import MessageChain
except ImportError:  # standalone tests: minimal structural stand-ins

    class Text:  # type: ignore[no-redef]
        """Minimal ``Text`` element for host-less unit tests."""

        def __init__(self, text: str) -> None:
            self.text = text

        def __repr__(self) -> str:
            return f"Text({self.text!r})"

    class MessageChain:  # type: ignore[no-redef]
        """Minimal ``MessageChain`` for host-less unit tests."""

        def __init__(self, message_list: list[Any] | None = None) -> None:
            self.message_list = list(message_list or [])

        def __iter__(self):
            return iter(self.message_list)

        def __len__(self) -> int:
            return len(self.message_list)

        def __getitem__(self, index):
            return self.message_list[index]

        def __repr__(self) -> str:
            return f"MessageChain({self.message_list!r})"


async def send_notice(plugin: Any, sid: str, text: str) -> bool:
    """Publish ``text`` as a mentioned system notice into session ``sid``.

    Returns ``True`` when the notice was published, ``False`` when the session
    could not be resolved, no adapter is available for it, or the host raised.
    """
    sid = str(sid or "").strip()
    body = str(text or "")
    if not sid or not body:
        logger.warning("send_notice: empty session or text (sid=%r)", sid)
        return False

    ctx = getattr(plugin, "ctx", None)
    session_mgr = getattr(ctx, "session_mgr", None)
    publish = getattr(ctx, "publish_notice", None)
    resolver = getattr(session_mgr, "resolve_session", None)
    if not callable(publish) or not callable(resolver):
        logger.warning("send_notice: host context missing session_mgr/publish_notice")
        return False

    try:
        session = resolver(sid)
        if inspect.isawaitable(session):
            session = await session
    except Exception:
        logger.warning("send_notice: cannot resolve session %r", sid, exc_info=True)
        return False

    try:
        chain = MessageChain([Text(body)])
        await publish(session, chain, is_mentioned=True)
    except Exception:
        logger.warning("send_notice: publish failed for %r", sid, exc_info=True)
        return False
    logger.info("send_notice: published notice to session=%s", sid)
    return True