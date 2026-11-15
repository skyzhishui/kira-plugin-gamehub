"""Test bootstrap for the KiraAI gamehub plugin.

Puts the plugin root on ``sys.path`` so the host-independent ``engine``
package can be imported as a top-level package (``from engine import ...``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]

if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


@pytest.fixture(autouse=True)
def _instant_bot_pacing(monkeypatch):
    """规则陪练的拟人化延迟在测试里归零。

    生产默认是 1-2 秒（``bot_move_delay_min_ms`` / ``max_ms``），测试若
    真等这些秒数会又慢又抖。这里只把延迟压到 0：房间层仍走「延迟后步进」
    的同一条代码路径，断言用 ``wait_until`` 等任务落地即可。
    """
    try:
        import room

        monkeypatch.setattr(room, "DEFAULT_BOT_DELAY_S", (0.0, 0.0))
    except ImportError:  # pragma: no cover - room always importable in this suite
        pass
    try:
        import game_agent

        monkeypatch.setattr(game_agent, "BOT_DELAY_S", (0.0, 0.0))
    except ImportError:  # pragma: no cover - game_agent always importable here
        pass
    try:
        from host import settings as host_settings

        monkeypatch.setitem(host_settings.DEFAULTS, "bot_move_delay_min_ms", 0)
        monkeypatch.setitem(host_settings.DEFAULTS, "bot_move_delay_max_ms", 0)
    except ImportError:  # pragma: no cover - host layer always importable here
        pass
