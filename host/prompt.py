"""Per-turn prompt and request-scoped tool injection for the host agent."""

from __future__ import annotations

from typing import Any

from core.prompt_manager import Prompt

try:  # production package
    from ..context import build_game_state_context
except ImportError:  # standalone tests import plugin-root modules directly
    from context import build_game_state_context


def inject_into_request(plugin: Any, event: Any, request: Any, tag_set: Any = None) -> None:
    """Attach the board context and any turn-scoped tools to one request.

    - ``req.tool_set`` receives ``play_<match>`` / ``state_<match>`` only while
      a notice-driven turn grant is active for this session; grants are
      request-scoped, so not adding them on the next request revokes them.
    - The board block is appended to ``user_prompt`` with ``persist=False`` so
      it never enters memory and never invalidates the system-prompt cache.
    """
    sid = str(getattr(event, "sid", "") or "")
    if not sid:
        return

    director = getattr(plugin, "_director", None)
    tool_set = getattr(request, "tool_set", None)
    if director is not None and tool_set is not None:
        try:
            tools = director.tools_for(sid)
        except Exception:  # noqa: BLE001 - tool injection must not break a turn
            tools = []
        if tools:
            tool_set.add(*tools)

    try:
        block = build_game_state_context(plugin, sid)
    except Exception:  # noqa: BLE001 - context is advisory only
        block = ""
    if block:
        request.user_prompt.append(Prompt(
            content=block,
            name="gamehub_context",
            source="user",
            persist=False,
        ))
