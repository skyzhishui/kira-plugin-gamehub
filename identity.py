"""页面玩家身份：user_id / 昵称 / 身份映射展示名。

页面固定 web 会话的 ``user_id`` 就是座位与聊天归属；昵称只影响棋桌上的
显示名。优先级（从高到低）：

1. ``IDENTITY_MAPPING``（``config/identity_mapping.toml`` 的 ``display_name``）
2. 游戏中心主页保存的昵称
3. 引擎默认名 ``Human``

``user_id`` 不参与显示名优先级，但决定 web 会话 id 与身份映射查询键。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WEB_USERID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_NICKNAME_LEN = 40
# M2.2 之前页面座位硬编码 actor="webui"；启动/改身份时迁移到真实 user_id。
LEGACY_PAGE_ACTOR = "webui"


@dataclass(frozen=True)
class PlayerIdentity:
    """页面玩家身份（source 仅用于 UI 说明称呼来源）。"""

    user_id: str
    nickname: str
    mapped_name: str
    effective_name: str
    source: str  # mapping | nickname | default

    def to_public(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "mapped_name": self.mapped_name,
            "effective_name": self.effective_name,
            "source": self.source,
        }


def resolve_player_identity(
    user_id: str,
    nickname: str,
    mapped_name: str,
) -> PlayerIdentity:
    """套用「映射 > 昵称 > 默认」的称呼优先级（纯函数，便于测试）。"""
    uid = str(user_id or "").strip()
    nick = str(nickname or "").strip()[:MAX_NICKNAME_LEN]
    mapped = str(mapped_name or "").strip()
    if mapped:
        effective, source = mapped, "mapping"
    elif nick:
        effective, source = nick, "nickname"
    else:
        effective, source = "Human", "default"
    return PlayerIdentity(
        user_id=uid,
        nickname=nick,
        mapped_name=mapped,
        effective_name=effective,
        source=source,
    )
