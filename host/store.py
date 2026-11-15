"""Private SQLite persistence for gamehub matches (nori models + repository).

Snapshot persistence (no event sourcing): one row per match;
``state_json`` / ``log_json`` / ``chat_json`` / ``seats_json`` hold the whole
snapshot. Every write replaces the full row inside the caller's per-match
lock (last-write-wins, single process / single writer).

Table discipline: a plugin-private ``DeclarativeBase`` (own ``MetaData``) keeps
``gamehub_matches`` out of the host's shared registry / alembic metadata.
``init()`` is idempotent and additionally back-fills columns that
``create_all`` cannot add to an existing table.

The store owns its own engine: ``sqlite+aiosqlite:///<plugin_data>/gamehub.sqlite3``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

try:  # production: plugin package (``plugins.<plugin>.host.store``)
    from ..engine.match import Match, MatchStatus, status_of
except ImportError:  # standalone tests import ``host.store`` as top-level
    from engine.match import Match, MatchStatus, status_of

DB_FILENAME = "gamehub.sqlite3"

# The chat window keeps a bounded read model; older lines are pruned on write.
CHAT_KEEP_LINES = 500

# Match states: running / finished / archived (archived never loads as active)
STATUS_ACTIVE = "active"
STATUS_OVER = "over"
STATUS_ARCHIVED = "archived"

ALL_STATUSES = (STATUS_ACTIVE, STATUS_OVER, STATUS_ARCHIVED)

# Marker for a broken JSON column (distinct from a legitimate JSON null).
_CORRUPT = object()


# ----------------------------------------------------------------------
#  Models
# ----------------------------------------------------------------------


class Base(DeclarativeBase):
    """Plugin-private declarative base (own MetaData, reload safe)."""


class GamehubMatch(Base):
    """Whole snapshot of one match.

    - ``version`` is client anti-rollback only (no conditional UPDATE);
    - ``seats_json`` carries seat token / kind / name / joined / actor /
      session_id / plan (plan lives on the seat, not in its own column);
    - ``winner_seats`` is the JSON list of winning seats; ``None`` = not
      finished, ``"[]"`` = draw;
    - every JSON column uses ``json.dumps(..., ensure_ascii=False)``.
    """

    __tablename__ = "gamehub_matches"

    match_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="Match id"
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Game kind (gomoku/reversi/...)"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="Version (client anti-rollback)"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STATUS_ACTIVE,
        comment="Status: active/over/archived",
    )
    seats_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
        comment="Seats JSON (token/kind/name/joined/actor/session_id/plan)",
    )
    state_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", comment="Engine state JSON (incl. RNG)"
    )
    log_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", comment="Move log JSON (<=500)"
    )
    chat_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
        comment="In-match chat JSON (<=100)",
    )
    mode_options_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        comment="Mode options JSON (mode/role/driver/difficulty/delegation)",
    )
    winner_seats: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Winner seats JSON; None=unfinished, []=draw"
    )
    resigned_seat: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Resigned seat; None=no resignation"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Created (UTC)"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Updated (UTC)"
    )

    __table_args__ = (
        # Main filter/sort path for load_active / archive_stale / cleanup
        Index("idx_gamehub_status_updated", "status", "updated_at"),
    )


class GamehubChatLine(Base):
    """One line of the match-page chat window (player <-> host agent).

    The window is session-level: lines from every match of the plugin-owned
    ``gamehub`` session share this table, newest last. Kept plugin-private on
    purpose — the core's ``messages`` archive belongs to the host, and the
    page needs a cheap, trimmed, read-model it can backfill from.
    """

    __tablename__ = "gamehub_chat_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    uid: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="player | agent"
    )
    sender: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="Display name"
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="Message text")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Created (UTC)"
    )

    __table_args__ = (
        Index("idx_gamehub_chat_created", "created_at"),
    )


def _ensure_columns(sync_conn: Any) -> None:
    """Add columns missing from an old table (idempotent)."""
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if not inspector.has_table(GamehubMatch.__tablename__):
        return
    existing = {c["name"] for c in inspector.get_columns(GamehubMatch.__tablename__)}
    for column in _MIGRATION_COLUMNS:
        if column.name not in existing:
            sync_conn.execute(text(column.ddl))
    chat_columns = {c["name"] for c in inspector.get_columns(GamehubChatLine.__tablename__)}
    for name in ("match_id", "uid"):
        if name not in chat_columns:
            sync_conn.execute(text(f"ALTER TABLE gamehub_chat_lines ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"))


class _ColumnMigration:
    """Old-table column back-fill: column name + idempotent ALTER DDL."""

    __slots__ = ("name", "ddl")

    def __init__(self, name: str, ddl: str) -> None:
        self.name = name
        self.ddl = ddl


_MIGRATION_COLUMNS = (
    _ColumnMigration(
        name="mode_options_json",
        ddl=(
            "ALTER TABLE gamehub_matches ADD COLUMN mode_options_json "
            "TEXT NOT NULL DEFAULT '{}'"
        ),
    ),
    _ColumnMigration(
        name="resigned_seat",
        ddl="ALTER TABLE gamehub_matches ADD COLUMN resigned_seat INTEGER",
    ),
)


# ----------------------------------------------------------------------
#  Row <-> Match (pure helpers, unit-testable without a database)
# ----------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def epoch_to_datetime(value: Any) -> datetime:
    """Match-layer epoch millis -> UTC datetime (tolerates legacy seconds)."""
    raw = int(value or 0)
    if raw and raw < 100_000_000_000:  # < 1e11 counts as seconds (legacy)
        raw *= 1000
    return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)


def datetime_to_epoch(value: Any) -> int:
    """DB datetime -> epoch millis; SQLite reads back naive, treated as UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _get(row: Any, name: str) -> Any:
    """Uniform attribute access for ORM instances and dicts."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _loads(raw: Any, *, field: str, match_id: str) -> Any:
    """Parse a JSON column; on corruption log a warning and return ``_CORRUPT``.

    Non-strings are treated as already-parsed values (test stubs / future
    JSON column migration tolerance).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("对局 %s 的 %s 列 JSON 损坏，跳过该行", match_id or "?", field)
        return _CORRUPT


def match_to_row(match: Match, status: MatchStatus | str) -> dict[str, Any]:
    """Match + status -> row dict (column names, full-row overwrite).

    ``status`` accepts either a ``MatchStatus`` or a status string
    (``active`` / ``over``); winner / outcome always come from ``status_of`` so
    a string form can never lose the result.
    """
    data = match.to_dict()
    derived = status_of(match)
    outcome = derived.outcome
    if isinstance(status, str):
        column = status
    else:
        column = STATUS_OVER if outcome is not None else STATUS_ACTIVE
    # A resigned/finished snapshot must never land as active (otherwise it
    # would come back to life after a restart).
    if outcome is not None and column == STATUS_ACTIVE:
        column = STATUS_OVER
    resigned = match.resigned
    return {
        "match_id": match.id,
        "kind": match.kind,
        "version": int(match.version),
        "status": column,
        "seats_json": _dumps(data["seats"]),
        "state_json": _dumps(data["state"]),
        "log_json": _dumps(data["log"]),
        "chat_json": _dumps(data["chat"]),
        "mode_options_json": _dumps(data.get("mode_options") or {}),
        "winner_seats": None if outcome is None else _dumps(list(outcome.winners)),
        "resigned_seat": (
            int(resigned)
            if isinstance(resigned, int) and not isinstance(resigned, bool)
            else None
        ),
        "created_at": epoch_to_datetime(match.created_at),
        "updated_at": epoch_to_datetime(match.updated_at),
    }


def _resigned_from_log(log: list[Any]) -> int | None:
    """Back-fill the resigned seat from the move log (old rows lack the column)."""
    for entry in reversed(log):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("move") or "") != "resign":
            continue
        try:
            seat = int(entry.get("seat"))
        except (TypeError, ValueError):
            return None
        return seat
    return None


def _resigned_of(row: Any, log: list[Any]) -> int | None:
    """Resigned seat: explicit column first, legacy snapshots from the log."""
    value = _get(row, "resigned_seat")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return _resigned_from_log(log)


def row_to_match(row: Any) -> Match | None:
    """Row (ORM instance or dict) -> Match; bad JSON / deserialization -> None."""
    match_id = str(_get(row, "match_id") or "")
    seats = _loads(_get(row, "seats_json"), field="seats_json", match_id=match_id)
    state = _loads(_get(row, "state_json"), field="state_json", match_id=match_id)
    log = _loads(_get(row, "log_json"), field="log_json", match_id=match_id)
    chat = _loads(_get(row, "chat_json"), field="chat_json", match_id=match_id)
    mode_options = _loads(
        _get(row, "mode_options_json"), field="mode_options_json", match_id=match_id
    )
    if any(item is _CORRUPT for item in (seats, state, log, chat)):
        return None
    if not isinstance(seats, list) or not isinstance(log, list) or not isinstance(chat, list):
        logger.warning("对局 %s 的 JSON 列结构不符合预期，跳过该行", match_id or "?")
        return None
    data = {
        "id": match_id,
        "kind": _get(row, "kind"),
        "created_at": datetime_to_epoch(_get(row, "created_at")),
        "updated_at": datetime_to_epoch(_get(row, "updated_at")),
        "version": _get(row, "version"),
        "seats": seats,
        "state": state,
        # Explicit column first; old rows fall back to the log's resign entry so
        # a resigned match is not revived as active after a restart.
        "resigned": _resigned_of(row, log),
        "mode_options": mode_options if isinstance(mode_options, dict) else {},
        "log": log,
        "chat": chat,
    }
    try:
        return Match.from_dict(data)
    except (KeyError, TypeError, ValueError, AttributeError):
        logger.warning("对局 %s 反序列化失败，跳过该行", match_id or "?", exc_info=True)
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
#  Store
# ----------------------------------------------------------------------


class MatchStore:
    """``gamehub_matches`` reader/writer on a plugin-private SQLite file."""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._db_path = self._data_dir / DB_FILENAME
        self._engine: Any = None
        self._session_maker: Any = None

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def engine(self) -> Any:
        return self._engine

    def session(self):
        """Async session context manager (``async with store.session()``)."""
        if self._session_maker is None:
            raise RuntimeError("MatchStore.init() has not been called")
        return self._session_maker()

    async def init(self) -> None:
        """Create the engine, the table and any missing columns (idempotent)."""
        if self._engine is not None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._db_path}",
            future=True,
        )
        self._session_maker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
            await conn.run_sync(_ensure_columns)

    async def upsert(self, match: Match, status: MatchStatus | str | None = None) -> None:
        """Idempotent upsert of the whole snapshot (last-write-wins).

        Exceptions propagate: the caller (room) marks the match dirty and
        retries on the next change.
        """
        row = match_to_row(match, status_of(match) if status is None else status)
        async with self.session() as session:
            existing = await session.get(GamehubMatch, row["match_id"])
            if existing is None:
                session.add(GamehubMatch(**row))
            else:
                for key, value in row.items():
                    setattr(existing, key, value)
            await session.commit()

    async def save(self, match: Match, status: MatchStatus | str | None = None) -> None:
        """Compatibility alias for the nori repository API used by ``room.py``."""
        await self.upsert(match, status)

    async def get(self, match_id: str) -> Match | None:
        """Read one match by id; missing or corrupt snapshot -> None."""
        async with self.session() as session:
            row = await session.get(GamehubMatch, match_id)
            if row is None:
                return None
            return row_to_match(row)

    async def load_active(self, limit: int = 200) -> list[Match]:
        """Recently updated matches whose *derived* status is still active.

        The ``status`` column is the state at write time; legacy rows may say
        active after a resignation. Rows are re-derived through ``status_of``
        and terminal matches are dropped so a restart never revives them.
        """
        limit = max(1, int(limit))
        stmt = (
            select(GamehubMatch)
            .where(GamehubMatch.status == STATUS_ACTIVE)
            .order_by(GamehubMatch.updated_at.desc())
            .limit(limit)
        )
        async with self.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
            out: list[Match] = []
            for row in rows:
                match = row_to_match(row)
                if match is None:
                    continue
                try:
                    terminal = status_of(match).outcome is not None
                except Exception:  # noqa: BLE001 - unknown kind / bad snapshot
                    logger.warning(
                        "对局 %s 状态派生失败，跳过恢复",
                        getattr(match, "id", "?"),
                        exc_info=True,
                    )
                    continue
                if terminal:
                    continue
                out.append(match)
            return out

    async def mark_status(self, match_id: str, status: str) -> int:
        """Set the status column directly (also bumps ``updated_at``)."""
        stmt = (
            update(GamehubMatch)
            .where(GamehubMatch.match_id == match_id)
            .values(status=status, updated_at=_now())
        )
        async with self.session() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def archive_stale(self, days: int) -> int:
        """Archive ``over`` matches older than ``now - days``; returns the count."""
        cutoff = _now() - timedelta(days=max(0, int(days)))
        stmt = (
            update(GamehubMatch)
            .where(
                GamehubMatch.status == STATUS_OVER,
                GamehubMatch.updated_at < cutoff,
            )
            .values(status=STATUS_ARCHIVED, updated_at=_now())
        )
        async with self.session() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def delete(self, match_id: str) -> int:
        """Delete one match (idempotent); returns the deleted row count."""
        stmt = delete(GamehubMatch).where(GamehubMatch.match_id == match_id)
        async with self.session() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def close(self) -> None:
        """Dispose the engine; safe to call more than once."""
        engine, self._engine = self._engine, None
        self._session_maker = None
        if engine is not None:
            await engine.dispose()

    # ------------------------------------------------------------------
    #  Match-page chat window (gamehub_chat_lines)
    # ------------------------------------------------------------------

    async def chat_append(
        self, role: str, sender: str, text: str, *, ts_ms: int | None = None,
        match_id: str = "", uid: str = "",
    ) -> dict[str, Any]:
        """Append one chat line; returns the public line dict."""
        line = GamehubChatLine(
            match_id=match_id, uid=uid,
            role=str(role or "player")[:16],
            sender=str(sender or "")[:64],
            text=str(text or ""),
            created_at=(
                epoch_to_datetime(ts_ms)
                if ts_ms
                else datetime.now(timezone.utc)
            ),
        )
        async with self.session() as session:
            session.add(line)
            await session.commit()
            row_id = int(line.id)
            created = line.created_at
        # Hygiene: the chat window is a read model, not an archive — keep the
        # most recent lines only (cheap PK-range delete, runs per append).
        try:
            async with self.session() as session:
                result = await session.execute(
                    delete(GamehubChatLine).where(
                        GamehubChatLine.id < row_id - CHAT_KEEP_LINES
                    )
                )
                await session.commit()
                _ = result.rowcount
        except Exception:  # noqa: BLE001 - pruning must never break a send
            logger.debug("gamehub: chat prune failed", exc_info=True)
        return {
            "id": str(row_id),
            "ts": datetime_to_epoch(created),
            "role": line.role,
            "match_id": line.match_id,
            "uid": line.uid,
            "sender": line.sender,
            "text": line.text,
        }

    async def chat_recent(self, limit: int = 100, *, match_id: str | None = None) -> list[dict[str, Any]]:
        """Most recent chat lines, oldest first (page render order)."""
        limit = max(1, min(int(limit), 200))
        stmt = (
            select(GamehubChatLine)
            .order_by(GamehubChatLine.id.desc())
            .limit(limit)
        )
        if match_id is not None:
            stmt = stmt.where(GamehubChatLine.match_id == match_id)
        async with self.session() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()
        return [
            {
                "id": str(row.id),
                "ts": datetime_to_epoch(row.created_at),
                "role": row.role,
                "uid": row.uid,
                "match_id": row.match_id,
                "sender": row.sender,
                "text": row.text,
            }
            for row in rows
        ]
