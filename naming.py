"""Shared request-scoped naming helpers (leaf module, no plugin-internal imports).

``play_<match>`` / ``state_<match>`` name sanitisation and the host agent
display-name resolution are used by both the chat tool surface (``tools``,
``context``) and the per-turn surface (``tools_turn``). Keeping them in one
leaf module means the two sides can never drift: a model that submits a move
under a name the other side does not recognise loses the move silently.

This module must stay dependency-free (stdlib only) so every importer can use
it regardless of whether the plugin is packaged as ``<plugin>.naming`` or
loaded as a top-level module in the standalone test suite.
"""

from __future__ import annotations

from typing import Any

# Neutral fallback when no host persona name is configured.
NEUTRAL_HOST_NAME = "对手"


def safe_id(match_id: Any) -> str:
    """Alphanumeric-only match id fragment used inside tool/tag names."""
    return "".join(ch for ch in str(match_id or "") if ch.isalnum())[:24]


def play_tool_name(match_id: Any) -> str:
    """Turn-scoped move surface name: ``play_<match_id>``.

    Several matches may share one target session, so the name carries the
    match id: a bare ``play`` would collide across concurrent matches (the
    name is shared by the native tool and today's root tag).
    """
    return f"play_{safe_id(match_id) or 'turn'}"


def state_tool_name(match_id: Any) -> str:
    """Turn-scoped state tool name: ``state_<match_id>``."""
    return f"state_{safe_id(match_id) or 'turn'}"


def host_agent_name(plugin: Any, fallback: str = NEUTRAL_HOST_NAME) -> str:
    """Host agent display name, resolved from the plugin instance only.

    ``plugin.host_agent_name`` is refreshed by ``host/runtime.py`` on every
    request; there is no host ``ctx.bot_config`` candidate (the plugin context
    of the target host has no such attribute). The caller picks the fallback
    so a neutral display name and an in-sentence pronoun can coexist.
    """
    return str(getattr(plugin, "host_agent_name", "") or "").strip() or fallback


__all__ = [
    "NEUTRAL_HOST_NAME",
    "host_agent_name",
    "play_tool_name",
    "safe_id",
    "state_tool_name",
]
