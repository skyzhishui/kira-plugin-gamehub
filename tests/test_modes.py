"""S2-S4 模式选项（modes）纯函数测试：归一、旧数据回填、切换校验。"""

from __future__ import annotations

from types import SimpleNamespace

import modes


def _seats(*kinds: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(kind=k) for k in kinds]


def _match(seats: list[SimpleNamespace], options: dict | None = None, kind: str = "gomoku") -> SimpleNamespace:
    return SimpleNamespace(
        seats=seats,
        log=[],
        kind=kind,
        mode_options=(options if options is not None else {}),
    )


# ----------------------------------------------------------------------
#  归一 / 旧数据回填
# ----------------------------------------------------------------------
def test_legacy_gomoku_ai_seat_migrates_to_turn_driver():
    """旧无选项 gomoku ai 座位局：读时迁移为 driver=turn（诺里模式无 game_agent）。"""
    options = modes.options_of(_match(_seats("human", "ai")))
    assert options == {
        "level": "intermediate",
        "mode": "nori",
        "role": "opponent",
        "driver": "turn",
        "difficulty": "",
        "delegation": False,
    }


def test_legacy_non_gomoku_ai_seat_keeps_agent_driver():
    """非五子棋/未知 kind：通用旧行为不变（保守 agent），不外溢 S2 语义。"""
    options = modes.options_of(_match(_seats("human", "ai"), kind="chess"))
    assert options["mode"] == "nori"
    assert options["driver"] == "agent"


def test_legacy_bot_seat_match_keeps_easy_teammate():
    options = modes.options_of(_match(_seats("human", "bot")))
    assert options["mode"] == "bot"
    assert options["role"] == "teammate"
    assert options["driver"] == "bot"
    assert options["difficulty"] == "easy"
    assert options["delegation"] is False


def test_new_nori_match_defaults_to_turn_driver():
    options = modes.normalize_mode_options({"mode": "nori"}, kind="gomoku")
    assert options["driver"] == "turn"
    assert options["difficulty"] == ""
    assert options["delegation"] is False


def test_bot_advanced_maps_agent_and_easy_maps_bot():
    advanced = modes.normalize_mode_options(
        {"mode": "bot", "difficulty": "advanced"},
        seats=_seats("human", "ai"),
    )
    assert advanced["driver"] == "agent"
    assert advanced["difficulty"] == "advanced"
    assert advanced["role"] == "teammate"

    easy = modes.normalize_mode_options(
        {"mode": "bot", "difficulty": "easy"},
        seats=_seats("human", "bot"),
    )
    assert easy["driver"] == "bot"
    assert easy["difficulty"] == "easy"


def test_delegation_only_valid_in_bot_mode():
    options = modes.normalize_mode_options(
        {"mode": "nori", "delegation": True},
    )
    assert options["delegation"] is False
    options = modes.normalize_mode_options(
        {"mode": "bot", "delegation": True},
    )
    assert options["delegation"] is True


def test_invalid_values_fall_back_safely():
    options = modes.normalize_mode_options(
        {"mode": "chaos", "driver": "psychic", "difficulty": "impossible"},
        seats=_seats("human", "ai"),
    )
    # 非法 mode 按座位回填；驱动按保守默认（ai 座位非 legacy → turn 新默认）
    assert options["mode"] == "nori"
    assert options["driver"] in ("agent", "turn")
    assert options["difficulty"] == ""


def test_nori_mode_never_bot_driver_and_bot_mode_never_turn():
    assert modes.normalize_mode_options({"mode": "nori", "driver": "bot"})["driver"] == "turn"
    assert modes.normalize_mode_options({"mode": "bot", "driver": "turn"})["driver"] == "bot"


# ----------------------------------------------------------------------
#  判据
# ----------------------------------------------------------------------
def test_is_directed_only_for_turn_driver():
    assert modes.is_directed(_match(_seats("human", "ai"), {"mode": "nori", "driver": "turn"}))
    assert not modes.is_directed(_match(_seats("human", "ai"), {"mode": "nori", "driver": "agent"}))


def test_trigger_seat_role_aware():
    # 对手局沿事件座位（legacy 判据一致）
    m = _match(_seats("human", "ai"), {"mode": "nori", "driver": "agent"})
    assert modes.trigger_seat(m, {"seat": 0}) == 0
    assert modes.is_wanted_trigger(m, 0) is True
    assert modes.is_wanted_trigger(m, 1) is False

    # 队友局以真实最后一手为准（easy 应手内嵌在 apply 里）
    teammate = _match(_seats("human", "bot"), {"mode": "bot", "difficulty": "easy"})
    teammate.log = [SimpleNamespace(seat=1, move="A1")]  # bot 应手
    assert modes.trigger_seat(teammate, {"seat": 0}) == 1
    assert modes.is_wanted_trigger(teammate, 1) is True
    assert modes.is_wanted_trigger(teammate, 0) is False


def test_seat_index_helpers():
    m = _match(_seats("human", "ai"))
    assert modes.human_seat_index(m) == 0
    assert modes.ai_seat_index(m) == 1
    assert modes.opponent_seat_index(m) == 1
    assert modes.bot_seat_index(m) is None


# ----------------------------------------------------------------------
#  切换校验
# ----------------------------------------------------------------------
def test_mode_switch_requires_compatible_seat_kind():
    ai_opponent = _match(_seats("human", "ai"), {"mode": "nori", "driver": "turn"})
    bot_opponent = _match(_seats("human", "bot"), {"mode": "bot", "difficulty": "easy"})

    # ai 座位：nori ↔ bot+advanced 允许；easy 拒绝（需要 bot 座位）
    options, error = modes.validate_mode_switch(ai_opponent, mode="bot", difficulty="easy")
    assert options is None and "不匹配" in (error or "")
    options, error = modes.validate_mode_switch(ai_opponent, mode="bot", difficulty="advanced")
    assert options is not None and error == ""
    assert options["driver"] == "agent"

    # bot 座位：easy 允许；nori/advanced 拒绝
    options, error = modes.validate_mode_switch(bot_opponent, mode="bot", difficulty="easy")
    assert options is not None and error == ""
    options, error = modes.validate_mode_switch(bot_opponent, mode="nori", difficulty="")
    assert options is None and "不匹配" in (error or "")


def test_validate_mode_switch_inherits_current_difficulty_when_omitted():
    """省略 difficulty / 同模式 no-op：继承当前难度，不降级 advanced。"""
    advanced = _match(
        _seats("human", "ai"),
        {"mode": "bot", "difficulty": "advanced", "delegation": False},
    )
    options, error = modes.validate_mode_switch(advanced, mode="bot", difficulty="")
    assert error == "" and options is not None
    assert options["difficulty"] == "advanced"
    assert options["driver"] == "agent"

    # 只改 mode 同值（web API 常用形态）
    options, error = modes.validate_mode_switch(advanced, mode="bot", difficulty="")
    assert error == "" and options is not None
    assert options["difficulty"] == "advanced"

    # delegation-only：mode 也不传
    options, error = modes.validate_mode_switch(advanced, mode="", difficulty="")
    assert error == "" and options is not None
    assert options["difficulty"] == "advanced"

    easy = _match(_seats("human", "bot"), {"mode": "bot", "difficulty": "easy"})
    options, error = modes.validate_mode_switch(easy, mode="bot", difficulty="")
    assert error == "" and options is not None
    assert options["difficulty"] == "easy"

    nori = _match(_seats("human", "ai"), {"mode": "nori", "driver": "turn"})
    options, error = modes.validate_mode_switch(nori, mode="", difficulty="")
    assert error == "" and options is not None
    assert options["driver"] == "turn"
    assert options["difficulty"] == ""


def test_mode_switch_preserves_delegation():
    m = _match(_seats("human", "ai"), {"mode": "bot", "difficulty": "advanced", "delegation": True})
    options, _error = modes.validate_mode_switch(m, mode="bot", difficulty="advanced")
    assert options is not None
    assert options["delegation"] is True


def test_mode_switch_rejects_unknown_mode():
    m = _match(_seats("human", "ai"), {"mode": "nori", "driver": "turn"})
    options, error = modes.validate_mode_switch(m, mode="chaos", difficulty="")
    assert options is None and "不支持的模式" in (error or "")
