"""Internal adapter backing the shared ``gamehub:gm:room`` group session.

The adapter is not a chat platform. It exists so notices can drive the host
agent inside a session that is isolated from the user's real WebChat session,
and so the agent's replies can be rendered by the match page instead of being
delivered to a platform.
"""

from __future__ import annotations

import asyncio

from core.adapter.access import ListAccessPolicy
from core.adapter.base import AdapterTargetId, BaseAdapter
from core.adapter.capabilities import IMCapability
from core.adapter.message_format_metadata import MessageFormatMetadata
from core.chat.message_utils import KiraIMSentResult, MessageChain

try:  # production: loaded as ``plugins.<plugin>.adapter.__kira_adapter__``
    from ..host.bridge import emit_outbound
except ImportError:  # standalone import of this file
    from host.bridge import emit_outbound

NAME = "gamehub"


class GamehubIMCapability(IMCapability):
    """Forward native room group replies to the plugin runtime.

    The outbound chain is rendered by ``host.runtime._chain_text``, which only
    concatenates ``Text`` elements, so declaring media elements would let the
    host register expensive output tags (img/record/video) that can never be
    delivered here. Advertise text only; routing still goes through
    ``emit_outbound`` and the sink semantics are unchanged.
    """

    _SUPPORTED_ELEMENTS = ["text"]

    async def get_message_metadata(self) -> MessageFormatMetadata:
        return MessageFormatMetadata(self._supported_elements)

    async def send_direct_message(
        self, user_id: AdapterTargetId, message: MessageChain,
    ) -> KiraIMSentResult:
        await emit_outbound(f"{NAME}:dm:{user_id}", message)
        return KiraIMSentResult(ok=True)

    async def send_group_message(
        self, group_id: AdapterTargetId, message: MessageChain,
    ) -> KiraIMSentResult:
        await emit_outbound(f"{NAME}:gm:{group_id}", message)
        return KiraIMSentResult(ok=True)


class GamehubAdapter(BaseAdapter):
    """One long-lived adapter instance owned by the gamehub plugin."""

    NAME = NAME

    def __init__(self, ctx):
        super().__init__(ctx)
        self._stopped = asyncio.Event()
        self.register_capability(IMCapability, GamehubIMCapability(self))
        self.access.set_policy(
            capability_type=IMCapability,
            permission="im.direct.receive",
            policy=ListAccessPolicy.from_lists("allow_list", allow_list=["admin"]),
        )

    async def start(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    def get_client(self):
        return None

    async def send_direct_message(
        self, user_id: AdapterTargetId, send_message_obj: MessageChain,
    ) -> KiraIMSentResult:
        """Compatibility wrapper; prefer the IM capability directly."""
        return await self.get_capability(IMCapability).send_direct_message(user_id, send_message_obj)

    async def send_group_message(
        self, group_id: AdapterTargetId, send_message_obj: MessageChain,
    ) -> KiraIMSentResult:
        """Compatibility wrapper; prefer the IM capability directly."""
        return await self.get_capability(IMCapability).send_group_message(group_id, send_message_obj)
