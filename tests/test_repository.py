"""Persistence integration tests (``host.store``).

Real SQLite (``aiosqlite``) + SQLAlchemy async sessions verify:

- round-trip (Chinese text, log/chat, seat actor/token/plan);
- idempotent upsert (two saves, one row);
- ``load_active`` returns only derived-active matches;
- ``mark_status`` / ``archive_stale`` state transitions;
- corrupt JSON tolerance (warn, skip the row, never fail a batch read);
- ``init`` creates the table idempotently in the plugin data dir.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from engine import (
    Match,
    MatchChat,
    NewMatchOptions,
    NewSeat,
    apply_match_action,
    create_match,
    status_of,
)
from host.store import (
    CHAT_KEEP_LINES,
    GamehubMatch,
    MatchStore,
    match_to_row,
    row_to_match,
)


@pytest.fixture()
async def store(tmp_path):
    store = MatchStore(tmp_path)
    await store.init()
    try:
        yield store
    finally:
        await store.close()


def _new_match(match_id: str = "m1", now: int | None = None) -> Match:
    """A human vs AI gomoku match (Chinese names + human token + chat actor)."""
    now = int(time.time() * 1000) if now is None else now
    seats = [
        NewSeat(
            kind="human",
            token="m1-abc123",
            name="小明",
            actor="qq:10001",
            session_id="qq-10001-私聊",
        ),
        NewSeat(
            kind="ai",
            token="",
            name="诺里",
            actor="nori_notice",
            session_id="web-admin-bot-token",
        ),
    ]
    return create_match(
        NewMatchOptions(id=match_id, kind="gomoku", seats=seats, seed=7, now=now)
    )


# ----------------------------------------------------------------------
#  round-trip
# ----------------------------------------------------------------------


async def test_round_trip_preserves_chinese_log_chat_and_plan(store):
    match = _new_match("m-cn", now=1_700_000_000_000)

    said = apply_match_action(match, 0, {"type": "say", "text": "你好呀，诺里！"}, 1_700_000_010_000)
    assert said.ok
    match = said.match

    moved = apply_match_action(match, 0, {"type": "move", "move": "H8"}, 1_700_000_020_000)
    assert moved.ok
    match = moved.match

    # Delegated play: the say is attributed to the host agent (by="host").
    # Built via replace() — the apply path itself is covered by the room tests;
    # what matters here is that the marker survives the store round trip.
    match = replace(
        match,
        chat=[
            *match.chat,
            MatchChat(seat=0, text="我替你守这里", t=1_700_000_025_000, at=len(match.log), by="host"),
        ],
    )

    plan = {"direction": "先稳后攻，别急着连四", "aggression": "steady", "ttl_moves": 4}
    match = replace(
        match,
        seats=[replace(match.seats[0], plan=plan), *match.seats[1:]],
        # Top-level plan is a runtime side field; it is not persisted.
        plan={"direction": "不该落库"},
    )

    status = status_of(match)
    assert status.outcome is None
    await store.upsert(match)

    loaded = await store.get(match.id)
    assert loaded is not None
    assert loaded.to_dict() == match.to_dict()
    assert loaded.seats[0].plan == plan
    assert loaded.seats[1].plan is None
    assert loaded.seats[0].actor == "qq:10001"
    assert loaded.seats[0].session_id == "qq-10001-私聊"
    assert loaded.seats[0].token == "m1-abc123"
    assert loaded.seats[1].token == ""  # AI seats carry no token
    assert loaded.chat[0].text == "你好呀，诺里！"
    assert loaded.chat[0].by == ""  # seat-own speech keeps the empty marker
    assert loaded.chat[1].text == "我替你守这里"
    assert loaded.chat[1].by == "host"  # delegated speaker survives the round trip
    assert loaded.log[0].move == "H8"

    # Raw columns keep Chinese unescaped (ensure_ascii=False).
    async with store.session() as session:
        row = await session.get(GamehubMatch, match.id)
        assert row is not None
        assert "诺里" in row.seats_json and "\\u" not in row.seats_json
        assert "你好呀" in row.chat_json


async def test_round_trip_preserves_match_source(store):
    """Match source is persisted explicitly so a chat match is not mistaken for a page one."""
    match = _new_match("m-src", now=1_700_000_000_000)
    match = replace(
        match,
        seats=[replace(match.seats[0], source="chat"), *match.seats[1:]],
    )
    await store.upsert(match, "active")
    loaded = await store.get("m-src")
    assert loaded is not None
    assert loaded.seats[0].source == "chat"
    active = await store.load_active()
    assert active and active[0].seats[0].source == "chat"

    # Legacy data has no source key: reads back empty, routing infers it.
    row = match_to_row(match, "active")
    seats = json.loads(row["seats_json"])
    seats[0].pop("source")
    row["seats_json"] = json.dumps(seats, ensure_ascii=False)
    legacy = row_to_match(row)
    assert legacy is not None and legacy.seats[0].source == ""


async def test_row_to_match_accepts_plain_dict_and_winner_seats(store):
    match = _new_match("m-dict", now=1_700_000_000_000)
    row = match_to_row(match, status_of(match))

    assert row["status"] == "active"
    assert row["winner_seats"] is None
    restored = row_to_match(row)
    assert restored is not None
    assert restored.to_dict() == match.to_dict()

    resigned = apply_match_action(match, 1, {"type": "resign"}, 1_700_000_100_000)
    assert resigned.ok
    over_row = match_to_row(resigned.match, status_of(resigned.match))
    assert over_row["status"] == "over"
    assert json.loads(over_row["winner_seats"]) == [0]


async def test_resigned_seat_round_trips_and_legacy_log_derives(store):
    """The resigned seat is a real column; legacy rows derive it from the log."""
    match = _new_match("m-resign", now=1_700_000_000_000)
    result = apply_match_action(match, 1, {"type": "resign"}, 1_700_000_100_000)
    assert result.ok
    resigned = result.match
    assert status_of(resigned).outcome is not None

    await store.upsert(resigned)
    loaded = await store.get("m-resign")
    assert loaded is not None
    assert loaded.resigned == 1
    assert status_of(loaded).outcome is not None

    # Simulate an old row: no resigned_seat column value, only the log entry.
    row = match_to_row(resigned, "active")
    assert row["status"] == "over"  # a finished match is never written as active
    row.pop("resigned_seat", None)
    legacy = row_to_match(row)
    assert legacy is not None
    assert legacy.resigned == 1
    assert status_of(legacy).outcome is not None


async def test_load_active_ignores_phantom_active_resigned_rows(store):
    """Legacy bad rows (status=active but resigned) must not revive on restart."""
    match = _new_match("m-phantom", now=int(time.time() * 1000))
    result = apply_match_action(match, 1, {"type": "resign"}, int(time.time() * 1000))
    assert result.ok
    await store.upsert(result.match)

    # Simulate an old version: written as active with no resigned_seat value.
    async with store.session() as session:
        await session.execute(
            update(GamehubMatch)
            .where(GamehubMatch.match_id == "m-phantom")
            .values(status="active", resigned_seat=None)
        )
        await session.commit()

    assert await store.load_active() == []
    assert await store.get("m-phantom") is not None  # still readable by id


# ----------------------------------------------------------------------
#  upsert
# ----------------------------------------------------------------------


async def test_save_twice_upserts_single_row(store):
    match = _new_match("m-upsert", now=1_700_000_000_000)
    await store.upsert(match)

    moved = apply_match_action(match, 0, {"type": "move", "move": "H8"}, 1_700_000_050_000)
    assert moved.ok
    await store.upsert(moved.match)

    async with store.session() as session:
        count = await session.scalar(select(func.count()).select_from(GamehubMatch))
        rows = (await session.execute(select(GamehubMatch))).scalars().all()
    assert count == 1
    assert len(rows) == 1
    assert rows[0].match_id == "m-upsert"
    assert rows[0].version == 2
    assert rows[0].log_json != "[]"

    loaded = await store.get("m-upsert")
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.log[0].move == "H8"


# ----------------------------------------------------------------------
#  load_active / status transitions
# ----------------------------------------------------------------------


async def test_load_active_filters_over_and_archived(store):
    active = _new_match("m-active", now=1_700_000_000_000)
    await store.upsert(active)

    over = _new_match("m-over", now=1_700_000_000_000)
    resigned = apply_match_action(over, 1, {"type": "resign"}, 1_700_000_100_000)
    assert resigned.ok
    over_status = status_of(resigned.match)
    assert over_status.outcome is not None
    await store.upsert(resigned.match, over_status)

    archived = _new_match("m-archived", now=1_700_000_000_000)
    await store.upsert(archived)
    assert await store.mark_status("m-archived", "archived") == 1

    loaded = await store.load_active()
    assert [m.id for m in loaded] == ["m-active"]
    assert loaded[0].to_dict() == active.to_dict()


async def test_mark_status_and_archive_stale(store):
    now = int(time.time())
    old = now - 40 * 86400
    recent = now - 3600

    stale = _new_match("m-stale", now=old)
    stale_resigned = apply_match_action(stale, 1, {"type": "resign"}, old + 30)
    assert stale_resigned.ok
    await store.upsert(stale_resigned.match)

    fresh = _new_match("m-fresh", now=recent)
    fresh_resigned = apply_match_action(fresh, 1, {"type": "resign"}, recent)
    assert fresh_resigned.ok
    await store.upsert(fresh_resigned.match)

    # Only the match finished 40 days ago is archived.
    assert await store.archive_stale(30) == 1
    assert await store.archive_stale(30) == 0
    async with store.session() as session:
        statuses = {
            row.match_id: row.status
            for row in (await session.execute(select(GamehubMatch))).scalars()
        }
    assert statuses == {"m-stale": "archived", "m-fresh": "over"}

    # mark_status on a missing row is a no-op returning 0.
    assert await store.mark_status("missing", "archived") == 0

    # active -> over through mark_status
    active = _new_match("m-direct", now=now)
    await store.upsert(active)
    assert await store.mark_status("m-direct", "over") == 1
    assert await store.load_active() == []


async def test_delete_is_idempotent(store):
    match = _new_match("m-del")
    await store.upsert(match)
    assert await store.delete("m-del") == 1
    assert await store.delete("m-del") == 0
    assert await store.get("m-del") is None


# ----------------------------------------------------------------------
#  Corrupt JSON tolerance
# ----------------------------------------------------------------------


@pytest.mark.parametrize("column", ["seats_json", "state_json", "log_json", "chat_json"])
async def test_corrupt_json_is_skipped_with_warning(store, caplog, column):
    match = _new_match(f"m-broken-{column}")
    await store.upsert(match)

    async with store.session() as session:
        await session.execute(
            update(GamehubMatch)
            .where(GamehubMatch.match_id == match.id)
            .values(**{column: "{not json"})
        )
        await session.commit()

    with caplog.at_level(logging.WARNING, logger="host.store"):
        assert await store.get(match.id) is None
        assert any("JSON 损坏" in r.message for r in caplog.records)
        caplog.clear()
        # A batch read must not fail because one snapshot is corrupt.
        assert await store.load_active() == []
        assert any("JSON 损坏" in r.message for r in caplog.records)


def test_row_to_match_detects_corrupt_dict():
    match = _new_match("m-dict-broken")
    row = match_to_row(match, status_of(match))
    assert row_to_match({**row, "seats_json": "{oops"}) is None
    assert row_to_match({**row, "state_json": "42-ish"}) is None


# ----------------------------------------------------------------------
#  init
# ----------------------------------------------------------------------


async def test_init_creates_table_idempotently(tmp_path):
    from sqlalchemy import inspect as sa_inspect

    store = MatchStore(tmp_path)
    await store.init()
    try:
        assert store.path == tmp_path / "gamehub.sqlite3"
        await store.init()  # idempotent
        async with store.engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: sa_inspect(sync_conn).get_table_names()
            )
        assert "gamehub_matches" in tables
    finally:
        await store.close()
    # Closing is idempotent as well.
    await store.close()


def test_match_to_row_accepts_room_status_string():
    """room may pass a plain status string; it must still persist correctly."""
    match = _new_match("m-str", now=int(time.time() * 1000))
    row = match_to_row(match, "active")
    assert row["status"] == "active"
    assert row["winner_seats"] is None
    over = apply_match_action(match, 1, {"type": "resign"}, int(time.time() * 1000))
    assert over.ok
    row = match_to_row(over.match, "over")
    assert row["status"] == "over"
    assert json.loads(row["winner_seats"]) == [0]

async def test_chat_lines_append_recent_and_prune(tmp_path):
    """Chat-window read model: ascending order, bounded retention."""
    store = MatchStore(tmp_path)
    await store.init()
    try:
        first = await store.chat_append("player", "小白", "你好")
        second = await store.chat_append("agent", "汐祈（Seki）", "要来一局吗？")
        assert first["role"] == "player" and second["role"] == "agent"
        assert int(second["ts"]) >= int(first["ts"]) > 0

        lines = await store.chat_recent(10)
        assert [line["text"] for line in lines] == ["你好", "要来一局吗？"]

        limited = await store.chat_recent(1)
        assert [line["text"] for line in limited] == ["要来一局吗？"]

        # Retention: writing past the cap prunes the oldest lines.
        for i in range(CHAT_KEEP_LINES + 5):
            await store.chat_append("player", "小白", f"m{i}")
        kept = await store.chat_recent(200)
        assert len(kept) <= CHAT_KEEP_LINES
        assert kept[-1]["text"] == f"m{CHAT_KEEP_LINES + 4}"
        assert kept[0]["text"] != "你好"
    finally:
        await store.close()
