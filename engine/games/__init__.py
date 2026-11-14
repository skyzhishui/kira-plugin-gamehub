"""游戏注册表：游戏模块在此登记，对局层/大厅/工具只依赖 registry。

新增游戏 = 写 ``engine/games/<kind>.py``（或非公共代码/资源独立的
``engine/games/<kind>/`` 包）+ 在 ``_BUILDERS`` 加一行 + 页面
``web/games/<kind>.js`` + 注册表一行（设计 §5.2）。
"""

from __future__ import annotations

from typing import Any

from ..types import CreateContext, GameModule

from .gomoku import gomoku
from .reversi import reversi
from .doudizhu import doudizhu

_MODULES: dict[str, GameModule] = {}


def register(module: GameModule) -> None:
    _MODULES[module.kind] = module


def get(kind: str) -> GameModule:
    module = _MODULES.get(kind)
    if module is None:
        raise KeyError(f"未知游戏: {kind}")
    return module


def maybe_get(kind: str) -> GameModule | None:
    return _MODULES.get(kind)


def all_modules() -> list[GameModule]:
    return list(_MODULES.values())


def create_context(*, seed: int, players: int, options: dict[str, str]) -> CreateContext:
    return CreateContext(seed=seed, players=players, options=options)


# 三人桌阵容标签模板：``{host}`` = 宿主主 agent（跑行动通知回合的那个 AI），
# ``{agent}`` = 插件自有的 game_agent。两边都不写死名字——配置改了标签跟着变。
LINEUP_TEMPLATES = (
    ("nori_agent", "{host} + {agent}"),
    ("nori_bot", "{host} + 陪练"),
    ("agent_bot", "{agent} + 陪练（{host}队友）"),
    ("bot_bot", "两位陪练（{host}队友）"),
)

# 调用方拿不到名字时的中性称呼（绝不回落到某个写死的人设名）。
# 正常路径一定带真实名字：宿主名来自宿主当前激活人设，agent 名来自配置。
NEUTRAL_HOST_NAME = "AI"
NEUTRAL_AGENT_NAME = "搭档"


def lineup_labels(host_name: str = "", agent_name: str = "") -> list[dict[str, str]]:
    """Render the three-seat lineup labels for the given identities."""
    host = str(host_name or "").strip() or NEUTRAL_HOST_NAME
    agent = str(agent_name or "").strip() or NEUTRAL_AGENT_NAME
    return [
        {
            "value": value,
            "label": template.replace("{host}", host).replace("{agent}", agent),
        }
        for value, template in LINEUP_TEMPLATES
    ]


def catalog(host_name: str = "", agent_name: str = "") -> list[dict[str, Any]]:
    """大厅/``game_list`` 用的游戏清单（不含规则长文）。

    Only games that are actually playable are listed: an entry the lobby shows
    but cannot start (``ready=False``) is a dead "筹备中" button, which is
    exactly the kind of non-entry we do not want in the lobby. The capability
    fields below let the front end decide what it may offer, so no client-side
    ``kind === ...`` allow-list is needed.
    """
    return [
        {
            "kind": m.kind,
            "name": m.name,
            "family": m.family,
            "blurb": m.blurb,
            "ready": m.ready,
            "players": {
                "min": m.players.min,
                "max": m.players.max,
                "default": m.players.default,
            },
            "options": [
                {
                    "key": o.key,
                    "label": o.label,
                    "choices": o.choices,
                    "default": o.default,
                }
                for o in m.options
            ],
            "move_help": m.move_help,
            "tempo": m.tempo,
            "has_bot": type(m).bot is not GameModule.bot,
            "has_key_events": type(m).key_events is not GameModule.key_events,
            "has_chat_context": type(m).chat_context is not GameModule.chat_context,
            # 能力声明：前端/模式据此决定是否支持「行动通知回合」等模式，
            # 不再维护 ``kind === "gomoku"`` 白名单。
            "supports_turn": bool(getattr(m, "supports_turn_driver", False)),
            **({"lineups": lineup_labels(host_name, agent_name)}
               if m.kind == "doudizhu" else {}),
        }
        for m in all_modules()
        if m.ready
    ]


register(gomoku)
register(reversi)
register(doudizhu)

__all__ = [
    "all_modules",
    "catalog",
    "create_context",
    "get",
    "maybe_get",
    "register",
]
