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
from types import SimpleNamespace

import pytest

from engine import (
    NewMatchOptions,
    NewSeat,
    Outcome,
    apply_match_action,
    create_match,
    games,
    status_of,
    view_match,
)
from engine.match import RESIGNED_INVALID, LogEntry, Match
from engine.types import GameModule


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


@pytest.mark.parametrize(
    "tail",
    [
        # A legacy/hand-repaired row with entries *after* the resignation. The
        # backward scan must still find it instead of reading the match active.
        [LogEntry(seat=0, move="H8", t=2_100)],
        [LogEntry(seat=0, move="H8", t=2_100), LogEntry(seat=1, move="J9", t=2_200)],
        # A newer resignation wins over an older one (the newest ends the match).
        [LogEntry(seat=1, move="resign", t=2_100), LogEntry(seat=0, move="H8", t=2_200)],
    ],
)
def test_legacy_resignation_is_found_by_scanning_backwards(tail) -> None:
    """Only the tail entry is checked on the baseline; a later entry hid it."""
    resigned = apply_match_action(_gomoku(), 1, {"type": "resign"}, 2_000)
    assert resigned.ok
    legacy = replace(resigned.match, resigned=None, log=[*resigned.match.log, *tail])
    status = status_of(legacy)
    assert status.outcome is not None  # terminal, never active again
    assert status.waiting_on == []
    assert status.result_text == "P1 认输"
    assert view_match(legacy, None)["resigned"] == 1


@pytest.mark.parametrize("bad_seat", [99, -1, True, "x", None])
def test_malformed_resign_log_entry_fails_closed(bad_seat) -> None:
    """A resign entry with a garbage seat stays unresolvable (terminal).

    The entry must not be skipped as if nothing was logged — that would read
    the match as active — and it must not index the seat list.
    """
    match = replace(
        _gomoku(match_id=f"m-bad-resign-{bad_seat}"),
        log=[LogEntry(seat=bad_seat, move="resign", t=2_000)],
    )
    _assert_terminal(match)
    assert status_of(match).outcome.winners == []
    assert status_of(match).result_text == "对局已认输（座位数据异常）"
    assert view_match(match, None)["resigned"] is None


@pytest.mark.parametrize("bad_seat", [99, -1, True, "x", None])
def test_latest_malformed_resign_fails_closed_even_with_older_valid_one(bad_seat) -> None:
    """Newest resign wins; a malformed newest entry must not fall back.

    The backward scan stops at the first ``resign`` entry it finds. Skipping a
    corrupt newest entry to an older valid one would report the *wrong*
    resigner (and, for team games, the wrong camp), so the match stays
    unresolvable/terminal instead. This pins the fail-closed choice the review
    asked about, together with the older valid entry that must be ignored.
    """
    match = replace(
        _gomoku(match_id=f"m-latest-bad-resign-{bad_seat}"),
        log=[
            LogEntry(seat=1, move="resign", t=2_000),
            LogEntry(seat=bad_seat, move="resign", t=2_100),
        ],
    )
    status = status_of(match)
    _assert_terminal(match)
    assert status.outcome.winners == []
    assert status.result_text == "对局已认输（座位数据异常）"
    assert view_match(match, None)["resigned"] is None  # never seat 1


def test_newest_valid_resign_wins_over_older_one() -> None:
    """Contrast case: two *valid* resign entries resolve to the newest seat."""
    match = replace(
        _gomoku(match_id="m-two-valid-resigns"),
        log=[
            LogEntry(seat=1, move="resign", t=2_000),
            LogEntry(seat=0, move="resign", t=2_100),
        ],
    )
    assert status_of(match).result_text == "P0 认输"


@pytest.mark.parametrize(
    "log",
    [
        # A single out-of-range resign entry, as it arrives from the DB.
        [{"seat": 99, "move": "resign", "t": 2_000}],
        # Newest malformed + older valid: the older seat must not be reported.
        [
            {"seat": 1, "move": "resign", "t": 2_000},
            {"seat": 99, "move": "resign", "t": 2_100},
        ],
    ],
    ids=["single-bad", "newest-bad-older-valid"],
)
def test_persisted_out_of_range_resign_stays_terminal(log) -> None:
    """The fail-closed precedence survives a persistence reload."""
    data = _gomoku(match_id=f"m-persist-bad-resign-{len(log)}-{log[-1]['t']}").to_dict()
    data["resigned"] = None
    data["log"] = log
    reloaded = Match.from_dict(data)
    _assert_terminal(reloaded)
    assert status_of(reloaded).result_text == "对局已认输（座位数据异常）"
    assert view_match(reloaded, None)["resigned"] is None


def test_log_without_resign_entry_keeps_active_match_active() -> None:
    """The backward scan must not invent a resignation from ordinary moves."""
    moved = apply_match_action(_gomoku(), 0, {"type": "move", "move": "H8"}, 2_000)
    assert moved.ok
    status = status_of(replace(moved.match, resigned=None))
    assert status.outcome is None
    assert status.waiting_on == [1]


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


def _three_seat_resignation():
    """Resign seat 1 of a three-seat match (two partners around the resigner)."""
    resigned = apply_match_action(_doudizhu(), 1, {"type": "resign"}, 3_000)
    assert resigned.ok
    return resigned.match


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param(lambda state, seat: None, id="returns-none"),
        pytest.param(
            lambda state, seat: (_ for _ in ()).throw(RuntimeError("bad hook")),
            id="raises",
        ),
        pytest.param(lambda state, seat: "not-an-outcome", id="wrong-type"),
    ],
)
def test_failed_override_hook_is_terminal_without_winners(monkeypatch, hook) -> None:
    """A broken camp hook must never award the resigner's teammate a win.

    The generic "everyone but the resigner" fallback would list seat 2 here,
    which is precisely the P1-4 bug for a multi-faction game.
    """
    import engine.match as match_module

    match = _three_seat_resignation()
    fake = SimpleNamespace(outcome_for_resign=hook)
    monkeypatch.setattr(match_module.games, "get", lambda kind: fake)
    status = status_of(match)
    assert status.outcome is not None  # still terminal
    assert status.waiting_on == []
    assert status.outcome.winners == []  # fail closed, never [0, 2]
    assert status.result_text == "A 认输"


def test_override_hook_outcome_is_used_as_is(monkeypatch) -> None:
    import engine.match as match_module

    match = _three_seat_resignation()
    fake = SimpleNamespace(
        outcome_for_resign=lambda state, seat: Outcome(winners=[0], text="自定义裁决")
    )
    monkeypatch.setattr(match_module.games, "get", lambda kind: fake)
    status = status_of(match)
    assert status.outcome.winners == [0]
    assert status.outcome.text == "自定义裁决"


def test_inherited_default_hook_still_uses_the_generic_rule() -> None:
    """The base ``GameModule`` default is not a failure: two-seat semantics hold.

    ``GameModule.outcome_for_resign`` returns ``None``; only an *override* that
    returns ``None`` is treated as an undecided verdict.
    """
    assert GameModule.outcome_for_resign(object(), {}, 0) is None
    assert type(games.get("gomoku")).outcome_for_resign is GameModule.outcome_for_resign
    resigned = apply_match_action(_gomoku(), 1, {"type": "resign"}, 2_000)
    assert resigned.ok
    assert status_of(resigned.match).outcome.winners == [0]


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


# Every numeric column read by ``from_dict`` must reject garbage with
# ``ValueError`` (the store/room contract), never leak ``TypeError`` from a bare
# ``int(None)``/``int(dict)``. Values are appended to an otherwise valid row so
# the numeric path is what rejects it, not an earlier shape check.
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["log"].append({"seat": None, "move": "H8", "t": 1}),
        lambda d: d["log"].append({"seat": 0, "move": "H8", "t": "later"}),
        lambda d: d["log"].append({"seat": {}, "move": "H8", "t": 1}),
        lambda d: d["chat"].append({"seat": None, "text": "hi", "t": 1, "at": 1}),
        lambda d: d["chat"].append({"seat": 0, "text": "hi", "t": [], "at": 1}),
        lambda d: d["chat"].append({"seat": 0, "text": "hi", "t": 1, "at": "now"}),
        lambda d: d.__setitem__("created_at", {"bad": 1}),
        lambda d: d.__setitem__("updated_at", "yesterday"),
        lambda d: d.__setitem__("version", ["v1"]),
    ],
    ids=[
        "log-seat-null",
        "log-t-text",
        "log-seat-object",
        "chat-seat-null",
        "chat-t-list",
        "chat-at-text",
        "created-at-object",
        "updated-at-text",
        "version-list",
    ],
)
def test_from_dict_rejects_malformed_numbers_with_value_error(mutate) -> None:
    data = _gomoku().to_dict()
    mutate(data)
    with pytest.raises(ValueError):
        Match.from_dict(data)


def test_from_dict_still_accepts_legacy_integer_strings_and_defaults() -> None:
    """Normalising the exception type must not reject legacy persisted rows."""
    data = _gomoku().to_dict()
    data["created_at"] = "1000"
    data["version"] = "2"
    data["log"] = [{"seat": "1", "move": "H8", "t": "5"}]
    data["chat"] = [{"seat": "0", "text": "hi", "t": "6", "at": "7"}]
    restored = Match.from_dict(data)
    assert (restored.created_at, restored.version) == (1_000, 2)
    assert (restored.log[0].seat, restored.log[0].t) == (1, 5)
    assert (restored.chat[0].seat, restored.chat[0].t, restored.chat[0].at) == (0, 6, 7)

    # Missing keys keep their historical defaults.
    for key in ("created_at", "updated_at", "version"):
        data.pop(key)
    data["log"] = [{"move": "H8"}]
    data["chat"] = [{"text": "hi"}]
    restored = Match.from_dict(data)
    assert (restored.created_at, restored.updated_at, restored.version) == (0, 0, 1)
    assert (restored.log[0].seat, restored.log[0].t) == (0, 0)
    assert (restored.chat[0].seat, restored.chat[0].t, restored.chat[0].at) == (0, 0, 0)


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
