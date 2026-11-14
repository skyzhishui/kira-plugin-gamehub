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


def catalog() -> list[dict[str, Any]]:
    """大厅/``game_list`` 用的游戏清单（不含规则长文）。"""
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
            **({"lineups": [
                {"value": "nori_agent", "label": "诺里 + 当前agent"},
                {"value": "nori_bot", "label": "诺里 + 陪练"},
                {"value": "agent_bot", "label": "当前agent + 陪练（诺里队友）"},
                {"value": "bot_bot", "label": "两位陪练（诺里队友）"},
            ]} if m.kind == "doudizhu" else {}),
        }
        for m in all_modules()
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
