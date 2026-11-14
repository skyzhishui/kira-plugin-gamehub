"""Outbound bridge between the internal gamehub adapter and the plugin runtime."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

OutboundSink = Callable[[str, Any], Awaitable[None]]

_outbound_sink: OutboundSink | None = None


def set_outbound_sink(sink: OutboundSink | None) -> None:
    """Install the runtime callback that renders messages sent to the game session."""
    global _outbound_sink
    _outbound_sink = sink


async def emit_outbound(sid: str, chain: Any) -> None:
    """Forward one outbound chain to the runtime; silently drop when unwired."""
    sink = _outbound_sink
    if sink is None:
        return
    await sink(sid, chain)


class MatchWSHub:
    """Per-match WebSocket fan-out used by the lobby/match pages."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[Any]] = {}
        self._lock = asyncio.Lock()

    def subscribe(self, match_id: str, websocket: Any) -> None:
        self._subscribers.setdefault(str(match_id), set()).add(websocket)

    def unsubscribe(self, match_id: str, websocket: Any) -> None:
        bucket = self._subscribers.get(str(match_id))
        if bucket is None:
            return
        bucket.discard(websocket)
        if not bucket:
            self._subscribers.pop(str(match_id), None)

    def subscriber_count(self, match_id: str) -> int:
        return len(self._subscribers.get(str(match_id), ()))

    async def broadcast(self, match_id: str, payload: dict[str, Any]) -> None:
        """Send one payload to every socket of a match; drop broken sockets."""
        async with self._lock:
            sockets = list(self._subscribers.get(str(match_id), ()))
        for websocket in sockets:
            try:
                sender = getattr(websocket, "send_json", None)
                if callable(sender):
                    await sender(payload)
                else:
                    await websocket.send_text(str(payload))
            except Exception:  # noqa: BLE001 - a broken socket must not break fan-out
                self.unsubscribe(match_id, websocket)


ws_hub = MatchWSHub()
