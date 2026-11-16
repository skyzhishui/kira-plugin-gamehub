"""Status derivation and snapshot-boundary regressions.

Covers the review findings that live in ``engine/match.py`` / ``engine/types.py``:

- **P1-3**: a corrupt ``resigned`` value must stay terminal (never resurrect the
  match as active) and never crash status/view derivation.
- **P1-4**: doudizhu resignation is decided by *camp*, not by "everyone but the
  resigner"; a farmer resigning must not hand the win to their partner.
- **P2 bare-index family**: corrupt persisted state (bad seat index, bad win
  line, wrong state shape) derives a terminal status instead of a 500.
- **D-25**: ``bump()`` keeps runtime side fields (``plan``) via ``replace``.

The deserialization matrix pins the one invariant that matters at the boundary:
a malformed snapshot rejects the whole row instead of silently filtering it,
because filtering would shift seat indices and hand a token to the wrong player.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from engine import (
    NewMatchOptions,
    NewSeat,
    apply_match_action,
    create_match,
    status_of,
    view_match,
)
from engine.match import RESIGNED_INVALID, Match


def _gomoku(seat_kinds=("human", "ai"), match_id="m-status"):
    seats = [
        NewSeat(kind=kind, token=f"tok{i}", name=f"P{i}")
        for i, kind in enumerate(seat_kinds)
    ]
    return create_match(
        NewMatchOptions(id=match_id, kind="gomoku", seats=seats, seed=7, now=1_000)
    )


def _doudizhu(match_id="dz-status"):
    return create_match(
        NewMatchOptions(
            id=match_id,
            kind="doudizhu",
            seats=[
                NewSeat("human", "tok-h", name="H"),
                NewSeat("ai", "", name="A"),
                NewSeat("bot", "", name="B"),
            ],
            seed=7,
            now=1_000,
            mode_options={"lineup": "nori_agent"},
            autoplay_bots=False,
        )
    )


def _landlord_state(match: Match, landlord: int = 0) -> Match:
    """Drive the bidding until ``landlord`` holds the role."""
    for _ in range(3):
        seat = status_of(match).waiting_on[0]
        move = "bid 3" if seat == landlord else "pass"
        result = apply_match_action(match, seat, {"type": "move", "move": move}, 2_000)
        assert result.ok, result.error
        match = result.match
        if match.state.get("landlord") is not None:
            return match
    raise AssertionError("landlord was never decided")


def _assert_terminal(match: Match) -> None:
    status = status_of(match)
    assert status.outcome is not None  # terminal: never resurrected as active
    assert status.waiting_on == []


# ----------------------------------------------------------------------
#  P1-3: a corrupt ``resigned`` value stays terminal
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [99, -1, True, "x", 3.5, "99"])
def test_corrupt_resigned_value_is_terminal_not_active_or_crash(bad) -> None:
    """``resigned`` out of range / wrong type must not IndexError or read active."""
    _assert_terminal(replace(_gomoku(), resigned=bad))


def test_corrupt_resigned_is_logged_and_not_echoed_in_the_view(caplog) -> None:
    # Distinct id: the warning is emitted once per match, so reusing the
    # parametrized match id above would be suppressed by that dedup.
    match = replace(_gomoku(match_id="m-status-warn"), resigned=99)
    with caplog.at_level(logging.WARNING, logger="engine.match"):
        view = view_match(match, None)
    assert view["status"]["outcome"] is not None  # page view stays renderable
    assert view["resigned"] is None  # the garbage seat is not echoed back
    assert any("认输座位无法解析" in r.getMessage() for r in caplog.records)


def test_legacy_none_resigned_with_log_entry_stays_terminal() -> None:
    """A snapshot whose column was lost but whose log says "resign" is terminal.

    ``resigned=None`` alone would be read as "active"; the trailing resign log
    entry is what must keep the match over (never revived as active).
    """
    resigned = apply_match_action(_gomoku(), 1, {"type": "resign"}, 2_000)
    assert resigned.ok
    legacy = replace(resigned.match, resigned=None)
    status = status_of(legacy)
    assert status.outcome is not None
    assert status.result_text == "P1 认输"
    assert view_match(legacy, None)["resigned"] == 1


def test_active_match_and_unknown_kind_do_not_raise() -> None:
    status = status_of(_gomoku())
    assert status.outcome is None
    assert status.waiting_on == [0]

    # A row for an unregistered game must not 500 the status derivation.
    unknown = replace(_gomoku(), kind="future-game", id="m-unknown")
    _assert_terminal(unknown)
    view = view_match(unknown, None)
    assert view["view"] == {}
    assert view["status"]["outcome"] is not None


# ----------------------------------------------------------------------
#  P1-4: doudizhu resignation is decided by camp
# ----------------------------------------------------------------------


def test_farmer_resign_never_wins_for_the_partner() -> None:
    """Farmer resigns -> the landlord wins alone; the other farmer is a loser."""
    match = _landlord_state(_doudizhu(), landlord=0)
    assert match.state["landlord"] == 0
    resigned = apply_match_action(match, 1, {"type": "resign"}, 3_000)
    assert resigned.ok
    status = status_of(resigned.match)
    assert status.outcome is not None
    assert status.outcome.winners == [0]
    assert status.outcome.text == "农民认输 · 地主胜"
    assert status.result_text == "A 认输"


def test_landlord_resign_loses_to_both_farmers() -> None:
    match = _landlord_state(_doudizhu(), landlord=0)
    resigned = apply_match_action(match, 0, {"type": "resign"}, 3_000)
    assert resigned.ok
    status = status_of(resigned.match)
    assert status.outcome is not None
    assert status.outcome.winners == [1, 2]
    assert status.outcome.text == "地主认输 · 农民胜"


def test_resign_during_bidding_is_terminal_and_never_names_the_resigner() -> None:
    """No landlord yet: still terminal, and the resigner is never a winner."""
    match = _doudizhu()
    assert match.state["landlord"] is None
    resigned = apply_match_action(match, 2, {"type": "resign"}, 2_000)
    assert resigned.ok
    status = status_of(resigned.match)
    assert status.outcome is not None
    assert status.outcome.winners == [0, 1]


def test_two_seat_resign_keeps_legacy_rule() -> None:
    """Games without a camp hook keep "everyone but the resigner wins"."""
    resigned = apply_match_action(_gomoku(), 0, {"type": "resign"}, 2_000)
    assert resigned.ok
    status = status_of(resigned.match)
    assert status.outcome is not None
    assert status.outcome.winners == [1]
    assert status.outcome.text == "认输"


# ----------------------------------------------------------------------
#  P2: corrupt persisted state must not 500 forever
# ----------------------------------------------------------------------


def test_corrupt_state_shape_derives_terminal_status() -> None:
    """``outcome()``/``waiting_on()`` raising on a bad row -> terminal, not 500."""
    broken = replace(_gomoku(), state={"size": 15, "cells": []})
    _assert_terminal(broken)

    # A win line pointing outside the board must not be indexed blindly.
    bad_win = replace(_gomoku(), state={**_gomoku().state, "winLine": [99_999]})
    status = status_of(bad_win)
    assert status.outcome is not None
    assert status.outcome.winners == []


# ----------------------------------------------------------------------
#  Deserialization: reject the row, never remap seats
# ----------------------------------------------------------------------


def test_from_dict_rejects_a_malformed_seat_instead_of_filtering() -> None:
    """A bad seat element must reject the row, never shift indices/tokens.

    Filtering a non-dict seat out of a two-seat list would silently promote the
    second seat to index 0 and hand its token to the wrong player. The same
    no-silent-gap rule applies to ``log``/``chat`` and to their containers.
    """
    data = _gomoku().to_dict()
    seats = data["seats"]
    assert len(seats) == 2

    bad_rows = [
        {**data, "seats": []},
        {**data, "seats": {"oops": 1}},
        {**data, "seats": [seats[0], "not-a-seat", seats[1]]},
        {**data, "seats": [seats[0], None]},
        {**data, "seats": [seats[0], ["x"]]},
        # Seat count is a module contract: truncated/padded rows are rejected.
        {**data, "seats": seats[:1]},
        {**data, "seats": [*seats, *seats]},
        {**data, "state": "not-an-object"},
        {**data, "log": [{"seat": 0, "move": "H8", "t": 1}, "junk"]},
        {**data, "chat": ["junk"]},
        {**data, "log": {"0": {"move": "x"}}},
        {**data, "chat": 42},
    ]
    for row in bad_rows:
        with pytest.raises(ValueError):
            Match.from_dict(row)


@pytest.mark.parametrize(
    ("kind", "mutate"),
    [
        ("gomoku", lambda s: {**s, "cells": []}),
        ("gomoku", lambda s: {**s, "cells": [0] * 224}),
        ("gomoku", lambda s: {**s, "toPlay": 3}),
        ("reversi", lambda s: {**s, "cells": [0] * 63}),
        ("doudizhu", lambda s: {k: v for k, v in s.items() if k != "cards"}),
        ("doudizhu", lambda s: {**s, "cards": [[], []]}),
    ],
)
def test_from_dict_rejects_bad_state_shape(kind, mutate) -> None:
    """The module contract probe rejects a state the game could not replay."""
    if kind == "doudizhu":
        match = _doudizhu()
    else:
        match = create_match(
            NewMatchOptions(
                id=f"m-{kind}",
                kind=kind,
                seats=[NewSeat("human", "t0"), NewSeat("ai", "")],
                seed=3,
                now=1_000,
            )
        )
    data = match.to_dict()
    data["state"] = mutate(match.state)
    with pytest.raises(ValueError):
        Match.from_dict(data)


def test_clean_and_unknown_kind_snapshots_round_trip_unchanged() -> None:
    match = _gomoku()
    restored = Match.from_dict(match.to_dict())
    assert [(s.kind, s.token, s.name) for s in restored.seats] == [
        (s.kind, s.token, s.name) for s in match.seats
    ]
    assert restored.to_dict() == match.to_dict()
    assert status_of(restored).waiting_on == status_of(match).waiting_on
    assert view_match(restored, 0)["view"] == view_match(match, 0)["view"]

    # Unknown kinds stay loadable (module contract unknown) but keep the shape.
    data = match.to_dict()
    data["kind"] = "future-game"
    assert Match.from_dict(data).kind == "future-game"


def test_from_dict_normalizes_garbage_resigned_to_terminal_marker(caplog) -> None:
    """Garbage ``resigned`` becomes a canonical terminal marker, never ``None``.

    ``99`` is deliberately out of range: it survives the DB integer column, so
    a reloaded row keeps deriving a terminal status instead of turning active.
    """
    data = _gomoku().to_dict()
    data["resigned"] = 99
    with caplog.at_level(logging.WARNING, logger="engine.match"):
        restored = Match.from_dict(data)
    assert restored.resigned == RESIGNED_INVALID  # not silently dropped to None
    assert restored.resigned < 0  # out of range for every seat test
    assert status_of(restored).outcome is not None
    assert any("resigned" in r.getMessage() for r in caplog.records)

    # Stays terminal across another serialization round trip.
    again = Match.from_dict(restored.to_dict())
    assert again.resigned == RESIGNED_INVALID
    assert status_of(again).outcome is not None


# ----------------------------------------------------------------------
#  D-25: bump() keeps every field via dataclasses.replace
# ----------------------------------------------------------------------


def test_bump_preserves_runtime_side_fields() -> None:
    """``plan`` is a runtime-only side field; a hand-written field list would
    drop it silently. ``replace`` keeps it (and future fields) automatically."""
    match = replace(_gomoku(), plan={"text": "先稳后攻"})
    said = apply_match_action(match, 0, {"type": "say", "text": "你好"}, 2_000)
    assert said.ok
    assert said.match.plan == {"text": "先稳后攻"}
    assert said.match.version == match.version + 1
    assert said.match.created_at == match.created_at
