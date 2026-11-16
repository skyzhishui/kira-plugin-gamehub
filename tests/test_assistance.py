"""Assistance helper contract: delegated human, level override, ranking.

``assistance.py`` sits between the match snapshot and the prompt/context layer.
Its contract is narrow and must stay safe for legacy snapshots:

- **Delegated human only**: advanced assistance for a delegated match is bound
  to the delegated *human* seat and never spills over to the opponent or any
  other seat (numeric-string seats come straight from persisted JSON).
- **Beginner override**: ``level=beginner`` disables analysis for regular
  seats, except for the delegated human seat (the host plays that seat).
- **Ranking / recommendation**: ``ranked_moves`` delegates to the game module's
  heuristic ranker; ``recommendation_text`` only renders for gomoku/reversi,
  only for seats that are actually waiting, and only at advanced level or for
  the delegated human seat.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import assistance
from engine import NewMatchOptions, NewSeat, create_match, games
from engine.match import Match


def _gomoku(match_id: str = "m-assist", mode_options: dict | None = None) -> Match:
    seats = [
        NewSeat(kind="human", token="h-token", name="小白", joined=True),
        NewSeat(kind="bot", token="", name="陪练"),
    ]
    return create_match(
        NewMatchOptions(
            id=match_id,
            kind="gomoku",
            seats=seats,
            seed=7,
            now=1_700_000_000_000,
            mode_options=mode_options or {},
        )
    )


def _delegated(match: Match, *, level: str = "intermediate") -> Match:
    """A delegated bot-mode match (host is teammate, human seat delegated).

    ``role`` is derived from ``mode`` by ``modes.normalize_mode_options``; it
    is not written here, mirroring what a real delegated snapshot stores.
    """
    return replace(
        match,
        mode_options={
            "mode": "bot",
            "driver": "bot",
            "difficulty": "easy",
            "delegation": True,
            "level": level,
        },
    )


def _with_level(match: Match, level: str) -> Match:
    return replace(match, mode_options={**match.mode_options, "level": level})


def test_level_reads_the_explicit_value_and_falls_back_to_intermediate():
    assert assistance.level_of(_gomoku()) == "intermediate"
    assert assistance.level_of(_with_level(_gomoku(), "beginner")) == "beginner"
    assert assistance.level_of(_with_level(_gomoku(), "advanced")) == "advanced"
    # normalize_mode_options drops an unknown level back to intermediate.
    unknown = replace(_gomoku(), mode_options={"level": "grandmaster"})
    assert assistance.level_of(unknown) == "intermediate"


def test_analysis_gating_by_level_without_delegation():
    beginner = _with_level(_gomoku(), "beginner")
    # Beginner: no analysis for any seat, including the human one and the
    # seat-less default used by context callers.
    assert assistance.analysis_enabled(beginner, 0) is False
    assert assistance.analysis_enabled(beginner, 1) is False
    assert assistance.analysis_enabled(beginner) is False
    # Intermediate and advanced keep analysis on.
    assert assistance.analysis_enabled(_with_level(_gomoku(), "intermediate"), 0) is True
    assert assistance.analysis_enabled(_with_level(_gomoku(), "advanced"), 1) is True


def test_delegated_assistance_binds_to_the_human_seat_only():
    """Beginner override yields to the delegated seat; nothing else gets it."""
    match = _delegated(_gomoku(), level="beginner")
    assert assistance.level_of(match) == "beginner"

    # The delegated human seat still needs threat analysis and candidates.
    assert assistance.analysis_enabled(match, 0) is True
    assert assistance.recommendation_text(match, 0).startswith(
        "Bot top-3 recommendations"
    )
    # Opponent / out-of-range / seat-less never get the delegated bonus.
    for seat in (1, 2, None):
        assert assistance.analysis_enabled(match, seat) is False, seat
        assert assistance.recommendation_text(match, seat) == "", seat

    # Legacy snapshots may carry the seat as a numeric string; bad values are
    # treated as "not delegated" instead of raising.
    assert assistance.analysis_enabled(match, "0") is True
    assert assistance.analysis_enabled(match, "1") is False
    assert assistance.analysis_enabled(match, "seat") is False
    assert assistance.analysis_enabled(match, object()) is False


def test_delegation_requires_a_bot_mode_teammate_snapshot():
    # Delegation only survives in bot mode; ``role`` is derived from it, so an
    # inconsistent snapshot cannot grant delegated assistance to an opponent.
    delegated_nori = replace(
        _gomoku(),
        mode_options={
            "mode": "nori",
            "role": "opponent",
            "delegation": True,
            "level": "beginner",
        },
    )
    assert assistance.analysis_enabled(delegated_nori, 0) is False
    # Control: the same level from a real bot-mode delegation still counts.
    assert assistance.analysis_enabled(_delegated(_gomoku(), level="beginner"), 0)


@pytest.mark.parametrize("kind", ["gomoku", "reversi"])
def test_ranked_moves_delegates_to_the_module_ranker_and_caps_the_list(kind):
    seats = [
        NewSeat(kind="human", token="h", name="小白", joined=True),
        NewSeat(kind="bot", token="", name="陪练"),
    ]
    match = create_match(
        NewMatchOptions(id=f"m-{kind}", kind=kind, seats=seats, seed=3, now=0)
    )

    moves = assistance.ranked_moves(kind, match.state, 0, limit=3)
    expected = games.get(kind).ranked_moves(match.state, 0)[:3]
    assert moves == expected
    assert moves, "the opening position must yield at least one candidate"
    assert all({"move", "score"} <= set(item) for item in moves)
    # The limit is honoured for the same position.
    assert len(assistance.ranked_moves(kind, match.state, 0, limit=1)) <= 1


def test_recommendation_text_gates_on_level_kind_and_turn():
    match = _gomoku()

    # Level: only advanced (or the delegated seat, covered above) renders.
    assert assistance.recommendation_text(_with_level(match, "beginner"), 0) == ""
    assert assistance.recommendation_text(_with_level(match, "intermediate"), 0) == ""
    text = assistance.recommendation_text(_with_level(match, "advanced"), 0)
    assert text.startswith("Bot top-3 recommendations")
    assert "1. " in text

    # Kind: only the two ranked board games have a recommendation surface.
    advanced = _with_level(match, "advanced")
    for kind in ("doudizhu", "chess", "unknown"):
        assert assistance.recommendation_text(replace(advanced, kind=kind), 0) == ""

    # Turn: seat 1 does not move first on a fresh gomoku board.
    assert games.get("gomoku").waiting_on(advanced.state) == [0]
    assert assistance.recommendation_text(advanced, 1) == ""
