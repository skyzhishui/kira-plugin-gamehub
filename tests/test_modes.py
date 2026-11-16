"""S2-S4 模式选项（modes）纯函数测试：归一、旧数据回填、切换校验。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

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
    """旧无选项 gomoku ai 座位局：读时迁移为 driver=turn（诺里模式无 game_agent）。

    新开局显式 ``mode=nori`` 必须落在同一默认上；``level`` 只在棋类出现。
    """
    legacy = modes.options_of(_match(_seats("human", "ai")))
    assert legacy == {
        "level": "intermediate",
        "mode": "nori",
        "role": "opponent",
        "driver": "turn",
        "difficulty": "",
        "delegation": False,
    }
    explicit = modes.normalize_mode_options({"mode": "nori"}, kind="gomoku")
    assert explicit["driver"] == "turn"
    assert explicit["difficulty"] == ""
    assert explicit["delegation"] is False


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
    # 非法 mode 按座位回填；未知驱动/难度一律归零，不留垃圾值。
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
    # A persisted delegated teammate match is never turn-directed.
    delegated = _match(
        _seats("human", "ai"),
        {"mode": "bot", "driver": "agent", "difficulty": "advanced", "delegation": True},
    )
    assert modes.is_directed(delegated) is False
    assert modes.is_delegated(delegated) is True


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

    # delegation-only：mode 也不传（web API 常用形态）
    options, error = modes.validate_mode_switch(advanced, mode="", difficulty="")
    assert error == "" and options is not None
    assert options["difficulty"] == "advanced"
    assert options["driver"] == "agent"

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


# ----------------------------------------------------------------------
#  斗地主阵容：写入口严格、读路径安全归一（不再裸 raise）
# ----------------------------------------------------------------------
def _table(kinds=("human", "ai", "bot"), **options: object) -> SimpleNamespace:
    return _match(_seats(*kinds), options, kind="doudizhu")


def test_doudizhu_unknown_lineup_raises_on_write_path():
    """New-match entry keeps rejecting bad lineups with a clear ValueError."""
    with pytest.raises(ValueError):
        modes.normalize_mode_options({"lineup": "chaos"}, kind="doudizhu")
    with pytest.raises(ValueError):
        # Read inside a new-match write probe (strict default) is strict too.
        modes.normalize_mode_options({"lineup": 42}, kind="doudizhu")


def test_doudizhu_persisted_bad_lineup_normalizes_once_per_value(caplog, monkeypatch):
    """A persisted bad lineup must not blow up every view/status read.

    Seat kinds identify the lineup, so a running game keeps its semantics; an
    ambiguous ``["human", "ai", "bot"]`` keeps the host as the active player.
    """
    monkeypatch.setattr(modes, "_warned_lineups", set())
    match = _table(("human", "ai", "ai"), lineup="chaos")
    with caplog.at_level(logging.WARNING, logger="modes"):
        options = modes.options_of(match)
    assert options["lineup"] == "nori_agent"  # playable default, not a crash
    assert options["seat_drivers"] == ["human", "turn", "agent"]
    assert any("斗地主阵容" in r.getMessage() for r in caplog.records)
    # Warn once per bad value, not on every read.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="modes"):
        modes.options_of(match)
    assert not caplog.records

    bot_bot = _table(("human", "bot", "bot"), lineup="oops")
    options = modes.options_of(bot_bot)
    assert options["lineup"] == "bot_bot"
    assert options["mode"] == modes.MODE_BOT
    assert options["difficulty"] == modes.DIFF_EASY

    nori_bot = _table(("human", "ai", "bot"), lineup=None)
    assert modes.options_of(nori_bot)["lineup"] == "nori_bot"
    assert modes.role_of(nori_bot) == modes.ROLE_OPPONENT


def test_doudizhu_valid_lineups_are_unchanged():
    """The strict/lenient split must not alter any known lineup mapping."""
    expected = {
        "nori_agent": ("nori", "opponent", "turn", ""),
        "nori_bot": ("nori", "opponent", "turn", ""),
        "agent_bot": ("bot", "teammate", "agent", "advanced"),
        "bot_bot": ("bot", "teammate", "bot", "easy"),
    }
    for lineup, (mode, role, driver, difficulty) in expected.items():
        options = modes.normalize_mode_options({"lineup": lineup}, kind="doudizhu")
        assert (
            options["mode"],
            options["role"],
            options["driver"],
            options["difficulty"],
        ) == (mode, role, driver, difficulty), lineup
        assert options["lineup"] == lineup
