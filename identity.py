"""Shared constants for page-player identity handling.

The production identity model is the single display-name field in
``host/web.py`` (``identity.json`` in the plugin data dir); the fictional
three-tier mapping system (``IDENTITY_MAPPING`` / ``resolve_player_identity``)
was ported dead code and was removed in the 2026-11-16 review remediation
(P1-12). What survives is the nickname length cap shared by the identity
form and the WebUI API.
"""

from __future__ import annotations

MAX_NICKNAME_LEN = 40
