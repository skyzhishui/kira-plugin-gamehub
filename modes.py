"""对局模式选项（S2-S4）：``role`` / ``driver`` / ``difficulty`` / ``delegation``。

把「诺里在这局里的身份、对手由谁驱动、难度、人类座位是否委托」建模为
**随对局快照持久化的模式选项**（``Match.mode_options``），旧数据（无选项
行）按座位构成做合理回填，行为与历史完全一致：

- 旧 ``ai`` 座位局 → ``{mode: nori, role: opponent, driver: agent}``：
  继续由 ``game_agent`` 驱动诺里走子（S2 之前的行为）；
- 旧 ``bot`` 座位局 → ``{mode: bot, role: teammate, driver: bot, difficulty: easy}``：
  纯规则陪练 + 诺里队友视角发言（与历史一致）。

新开局默认 ``{mode: nori, driver: turn}``：禁用 game_agent 对手驱动，人类
每手合法落子触发**强制行动通知**，诺里在回复回合内用会话级临时 ``play``
工具出招（S2）；``bot`` 模式下诺里是队友（只发言不落子），对手 ``advanced``
由 game_agent 驱动、``easy`` 仅规则 bot（S3）；``bot`` 模式可把人类座位
持久化委托给诺里代走（S4）。

纪律：

- 座位 kind 沿用引擎内部名（``human``/``ai``/``bot``），不在任何文案里
  露出「AI」字样；诺里座位的显示名仍是「诺里」，bot 座位是陪练名；
- 本模块是纯函数（不依赖房间层/引擎状态），供 main/room/talk/judge/
  context/tools/web_api 共用；
- 未知的选项键/取值一律归一到安全默认，绝不让坏数据炸掉读取路径。
"""

from __future__ import annotations

from typing import Any

# 模式：对诺里本人（S2 行动回合） / 对陪练（诺里队友视角）
MODE_NORI = "nori"
MODE_BOT = "bot"

# 诺里角色：opponent=对手（S2）；teammate=队友（S3/S4，不落子，委托时除外）
ROLE_OPPONENT = "opponent"
ROLE_TEAMMATE = "teammate"

# 对手座位驱动：turn=行动通知回合（S2）；agent=game_agent（旧/S3 advanced）；
# bot=规则 bot（S3 easy / 引擎兜底）
DRIVER_TURN = "turn"
DRIVER_AGENT = "agent"
DRIVER_BOT = "bot"

# 难度：nori 模式固定 standard；bot 模式 easy（纯 bot）/ advanced（game_agent）
DIFF_STANDARD = ""
DIFF_EASY = "easy"
DIFF_ADVANCED = "advanced"

# 选项白名单（读取时归一）
_MODES = (MODE_NORI, MODE_BOT)
_DRIVERS = (DRIVER_TURN, DRIVER_AGENT, DRIVER_BOT)
_DIFFS = (DIFF_STANDARD, DIFF_EASY, DIFF_ADVANCED)

# 全席位对齐（含人类），绝不以 kind=ai 猜测诺里身份。
LINEUPS = {
    "nori_agent": ["human", "turn", "agent"],
    "nori_bot": ["human", "turn", "bot"],
    "agent_bot": ["human", "agent", "bot"],
    "bot_bot": ["human", "bot", "bot"],
}


def _table_options(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    lineup = source.get("lineup", "nori_agent")
    if not isinstance(lineup, str) or lineup not in LINEUPS:
        raise ValueError("不支持的斗地主阵容")
    drivers = list(LINEUPS[lineup])
    nori = next((i for i, driver in enumerate(drivers) if driver == "turn"), None)
    coach = nori is None
    return {
        "schema": 2,
        "lineup": lineup,
        "seat_drivers": drivers,
        "nori_seat": nori,
        "nori_role": "coach" if coach else "player",
        "mode": MODE_BOT if coach else MODE_NORI,
        "role": ROLE_TEAMMATE if coach else ROLE_OPPONENT,
        "driver": (
            DRIVER_TURN if not coach
            else DRIVER_AGENT if "agent" in drivers else DRIVER_BOT
        ),
        "difficulty": (
            DIFF_ADVANCED if coach and "agent" in drivers
            else DIFF_EASY if coach else DIFF_STANDARD
        ),
        "delegation": source.get("delegation") is True and coach,
    }


def seat_driver_of(match: Any, seat: int) -> str:
    options = options_of(match)
    drivers = options.get("seat_drivers")
    if isinstance(drivers, list) and 0 <= seat < len(drivers):
        return drivers[seat]
    seats = _seats_of(match)
    if 0 <= seat < len(seats):
        kind = getattr(seats[seat], "kind", "")
        if kind in ("human", "bot"):
            return kind
    return str(options["driver"])


def nori_seat_index(match: Any) -> int | None:
    options = options_of(match)
    if "nori_seat" in options:
        return options["nori_seat"]
    return ai_seat_index(match) if options["mode"] == MODE_NORI else None


def _seats_of(match_or_seats: Any) -> list[Any]:
    """座位列表：接受 match 对象、座位列表或 ``{"seats": [...]}`` 字典。"""
    if match_or_seats is None:
        return []
    if isinstance(match_or_seats, dict):
        match_or_seats = match_or_seats.get("seats")
    if isinstance(match_or_seats, (list, tuple)):
        return list(match_or_seats)
    try:
        return list(getattr(match_or_seats, "seats", None) or [])
    except TypeError:
        return []


def _has_kind(seats: list[Any], kind: str) -> bool:
    return any(getattr(s, "kind", "") == kind for s in seats)


def _supports_turn_driver(kind: str | None) -> bool:
    """该 kind 是否声明支持「行动通知回合」（S2 turn 驱动）。

    能力来自游戏模块的 ``supports_turn_driver`` 声明（gomoku/reversi=True），
    不再写死 ``kind == "gomoku"``。未知/未加载 kind、模块声明缺失或读取
    异常一律保守返回 False（旧 ``agent`` 行为），绝不把 S2 语义外溢到未来
    游戏。
    """
    text = str(kind or "").strip().lower()
    if not text:
        return False
    try:
        from .engine.games import maybe_get
    except ImportError:
        # Standalone tests import this module as a top-level module (no package).
        try:
            from engine.games import maybe_get
        except Exception:  # noqa: BLE001 - 引擎未就绪按无能力处理
            return False
    except Exception:  # noqa: BLE001 - 引擎未就绪按无能力处理
        return False
    try:
        module = maybe_get(text)
    except Exception:  # noqa: BLE001 - 注册表异常按无能力处理
        return False
    return bool(getattr(module, "supports_turn_driver", False))


def normalize_mode_options(
    raw: Any = None,
    *,
    seats: Any = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """把任意输入归一为规范模式选项（dict，可 JSON 持久化）。

    ``raw`` 为空（旧数据/未指定）时按座位构成推断；给了 ``raw`` 则逐键
    校验，缺驱动时按 ``kind`` 决定默认。**kind 划界（按游戏能力声明）**：

    - 模块声明 ``supports_turn_driver=True``（gomoku / reversi）：legacy
      （无选项行）的 ai 座位局迁移为 ``driver=turn``（用户口径「与诺里对战
      不再有 game_agent 参与」，重启后既有 active 局即得新语义）；新开局
      缺驱动同样默认 turn；
    - 其他/未知 kind：保持通用旧行为——legacy 回填 ``agent``，缺驱动保守
      ``agent``，绝不把棋类的 S2 角色/触发器外溢到未来游戏。

    座位构成（``seats``）只在推断时使用。
    """
    if kind == "doudizhu":
        return _table_options(raw)
    seat_list = _seats_of(seats)
    has_bot = _has_kind(seat_list, "bot")
    has_ai = _has_kind(seat_list, "ai")
    source = raw if isinstance(raw, dict) and raw else None
    legacy = source is None
    # S2（turn 驱动/行动通知回合）按能力声明生效，不写死具体棋种。
    s2_kind = _supports_turn_driver(kind)

    raw_mode = str((source or {}).get("mode") or "").strip().lower()
    if raw_mode not in (MODE_NORI, MODE_BOT):
        # 旧数据回填：bot 座位局是陪练局；ai 座位局是诺里局。
        raw_mode = MODE_BOT if (has_bot and not has_ai) else MODE_NORI
    mode = raw_mode

    role = ROLE_OPPONENT if mode == MODE_NORI else ROLE_TEAMMATE

    raw_diff = str((source or {}).get("difficulty") or "").strip().lower()
    if raw_diff not in _DIFFS:
        raw_diff = ""

    raw_driver = str((source or {}).get("driver") or "").strip().lower()
    if raw_driver not in _DRIVERS:
        # 缺驱动：
        # - legacy（无选项行）：ai 座位局按历史行为回退——**声明
        #   supports_turn_driver 的棋种迁移为 turn**（S2 语义），其他 kind
        #   保守 agent；bot 局回退 bot；
        # - 非 legacy（部分写入）：声明能力的棋种默认 turn；其他 kind 保守
        #   agent（bot 局按显式难度推导 advanced=agent / easy=bot）。
        if mode == MODE_BOT:
            raw_driver = (
                DRIVER_AGENT if raw_diff == DIFF_ADVANCED else DRIVER_BOT
            )
        elif s2_kind:
            raw_driver = DRIVER_TURN
        else:
            raw_driver = DRIVER_AGENT
    driver = raw_driver
    if mode == MODE_NORI and driver == DRIVER_BOT:
        # nori 模式没有规则 bot 对手座位；防御归一到 turn。
        driver = DRIVER_TURN
    if mode == MODE_BOT and driver == DRIVER_TURN:
        # bot 模式的对手座位不由行动通知回合驱动（诺里是队友）。
        driver = DRIVER_BOT

    if mode == MODE_NORI:
        difficulty = DIFF_STANDARD
    elif driver == DRIVER_AGENT:
        difficulty = DIFF_ADVANCED
    else:
        difficulty = DIFF_EASY
    if mode == MODE_BOT and raw_diff in (DIFF_EASY, DIFF_ADVANCED):
        # 显式难度与驱动一致才保留（冗余校验以驱动为准）。
        if driver == DRIVER_AGENT and raw_diff == DIFF_ADVANCED:
            difficulty = DIFF_ADVANCED
        if driver == DRIVER_BOT and raw_diff == DIFF_EASY:
            difficulty = DIFF_EASY
    delegation = bool((source or {}).get("delegation")) and mode == MODE_BOT

    result = {
        "mode": mode,
        "role": role,
        "driver": driver,
        "difficulty": difficulty,
        "delegation": delegation,
    }
    if kind in ("gomoku", "reversi"):
        level = (source or {}).get("level", "intermediate")
        result["level"] = level if level in ("beginner", "intermediate", "advanced") else "intermediate"
    return result


def options_of(match: Any) -> dict[str, Any]:
    """读对局的规范模式选项（``Match.mode_options``；缺失按旧数据回填）。

    kind 从对局本身读取：legacy 且模块声明 ``supports_turn_driver``（如
    gomoku/reversi）的 ai 座位局迁移为 turn（S2 语义，重启恢复即得新行为）；
    其他 kind 保守保持 agent/bot 历史行为。
    """
    raw = getattr(match, "mode_options", None)
    return normalize_mode_options(
        raw, seats=match, kind=getattr(match, "kind", None)
    )


def mode_of(match: Any) -> str:
    return str(options_of(match).get("mode") or MODE_NORI)


def role_of(match: Any) -> str:
    return str(options_of(match).get("role") or ROLE_OPPONENT)


def driver_of(match: Any) -> str:
    return str(options_of(match).get("driver") or DRIVER_AGENT)


def difficulty_of(match: Any) -> str:
    return str(options_of(match).get("difficulty") or DIFF_STANDARD)


def delegation_of(match: Any) -> bool:
    return bool(options_of(match).get("delegation"))


def is_directed(match: Any) -> bool:
    """是否由「行动通知回合」驱动（S2）。

    directed 局的走子通知、watchdog 与 play 授权全部归 TurnDirector 所有，
    table_talk / notice judge / game_agent 驱动必须跳过。
    """
    return driver_of(match) == DRIVER_TURN


def is_delegated(match: Any) -> bool:
    """人类座位是否处于持久化委托状态（S4；仅 teammate 模式）。"""
    return delegation_of(match)


def human_seat_index(match: Any) -> int | None:
    for index, seat in enumerate(_seats_of(match)):
        if getattr(seat, "kind", "") == "human":
            return index
    return None


def ai_seat_index(match: Any) -> int | None:
    for index, seat in enumerate(_seats_of(match)):
        if getattr(seat, "kind", "") == "ai":
            return index
    return None


def bot_seat_index(match: Any) -> int | None:
    for index, seat in enumerate(_seats_of(match)):
        if getattr(seat, "kind", "") == "bot":
            return index
    return None


def opponent_seat_index(match: Any) -> int | None:
    """对手座位：human 座位之外的非 human 座位（ai 或 bot）。"""
    human = human_seat_index(match)
    for index, seat in enumerate(_seats_of(match)):
        if index == human:
            continue
        if getattr(seat, "kind", "") in ("ai", "bot"):
            return index
    return None


def last_mover(match: Any, event: Any = None) -> int:
    """实际最后一手落子的座位（easy 陪练应手内嵌在同一次 apply 里）。

    事件里的 ``seat`` 是**发起动作**的座位；规则陪练的应手由引擎在同一
    次 apply 内自动完成，最后一条 log 才是真正的最新落子。通知触发、
    视角归属都以最新 log 为准；空 log 时回退事件座位。
    """
    log = getattr(match, "log", None) or ()
    if log:
        try:
            return int(getattr(log[-1], "seat", -1))
        except (TypeError, ValueError):
            pass
    try:
        return int(event.get("seat", -1))
    except (TypeError, ValueError, AttributeError):
        return -1


def trigger_seat(match: Any, event: Any = None) -> int:
    """评论触发判据用的座位（角色感知）。

    - 队友局：真实最后一手（easy 陪练应手内嵌在同一次 apply，事件座位
      恒为发起 apply 的人类座位，看不出陪练是否已应手）；
    - 对手局（含 legacy）：沿事件座位（与历史判据一致；对手局的 ai 座位
      落子事件自带真实座位，不存在内嵌应手）。
    """
    if role_of(match) == ROLE_TEAMMATE:
        return last_mover(match, event)
    try:
        return int(event.get("seat", -1))
    except (TypeError, ValueError, AttributeError):
        return last_mover(match, event)


def is_wanted_trigger(match: Any, mover: int) -> bool:
    """该局的评论触发是否落在 mover 上（角色感知）。

    - 对手局（诺里参战）：人类落子触发（人类应手后轮到诺里/评论）；
    - 队友局：对手应手触发（诺里站人类一边，评论对手的回应）。
    终局/开局由调用方单独放行，不经过本判据。
    """
    seats = _seats_of(match)
    if not (0 <= mover < len(seats)):
        return False
    mover_kind = getattr(seats[mover], "kind", "")
    if role_of(match) == ROLE_TEAMMATE:
        return mover_kind != "human"
    return mover_kind == "human"


def bound_seat(bound: Any, match: Any) -> int | None:
    """绑定回合上下文 → 该回合的座位（通知工具的视角）。

    显式 ``seat`` 优先；缺省按角色取默认（对手局取诺里座位，队友局取
    人类座位）。解析不出返回 None（观战视图）。
    """
    try:
        seat = int(bound.get("seat"))
    except (AttributeError, TypeError, ValueError):
        seat = -1
    if 0 <= seat < len(_seats_of(match)):
        return seat
    if role_of(match) == ROLE_TEAMMATE:
        return human_seat_index(match)
    return ai_seat_index(match)


def seat_kind_compatible(mode: str, difficulty: str, seats: Any) -> bool:
    """模式/难度与既有座位构成是否兼容（切换校验用）。

    - ``nori`` 模式：对手座位必须是 ``ai``（turn/agent 都驱动 ai 座位）；
    - ``bot`` + advanced：对手座位必须是 ``ai``（game_agent 只驱动 ai）；
    - ``bot`` + easy：对手座位必须是 ``bot``（纯规则陪练）。
    """
    seat_list = _seats_of(seats)
    if not seat_list:
        return True
    opponent = opponent_seat_index({"seats": seat_list})
    if opponent is None:
        return False
    kind = getattr(seat_list[opponent], "kind", "")
    if mode == MODE_NORI or (mode == MODE_BOT and difficulty == DIFF_ADVANCED):
        return kind == "ai"
    return kind == "bot"


def validate_mode_switch(
    match: Any, *, mode: str, difficulty: str
) -> tuple[dict[str, Any] | None, str]:
    """校验对局中途的模式/难度切换；返回 ``(新选项, 错误)``。

    座位构成在开局时固定：切到与座位不兼容的模式一律拒绝（返回明确错误
    文案），避免「easy 请求落到 ai 座位却被静默改成 advanced」这类语义
    漂移。委托位不受切换影响（原值保留）。

    省略项按 **当前值** 继承（delegation-only / 只切 mode / 无变化都
    不降级当前难度）：``difficulty`` 为空且 mode 未变（或未给）时保留
    ``current.difficulty``，例如 advanced 陪练局只切委托仍保持 advanced。
    """
    current = options_of(match)
    if getattr(match, "kind", "") == "doudizhu":
        if (mode and mode != current["mode"]) or (difficulty and difficulty != current["difficulty"]):
            return None, "斗地主阵容开局后不可切换，请重开一局"
        return current, ""
    seats = match
    requested = str(mode or "").strip().lower()
    requested_mode = requested or str(current.get("mode") or "")
    requested_diff = str(difficulty or "").strip().lower()
    if not requested_diff and requested_mode == current.get("mode"):
        # no-op / 只改委托：继承当前难度，绝不由驱动推断降级。
        requested_diff = str(current.get("difficulty") or "")
    normalized = normalize_mode_options(
        {
            "mode": requested_mode,
            "difficulty": requested_diff,
            "delegation": current.get("delegation"),
            "level": current.get("level", "intermediate"),
        },
        seats=seats,
        kind=getattr(match, "kind", None),
    )
    if requested and requested != normalized["mode"]:
        return None, f"不支持的模式：{requested}"
    if (
        requested_diff
        and normalized["mode"] == MODE_BOT
        and requested_diff != normalized["difficulty"]
    ):
        return None, (
            f"难度 {requested_diff} 与对手驱动不一致"
            "（advanced=对手由棋手驱动，easy=纯规则陪练）"
        )
    if not seat_kind_compatible(
        normalized["mode"], normalized["difficulty"], seats
    ):
        return None, (
            "该模式与本局座位构成不匹配"
            "（对 bot 本人/进阶对手需要对手执 ai 座位，规则陪练需要 bot 座位），"
            "不能在对局中途切换"
        )
    return normalized, ""
