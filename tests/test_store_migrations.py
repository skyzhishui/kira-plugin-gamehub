"""Old-schema SQLite migration, end to end.

A real SQLite file is created with the *old* column set (``sqlite3``), then
opened through ``MatchStore`` (``aiosqlite`` + SQLAlchemy) to verify the upgrade
path users actually hit after a plugin update. One test walks the whole
lifecycle; the rest pin the boundaries that must stay independent:

- ``init()`` ALTERs ``gamehub_matches`` (adds ``mode_options_json`` /
  ``resigned_seat``) and ``gamehub_chat_lines`` (adds ``match_id`` / ``uid``
  back-filled with empty strings) and is idempotent across reopen;
- a legacy row still loads, its resignation is preserved from the log, and a
  phantom ``status=active`` row is not resurrected by ``load_active``;
- the chat window reads legacy lines and round-trips the new scoped columns;
- the migrated table accepts the new write path unchanged;
- ``row_to_match`` is pure and separately pinned for the explicit
  ``resigned_seat`` precedence over the log-derived value.
"""

from __future__ import annotations

import json
import sqlite3
import time

from sqlalchemy import inspect as sa_inspect

from engine import (
    Match,
    NewMatchOptions,
    NewSeat,
    apply_match_action,
    create_match,
    status_of,
)
from host.store import (
    GamehubChatLine,
    GamehubMatch,
    MatchStore,
    match_to_row,
    row_to_match,
)

# Columns exactly as an older plugin version created them: no mode options and
# no explicit resigned seat on matches; no match/uid scope on chat lines.
_LEGACY_MATCH_DDL = """
CREATE TABLE gamehub_matches (
    match_id VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    seats_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    log_json TEXT NOT NULL,
    chat_json TEXT NOT NULL,
    winner_seats TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (match_id)
)
"""

_LEGACY_CHAT_DDL = """
CREATE TABLE gamehub_chat_lines (
    id INTEGER NOT NULL,
    role VARCHAR(16) NOT NULL,
    sender VARCHAR(64) NOT NULL,
    text TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
)
"""

_LEGACY_MATCH_COLUMNS = {
    "match_id", "kind", "version", "status", "seats_json", "state_json",
    "log_json", "chat_json", "winner_seats", "created_at", "updated_at",
}
_LEGACY_CHAT_COLUMNS = {"id", "role", "sender", "text", "created_at"}


def _new_match(match_id: str = "legacy-1", now: int | None = None) -> Match:
    now = int(time.time() * 1000) if now is None else now
    seats = [
        NewSeat(
            kind="human",
            token="h-token",
            name="小明",
            actor="qq:10001",
            session_id="qq-10001-私聊",
        ),
        NewSeat(kind="bot", token="", name="陪练"),
    ]
    return create_match(
        NewMatchOptions(
            id=match_id,
            kind="gomoku",
            seats=seats,
            seed=7,
            now=now,
            bot_name="诺里",
        )
    )


def _legacy_row() -> dict:
    """A finished-by-resignation snapshot written by the old schema.

    The row is deliberately marked ``status=active`` and has no
    ``resigned_seat`` column: this is the phantom-active shape that used to
    come back to life after a restart.
    """
    match = _new_match(now=1_700_000_000_000)
    resigned = apply_match_action(match, 1, {"type": "resign"}, 1_700_000_100_000)
    assert resigned.ok
    row = match_to_row(resigned.match, "active")
    # Simulate the old writer: no mode options / resigned columns in the table.
    row.pop("mode_options_json", None)
    row.pop("resigned_seat", None)
    row["created_at"] = "2023-11-14 22:13:20.000000"
    row["updated_at"] = "2023-11-14 22:15:00.000000"
    return row


def _create_legacy_db(path, *, rows=(), chat_lines=()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_LEGACY_MATCH_DDL)
        conn.execute(_LEGACY_CHAT_DDL)
        if rows:
            columns = list(rows[0])
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f"INSERT INTO gamehub_matches ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                [[row[column] for column in columns] for row in rows],
            )
        for line in chat_lines:
            conn.execute(
                "INSERT INTO gamehub_chat_lines (role, sender, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                line,
            )
        conn.commit()
    finally:
        conn.close()


def _legacy_columns(path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


async def _open_columns(store: MatchStore, table: str) -> set[str]:
    async with store.engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in sa_inspect(sync_conn).get_columns(table)
            }
        )


async def test_legacy_file_upgrades_and_serves_the_full_lifecycle(tmp_path):
    """One real old file through every stage: schema, load, write, chat."""
    db_path = tmp_path / "gamehub.sqlite3"
    _create_legacy_db(
        db_path,
        rows=[_legacy_row()],
        chat_lines=[("player", "小白", "旧版留言", "2023-11-14 22:00:00.000000")],
    )
    # Precondition: the legacy file really lacks the new columns.
    assert _legacy_columns(db_path, "gamehub_matches") == _LEGACY_MATCH_COLUMNS
    assert _legacy_columns(db_path, "gamehub_chat_lines") == _LEGACY_CHAT_COLUMNS

    store = MatchStore(tmp_path)
    await store.init()
    try:
        # 1. Upgrade: the new columns exist with the fresh-schema defaults.
        assert {"mode_options_json", "resigned_seat"} <= await _open_columns(
            store, GamehubMatch.__tablename__
        )
        assert {"match_id", "uid"} <= await _open_columns(
            store, GamehubChatLine.__tablename__
        )
        assert _legacy_columns(db_path, "gamehub_matches") >= {
            "mode_options_json", "resigned_seat"
        }
        upgraded_columns = await _open_columns(store, GamehubMatch.__tablename__)

        # 2. The legacy row loads; the log supplies the resignation because
        #    the newly added column defaults to NULL. Legacy options are empty.
        loaded = await store.get("legacy-1")
        assert loaded is not None
        assert loaded.resigned == 1
        assert status_of(loaded).outcome is not None
        assert loaded.mode_options == {}

        # 3. Phantom active: ``status`` says active but the snapshot is
        #    terminal, so a restart must not revive it -- yet history keeps
        #    working by id.
        assert await store.load_active() == []
        assert await store.get("legacy-1") is not None

        # 4. The migrated table accepts the new write path unchanged.
        match = _new_match("post-migration", now=int(time.time() * 1000))
        await store.upsert(match)
        reloaded = await store.get("post-migration")
        assert reloaded is not None and reloaded.to_dict() == match.to_dict()
        assert [m.id for m in await store.load_active()] == ["post-migration"]

        # 5. Chat window: legacy lines are readable and back-filled columns
        #    default to empty strings, not NULL.
        legacy = await store.chat_recent(10)
        assert [line["text"] for line in legacy] == ["旧版留言"]
        assert legacy[0]["match_id"] == "" and legacy[0]["uid"] == ""

        written = await store.chat_append(
            "agent", "诺里", "新版回复", match_id="legacy-1", uid="qq:10001"
        )
        assert written["match_id"] == "legacy-1" and written["uid"] == "qq:10001"

        all_lines = await store.chat_recent(10)
        assert [line["text"] for line in all_lines] == ["旧版留言", "新版回复"]
        # Match scoping excludes the legacy unscoped line.
        scoped = await store.chat_recent(10, match_id="legacy-1")
        assert [(line["text"], line["uid"]) for line in scoped] == [
            ("新版回复", "qq:10001")
        ]
    finally:
        await store.close()

    # 6. Reopen: init() is a no-op (no duplicated/rebuild columns) and the
    #    upgraded data is still there.
    reopened = MatchStore(tmp_path)
    await reopened.init()
    await reopened.init()  # same instance: no-op
    try:
        assert await _open_columns(reopened, GamehubMatch.__tablename__) == (
            upgraded_columns
        )
        assert (await reopened.get("legacy-1")).resigned == 1
        assert (await reopened.get("post-migration")).version == match.version
        assert [line["text"] for line in await reopened.chat_recent(10)] == [
            "旧版留言", "新版回复"
        ]
    finally:
        await reopened.close()


def _row(match: Match, *, resigned_seat, status: str = "over") -> dict:
    row = match_to_row(match, status)
    row["resigned_seat"] = resigned_seat
    return row


def test_explicit_resigned_column_wins_over_the_log():
    """The column is DB truth: it wins even when it disagrees with the log."""
    match = _new_match(now=1_700_000_000_000)
    resigned = apply_match_action(match, 1, {"type": "resign"}, 1_700_000_100_000)
    assert resigned.ok
    snapshot = resigned.match  # log says seat 1 resigned

    assert row_to_match(_row(snapshot, resigned_seat=0)).resigned == 0
    assert row_to_match(_row(snapshot, resigned_seat=1)).resigned == 1
    # NULL column falls back to the log-derived seat.
    assert row_to_match(_row(snapshot, resigned_seat=None)).resigned == 1

    # No column and no log entry: a plain active snapshot stays active.
    active = _row(_new_match(now=1_700_000_000_000), resigned_seat=None, status="active")
    restored = row_to_match(active)
    assert restored is not None
    assert restored.resigned is None
    assert json.loads(active["winner_seats"] or "null") is None
