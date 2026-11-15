"""Per-turn prompt, request-scoped tool and play-tag injection for the host agent."""

from __future__ import annotations

from typing import Any

from core.prompt_manager import Prompt

try:  # production package
    from ..context import build_game_state_context
    from ..tools_turn import PlayTurnTag
except ImportError:  # standalone tests import plugin-root modules directly
    from context import build_game_state_context
    from tools_turn import PlayTurnTag


def inject_into_request(plugin: Any, event: Any, request: Any, tag_set: Any = None) -> None:
    """Attach the context block and the turn surface to one request.

    While a notice-driven turn grant is active for this session:

    - ``req.tool_set`` receives ``state_<match>`` (the read view);
    - ``tag_set`` receives the ``play_<match>`` root control tag — the move
      channel (``tools_turn.PlayTurnTag``). Registration is per request, so
      a resolved grant revokes both simply by not adding them again; other
      sessions never see the tag because their requests carry a different
      ``tag_set``.
    - The context block (full rules / move syntax / board snapshot / recent
      moves / assistance — the single carrier of rules and board state; turn
      notices carry only the header) is appended to ``user_prompt`` with
      ``persist=False`` so it never enters memory and never shifts the
      cacheable prompt prefix.
    """
    sid = str(getattr(event, "sid", "") or "")
    if not sid:
        return

    director = getattr(plugin, "_director", None)
    grant = None
    if director is not None:
        try:
            grant = director.active_grant_for(sid)
        except Exception:  # noqa: BLE001 - injection must not break a turn
            grant = None

    tool_set = getattr(request, "tool_set", None)
    if director is not None and tool_set is not None:
        try:
            tools = director.tools_for(sid)
        except Exception:  # noqa: BLE001 - tool injection must not break a turn
            tools = []
        if tools:
            tool_set.add(*tools)

    if grant is not None and tag_set is not None:
        try:
            match_kind = director.match_kind_of(grant.match_id)
        except Exception:  # noqa: BLE001 - description degrades to generic
            match_kind = ""
        try:
            tag_set.register(PlayTurnTag(plugin, grant, match_kind=match_kind))
        except Exception:  # noqa: BLE001 - tag injection must not break a turn
            pass

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
