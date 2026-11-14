"""Test bootstrap for the KiraAI gamehub plugin.

Puts the plugin root on ``sys.path`` so the host-independent ``engine``
package can be imported as a top-level package (``from engine import ...``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]

if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
