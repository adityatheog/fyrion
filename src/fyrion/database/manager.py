"""
Asynchronous SQLite connection pool and data-access layer for Fyrion.

Why a pool?
Discord event handlers (member joins, message scanning) and slash commands run
concurrently on the event loop. Sharing a single ``aiosqlite`` connection
serializes every query behind one background thread, so a slow write blocks
otherwise unrelated reads. This module keeps a small, bounded set of
connections and hands them out on demand.

SQLite specifics handled here:

* ``journal_mode = WAL`` so readers never block the single writer.
* ``auto_vacuum = INCREMENTAL`` plus a periodic ``PRAGMA incremental_vacuum``,
  so deleted rows return their pages to the file without a blocking full
  ``VACUUM``. Switching an existing non-vacuuming database over requires one
  full ``VACUUM``, which is performed once at startup.
* ``busy_timeout`` so writers wait instead of raising "database is locked".
* ``foreign_keys = ON`` per connection (SQLite defaults to OFF), which is what
  makes the ``ON DELETE CASCADE`` guarantees in the schema real.
* Writes are funnelled through an asyncio lock, because SQLite still permits
  only one writer at a time. The lock is *not* reentrant: never call one of the
  write helpers from inside an open :meth:`DatabasePool.transaction` block; use
  the connection the context manager yields instead.

Security model:

* Values are always bound as parameters. No helper interpolates a value into
  SQL text.
* Identifiers cannot be parameterized in SQL, so every table, column and sort
  direction that reaches a statement is validated against the allow-lists in
  :mod:`fyrion.database.schema` first. An unknown identifier raises
  ``ValueError`` before any SQL is built.
* :meth:`DatabasePool.update` and :meth:`DatabasePool.delete` refuse to run
  without a filter unless ``allow_full_table=True`` is passed explicitly, so a
  forgotten ``where`` cannot wipe a table.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence

import aiosqlite

from fyrion.config import Config
from fyrion.database.schema import (
    GUILD_SCOPED_TABLES,
    INITIAL_SCHEMA,
    MODERATION_ACTIONS,
    PRIMARY_KEYS,
    SCHEMA_VERSION,
    TABLE_COLUMNS,
)

log = logging.getLogger("fyrion.database.manager")

DEFAULT_POOL_SIZE = 5
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 30.0
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 3600.0

# Hard ceiling for a single SELECT, so a bad caller cannot pull an entire table
# into memory by accident.
MAX_FETCH_LIMIT = 10_000

# Each connection to an in-memory database gets its own private database file,
# so pooling is meaningless (and actively harmful) for these URLs.
IN_MEMORY_URLS = frozenset(
    {"", ":memory:", "file::memory:", "file::memory:?cache=shared"}
)

# Supported INSERT conflict resolutions, mapped to their SQL fragment.
_CONFLICT_CLAUSES = {
    "abort": "",
    "ignore": " OR IGNORE",
    "replace": " OR REPLACE",
    "rollback": " OR ROLLBACK",
}

_SORT_DIRECTIONS = frozenset({"ASC", "DESC"})


class DatabaseError(RuntimeError):
    """Base class for Fyrion data-access errors."""


class PoolNotConnectedError(DatabaseError):
    """Raised when a query is issued before ``connect()`` or after ``close()``."""


class InsufficientFundsError(DatabaseError):
    """Raised when an economy debit would drive an account negative."""


def utc_now_iso() -> str:
    """Returns the current UTC time in the format the schema stores."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_now(seconds: float) -> str:
    """Returns an ISO-8601 UTC timestamp ``seconds`` in the future."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_int(name: str, default: int) -> int:
    """Reads an integer environment variable, falling back on invalid input."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid integer for %s=%r; using default %s.", name, raw, default)
        return default


def _quote(identifier: str) -> str:
    """Quotes an already validated identifier.

    Callers must have checked the name against the schema allow-list first; the
    embedded assertion documents that contract and catches misuse in tests.
    """
    if '"' in identifier:
        raise ValueError(f"Illegal identifier: {identifier!r}")
    return f'"{identifier}"'


class DatabasePool:
    """A bounded pool of asynchronous SQLite connections plus CRUD helpers."""

    def __init__(
        self,
        db_url: str | None = None,
        pool_size: int | None = None,
        busy_timeout_ms: int | None = None,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
    ) -> None:
        self.db_url: str = db_url if db_url is not None else Config.DATABASE_URL

        self.busy_timeout_ms: int = max(
            0,
            busy_timeout_ms
            if busy_timeout_ms is not None
            else _env_int("DATABASE_BUSY_TIMEOUT_MS", DEFAULT_BUSY_TIMEOUT_MS),
        )
        self.acquire_timeout: float = acquire_timeout

        requested = (
            pool_size
            if pool_size is not None
            else _env_int("DATABASE_POOL_SIZE", DEFAULT_POOL_SIZE)
        )
        self.pool_size: int = max(1, requested)

        self._in_memory = self.db_url in IN_MEMORY_URLS
        if self._in_memory and self.pool_size != 1:
            log.warning("In-memory database detected; forcing pool_size=1.")
            self.pool_size = 1

        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(
            maxsize=self.pool_size
        )
        self._connections: list[aiosqlite.Connection] = []
        self._write_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return bool(self._connections) and not self._closed

    @property
    def in_memory(self) -> bool:
        return self._in_memory

    async def connect(self) -> None:
        """Opens every pooled connection, tunes SQLite and applies the schema."""
        if self._connections:
            log.debug("Pool already initialized; ignoring duplicate connect().")
            return

        log.info(
            "Opening SQLite pool (%d connection(s)) at %s",
            self.pool_size,
            self.db_url,
        )
        self._closed = False

        try:
            first = await self._new_connection()
            self._connections.append(first)
            # auto_vacuum must be configured before the schema creates pages.
            await self._configure_auto_vacuum(first)
            self._pool.put_nowait(first)

            for _ in range(self.pool_size - 1):
                conn = await self._new_connection()
                self._connections.append(conn)
                self._pool.put_nowait(conn)
        except Exception:
            # Never leak half-open connections if one of them fails.
            await self.close()
            raise

        await self._apply_schema()
        log.info(
            "Database pool ready; schema version %d verified.", SCHEMA_VERSION
        )

    async def _new_connection(self) -> aiosqlite.Connection:
        """Creates a single connection with Fyrion's required PRAGMAs applied."""
        conn = await aiosqlite.connect(
            self.db_url,
            timeout=max(1.0, self.busy_timeout_ms / 1000.0),
        )

        # Row factory allows column access by name, like a mapping.
        conn.row_factory = aiosqlite.Row

        # PRAGMA arguments cannot be parameterized; the timeout is coerced to
        # int so nothing but a number can reach the statement.
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)};")
        await conn.execute("PRAGMA temp_store = MEMORY;")

        if not self._in_memory:
            await conn.execute("PRAGMA journal_mode = WAL;")
            await conn.execute("PRAGMA synchronous = NORMAL;")
            await conn.execute("PRAGMA wal_autocheckpoint = 1000;")

        await conn.commit()
        return conn

    async def _configure_auto_vacuum(self, conn: aiosqlite.Connection) -> None:
        """Ensures the database uses incremental auto-vacuuming.

        ``auto_vacuum`` can only be changed on an empty database, or on an
        existing one by running a full ``VACUUM``. The rebuild happens once, at
        startup, before the bot connects to the gateway.
        """
        try:
            async with conn.execute("PRAGMA auto_vacuum;") as cursor:
                row = await cursor.fetchone()
            current = int(row[0]) if row is not None else 0

            if current == 2:  # already INCREMENTAL
                return

            await conn.execute("PRAGMA auto_vacuum = INCREMENTAL;")
            await conn.commit()

            if self._in_memory:
                return

            if await self._has_user_tables(conn):
                log.info(
                    "Rebuilding the database once to enable incremental "
                    "auto-vacuuming; this may take a moment."
                )
                # VACUUM must run outside a transaction.
                await conn.execute("VACUUM;")
                await conn.commit()
        except sqlite3.Error as exc:
            # Page reclamation is an optimization, never a startup blocker.
            log.warning("Could not configure auto-vacuuming: %s", exc)

    @staticmethod
    async def _has_user_tables(conn: aiosqlite.Connection) -> bool:
        query = (
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        async with conn.execute(query) as cursor:
            row = await cursor.fetchone()
        return bool(row is not None and int(row[0]) > 0)

    async def _apply_schema(self) -> None:
        """Applies the idempotent DDL and records the schema version."""
        async with self.transaction() as conn:
            await conn.executescript(INITIAL_SCHEMA)
            # user_version is an integer PRAGMA; the value is a module constant.
            await conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)};")
            await conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            await conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('last_started_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (utc_now_iso(),),
            )

    async def close(self) -> None:
        """Closes every pooled connection. Safe to call more than once."""
        if self._closed and not self._connections:
            return

        self._closed = True
        await self.stop_maintenance()

        for conn in self._connections:
            try:
                await conn.close()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                log.warning("Error while closing a pooled connection: %s", exc)

        self._connections.clear()
        while not self._pool.empty():
            self._pool.get_nowait()

        log.info("Database pool closed.")

    # ------------------------------------------------------------------
    # Connection checkout
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        """Checks out a connection for the duration of the context.

        The connection is always returned to the pool, including on error, so a
        failing query cannot shrink the pool.
        """
        if not self.is_connected:
            raise PoolNotConnectedError("Database pool is not connected.")

        try:
            conn = await asyncio.wait_for(self._pool.get(), self.acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise DatabaseError(
                f"Timed out after {self.acquire_timeout}s waiting for a pooled "
                "database connection."
            ) from exc

        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Runs a write transaction: commit on success, rollback on error.

        Writes are serialized with a non-reentrant lock, because SQLite allows a
        single writer. Use the yielded connection for every statement inside the
        block; calling :meth:`execute` here would deadlock.
        """
        async with self._write_lock:
            async with self.acquire() as conn:
                try:
                    yield conn
                except BaseException:
                    await conn.rollback()
                    raise
                else:
                    await conn.commit()

    # ------------------------------------------------------------------
    # Low-level query helpers (all parameterized)
    # ------------------------------------------------------------------

    async def execute(self, query: str, parameters: Iterable[Any] = ()) -> int:
        """Runs an INSERT/UPDATE/DELETE and commits. Returns rows affected."""
        async with self.transaction() as conn:
            async with conn.execute(query, tuple(parameters)) as cursor:
                return cursor.rowcount

    async def executemany(
        self, query: str, parameters: Iterable[Sequence[Any]]
    ) -> int:
        """Runs a batched write and commits. Returns rows affected."""
        rows = [tuple(item) for item in parameters]
        if not rows:
            return 0
        async with self.transaction() as conn:
            async with conn.executemany(query, rows) as cursor:
                return cursor.rowcount

    async def insert_returning_id(
        self, query: str, parameters: Iterable[Any] = ()
    ) -> int | None:
        """Runs an INSERT and returns the generated rowid."""
        async with self.transaction() as conn:
            async with conn.execute(query, tuple(parameters)) as cursor:
                return cursor.lastrowid

    async def fetchrow(
        self, query: str, parameters: Iterable[Any] = ()
    ) -> aiosqlite.Row | None:
        """Fetches a single row, or None."""
        async with self.acquire() as conn:
            async with conn.execute(query, tuple(parameters)) as cursor:
                return await cursor.fetchone()

    async def fetchall(
        self, query: str, parameters: Iterable[Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetches every matching row."""
        async with self.acquire() as conn:
            async with conn.execute(query, tuple(parameters)) as cursor:
                return list(await cursor.fetchall())

    async def fetchval(
        self, query: str, parameters: Iterable[Any] = (), default: Any = None
    ) -> Any:
        """Fetches the first column of the first row, or ``default``."""
        row = await self.fetchrow(query, parameters)
        if row is None:
            return default
        return row[0]

    async def ping(self) -> bool:
        """Returns True when the pool can serve a trivial query."""
        try:
            return await self.fetchval("SELECT 1") == 1
        except (DatabaseError, sqlite3.Error):
            return False

    # ------------------------------------------------------------------
    # Identifier validation
    # ------------------------------------------------------------------

    @staticmethod
    def _columns_for(table: str) -> frozenset[str]:
        try:
            return TABLE_COLUMNS[table]
        except KeyError:
            raise ValueError(f"Unknown table: {table!r}") from None

    @classmethod
    def _check_table(cls, table: str) -> None:
        """Rejects any table not in the allow-list before SQL is built.

        The column-level helpers only run when there are columns to check, so a
        no-column/no-where path (e.g. ``fetch_one(table, {})``) would otherwise
        never validate the table name. Call this first in every public helper.
        """
        cls._columns_for(table)

    @classmethod
    def _check_columns(cls, table: str, columns: Iterable[str]) -> None:
        allowed = cls._columns_for(table)
        unknown = sorted(set(columns) - allowed)
        if unknown:
            raise ValueError(f"Unknown column(s) for {table}: {unknown}")

    @classmethod
    def _where_clause(
        cls, table: str, where: Mapping[str, Any] | None
    ) -> tuple[str, list[Any]]:
        """Builds a conjunctive WHERE clause with bound parameters.

        ``None`` becomes ``IS NULL``; a list/tuple/set becomes ``IN (...)``.
        """
        if not where:
            return "", []

        cls._check_columns(table, where)

        clauses: list[str] = []
        params: list[Any] = []
        for column, value in where.items():
            quoted = _quote(column)
            if value is None:
                clauses.append(f"{quoted} IS NULL")
            elif isinstance(value, (list, tuple, set, frozenset)):
                items = list(value)
                if not items:
                    # An empty IN () is a syntax error and matches nothing.
                    clauses.append("0 = 1")
                    continue
                placeholders = ", ".join(["?"] * len(items))
                clauses.append(f"{quoted} IN ({placeholders})")
                params.extend(items)
            else:
                clauses.append(f"{quoted} = ?")
                params.append(value)

        return " WHERE " + " AND ".join(clauses), params

    @classmethod
    def _order_clause(
        cls, table: str, order_by: str | Sequence[str] | None
    ) -> str:
        if not order_by:
            return ""

        items = [order_by] if isinstance(order_by, str) else list(order_by)
        parts: list[str] = []
        for item in items:
            tokens = item.split()
            if not tokens or len(tokens) > 2:
                raise ValueError(f"Invalid ORDER BY expression: {item!r}")
            column = tokens[0]
            direction = tokens[1].upper() if len(tokens) == 2 else "ASC"
            if direction not in _SORT_DIRECTIONS:
                raise ValueError(f"Invalid sort direction: {tokens[1]!r}")
            cls._check_columns(table, [column])
            parts.append(f"{_quote(column)} {direction}")

        return " ORDER BY " + ", ".join(parts)

    @classmethod
    def _select_columns(
        cls, table: str, columns: Sequence[str] | None
    ) -> str:
        if not columns:
            return "*"
        cls._check_columns(table, columns)
        return ", ".join(_quote(column) for column in columns)

    @staticmethod
    def _limit_clause(limit: int | None, offset: int | None) -> str:
        if limit is None and offset is None:
            return ""

        effective_limit = MAX_FETCH_LIMIT if limit is None else int(limit)
        if effective_limit <= 0 or effective_limit > MAX_FETCH_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {MAX_FETCH_LIMIT}, got {limit!r}"
            )

        clause = f" LIMIT {effective_limit}"
        if offset is not None:
            effective_offset = int(offset)
            if effective_offset < 0:
                raise ValueError("offset must be zero or positive")
            clause += f" OFFSET {effective_offset}"
        return clause

    async def _ensure_parent_rows(
        self, table: str, values: Mapping[str, Any]
    ) -> None:
        """Creates the ``guild_settings`` parent row for guild-scoped inserts.

        Foreign keys are enforced, so a child row cannot be written before its
        guild exists. Doing this here keeps every call site from repeating it.
        """
        if table in GUILD_SCOPED_TABLES and values.get("guild_id") is not None:
            await self.ensure_guild(int(values["guild_id"]))

    # ------------------------------------------------------------------
    # Generic CRUD
    # ------------------------------------------------------------------

    async def insert(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        on_conflict: str = "abort",
    ) -> int | None:
        """Inserts one row and returns its rowid (None when nothing was written)."""
        if not values:
            raise ValueError("insert() requires at least one column.")
        self._check_table(table)
        self._check_columns(table, values)

        clause = _CONFLICT_CLAUSES.get(on_conflict.lower())
        if clause is None:
            raise ValueError(f"Unsupported on_conflict value: {on_conflict!r}")

        await self._ensure_parent_rows(table, values)

        columns = list(values)
        column_sql = ", ".join(_quote(column) for column in columns)
        placeholders = ", ".join(["?"] * len(columns))
        query = (
            f"INSERT{clause} INTO {_quote(table)} ({column_sql}) "
            f"VALUES ({placeholders})"
        )

        async with self.transaction() as conn:
            async with conn.execute(
                query, tuple(values[column] for column in columns)
            ) as cursor:
                if cursor.rowcount == 0:
                    return None
                return cursor.lastrowid

    async def upsert(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        conflict_columns: Sequence[str] | None = None,
        update_columns: Sequence[str] | None = None,
    ) -> None:
        """Inserts a row, updating it in place when the conflict target matches.

        ``conflict_columns`` defaults to the table's primary key.
        ``update_columns`` defaults to every supplied column that is not part of
        the conflict target; pass an empty sequence for insert-or-nothing.
        """
        if not values:
            raise ValueError("upsert() requires at least one column.")
        self._check_table(table)
        self._check_columns(table, values)

        keys = tuple(conflict_columns) if conflict_columns else PRIMARY_KEYS[table]
        self._check_columns(table, keys)

        if update_columns is None:
            updates = [column for column in values if column not in keys]
        else:
            self._check_columns(table, update_columns)
            updates = list(update_columns)

        await self._ensure_parent_rows(table, values)

        columns = list(values)
        column_sql = ", ".join(_quote(column) for column in columns)
        placeholders = ", ".join(["?"] * len(columns))
        conflict_sql = ", ".join(_quote(column) for column in keys)

        if updates:
            assignments = ", ".join(
                f"{_quote(column)} = excluded.{_quote(column)}" for column in updates
            )
            resolution = f"DO UPDATE SET {assignments}"
        else:
            resolution = "DO NOTHING"

        query = (
            f"INSERT INTO {_quote(table)} ({column_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_sql}) {resolution}"
        )
        await self.execute(query, tuple(values[column] for column in columns))

    async def fetch_one(
        self,
        table: str,
        where: Mapping[str, Any] | None = None,
        *,
        columns: Sequence[str] | None = None,
        order_by: str | Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Returns a single row as a plain dict, or None."""
        self._check_table(table)
        selection = self._select_columns(table, columns)
        where_sql, params = self._where_clause(table, where)
        order_sql = self._order_clause(table, order_by)
        query = (
            f"SELECT {selection} FROM {_quote(table)}{where_sql}{order_sql} LIMIT 1"
        )
        row = await self.fetchrow(query, params)
        return dict(row) if row is not None else None

    async def fetch_many(
        self,
        table: str,
        where: Mapping[str, Any] | None = None,
        *,
        columns: Sequence[str] | None = None,
        order_by: str | Sequence[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Returns matching rows as plain dicts."""
        self._check_table(table)
        selection = self._select_columns(table, columns)
        where_sql, params = self._where_clause(table, where)
        order_sql = self._order_clause(table, order_by)
        limit_sql = self._limit_clause(limit, offset)
        query = (
            f"SELECT {selection} FROM {_quote(table)}"
            f"{where_sql}{order_sql}{limit_sql}"
        )
        rows = await self.fetchall(query, params)
        return [dict(row) for row in rows]

    async def update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: Mapping[str, Any] | None = None,
        *,
        allow_full_table: bool = False,
    ) -> int:
        """Updates matching rows and returns how many changed."""
        if not values:
            raise ValueError("update() requires at least one column.")
        self._check_table(table)
        if not where and not allow_full_table:
            raise ValueError(
                "Refusing to update every row; pass a filter or "
                "allow_full_table=True."
            )

        self._check_columns(table, values)
        where_sql, where_params = self._where_clause(table, where)

        assignments = ", ".join(f"{_quote(column)} = ?" for column in values)
        query = f"UPDATE {_quote(table)} SET {assignments}{where_sql}"
        params = [*values.values(), *where_params]
        return await self.execute(query, params)

    async def delete(
        self,
        table: str,
        where: Mapping[str, Any] | None = None,
        *,
        allow_full_table: bool = False,
    ) -> int:
        """Deletes matching rows and returns how many were removed."""
        self._check_table(table)
        if not where and not allow_full_table:
            raise ValueError(
                "Refusing to delete every row; pass a filter or "
                "allow_full_table=True."
            )

        where_sql, params = self._where_clause(table, where)
        query = f"DELETE FROM {_quote(table)}{where_sql}"
        return await self.execute(query, params)

    async def count(
        self, table: str, where: Mapping[str, Any] | None = None
    ) -> int:
        """Counts matching rows."""
        self._check_table(table)
        where_sql, params = self._where_clause(table, where)
        query = f"SELECT COUNT(*) FROM {_quote(table)}{where_sql}"
        return int(await self.fetchval(query, params, default=0))

    async def exists(self, table: str, where: Mapping[str, Any]) -> bool:
        """Returns True when at least one row matches."""
        self._check_table(table)
        where_sql, params = self._where_clause(table, where)
        query = f"SELECT 1 FROM {_quote(table)}{where_sql} LIMIT 1"
        return await self.fetchrow(query, params) is not None

    async def increment(
        self,
        table: str,
        column: str,
        amount: int,
        where: Mapping[str, Any],
    ) -> int:
        """Adds ``amount`` to a numeric column in place. Returns rows changed.

        Read-modify-write in Python would race with concurrent handlers; doing
        the arithmetic in SQL keeps the update atomic.
        """
        if not where:
            raise ValueError("increment() requires a filter.")
        self._check_table(table)
        self._check_columns(table, [column])
        where_sql, params = self._where_clause(table, where)
        quoted = _quote(column)
        query = (
            f"UPDATE {_quote(table)} SET {quoted} = {quoted} + ?{where_sql}"
        )
        return await self.execute(query, [int(amount), *params])

    # ------------------------------------------------------------------
    # guild_settings
    # ------------------------------------------------------------------

    async def ensure_guild(self, guild_id: int) -> None:
        """Creates the ``guild_settings`` parent row for a guild, if missing.

        Every guild-scoped core table cascades from ``guild_settings``, so this
        one insert is enough. The few remaining legacy repositories that still
        hold a foreign key onto ``guild_configs`` create their own parent row
        (``INSERT OR IGNORE INTO guild_configs``) before writing.
        """
        async with self.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
                (int(guild_id),),
            )

    async def get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        """Returns a guild's settings, creating defaults on first access."""
        settings = await self.fetch_one("guild_settings", {"guild_id": guild_id})
        if settings is not None:
            return settings

        await self.ensure_guild(guild_id)
        settings = await self.fetch_one("guild_settings", {"guild_id": guild_id})
        if settings is None:  # pragma: no cover - only on a failed insert
            raise DatabaseError(f"Could not create settings for guild {guild_id}.")
        return settings

    async def update_guild_settings(self, guild_id: int, **values: Any) -> int:
        """Updates one or more guild settings. Column names are validated."""
        await self.ensure_guild(guild_id)
        return await self.update("guild_settings", values, {"guild_id": guild_id})

    async def delete_guild(self, guild_id: int) -> None:
        """Removes a guild and, by cascade, all of its guild-scoped data."""
        async with self.transaction() as conn:
            # Tickets carry no foreign key on purpose, so clean them explicitly.
            await conn.execute("DELETE FROM tickets WHERE guild_id = ?", (guild_id,))
            await conn.execute(
                "DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )

    # ------------------------------------------------------------------
    # moderation_cases
    # ------------------------------------------------------------------

    async def create_moderation_case(
        self,
        *,
        guild_id: int,
        action: str,
        target_id: int,
        moderator_id: int,
        reason: str | None = None,
        target_tag: str | None = None,
        evidence: str | None = None,
        duration_seconds: int | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Records a moderation case and returns it, including its case number.

        The number is allocated and used inside one transaction, so two
        concurrent moderators cannot receive the same case number.
        """
        if action not in MODERATION_ACTIONS:
            raise ValueError(f"Unsupported moderation action: {action!r}")
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive when provided.")

        if expires_at is None and duration_seconds is not None:
            expires_at = iso_from_now(duration_seconds)

        await self.ensure_guild(guild_id)

        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT COALESCE(MAX(case_number), 0) + 1 FROM moderation_cases "
                "WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                row = await cursor.fetchone()
            case_number = int(row[0]) if row is not None else 1

            await conn.execute(
                "INSERT INTO moderation_cases ("
                "    guild_id, case_number, action, target_id, target_tag, "
                "    moderator_id, reason, evidence, duration_seconds, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id,
                    case_number,
                    action,
                    target_id,
                    target_tag,
                    moderator_id,
                    reason,
                    evidence,
                    duration_seconds,
                    expires_at,
                ),
            )

            async with conn.execute(
                "SELECT * FROM moderation_cases WHERE guild_id = ? AND case_number = ?",
                (guild_id, case_number),
            ) as cursor:
                created = await cursor.fetchone()

        if created is None:  # pragma: no cover - insert already succeeded
            raise DatabaseError("Moderation case disappeared after insertion.")
        return dict(created)

    async def get_moderation_case(
        self, guild_id: int, case_number: int
    ) -> dict[str, Any] | None:
        return await self.fetch_one(
            "moderation_cases", {"guild_id": guild_id, "case_number": case_number}
        )

    async def get_member_cases(
        self,
        guild_id: int,
        target_id: int,
        *,
        action: str | Sequence[str] | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"guild_id": guild_id, "target_id": target_id}
        if action is not None:
            where["action"] = action
        if active_only:
            where["active"] = 1
        return await self.fetch_many(
            "moderation_cases",
            where,
            order_by="case_number DESC",
            limit=limit,
        )

    async def resolve_moderation_case(
        self, guild_id: int, case_number: int, resolved_by: int | None = None
    ) -> bool:
        """Marks a case inactive (a ban lifted, a timeout expired)."""
        changed = await self.update(
            "moderation_cases",
            {
                "active": 0,
                "resolved_at": utc_now_iso(),
                "resolved_by": resolved_by,
            },
            {"guild_id": guild_id, "case_number": case_number, "active": 1},
        )
        return changed > 0

    async def get_expired_cases(self, limit: int = 100) -> list[dict[str, Any]]:
        """Returns active, time-limited cases whose expiry has passed."""
        query = (
            "SELECT * FROM moderation_cases "
            "WHERE active = 1 AND expires_at IS NOT NULL AND expires_at <= ? "
            "ORDER BY expires_at ASC LIMIT ?"
        )
        rows = await self.fetchall(query, (utc_now_iso(), int(limit)))
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # automod_rules
    # ------------------------------------------------------------------

    async def get_automod_rules(
        self, guild_id: int, *, enabled_only: bool = True
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"guild_id": guild_id}
        if enabled_only:
            where["enabled"] = 1
        return await self.fetch_many("automod_rules", where, order_by="rule_id ASC")

    # ------------------------------------------------------------------
    # economy_accounts
    # ------------------------------------------------------------------

    async def get_economy_account(
        self, guild_id: int, user_id: int
    ) -> dict[str, Any]:
        """Returns a wallet, creating an empty one on first access."""
        account = await self.fetch_one(
            "economy_accounts", {"guild_id": guild_id, "user_id": user_id}
        )
        if account is not None:
            return account

        await self.ensure_guild(guild_id)
        await self.execute(
            "INSERT OR IGNORE INTO economy_accounts (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        account = await self.fetch_one(
            "economy_accounts", {"guild_id": guild_id, "user_id": user_id}
        )
        if account is None:  # pragma: no cover - only on a failed insert
            raise DatabaseError(
                f"Could not create an economy account for {user_id} in {guild_id}."
            )
        return account

    async def adjust_balance(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        field: str = "balance",
    ) -> int:
        """Credits (positive) or debits (negative) a wallet atomically.

        Returns the new value of ``field``. Raises
        :class:`InsufficientFundsError` when a debit would go negative; the
        guard is part of the SQL, so two concurrent debits cannot both succeed.
        """
        if field not in {"balance", "bank"}:
            raise ValueError("field must be 'balance' or 'bank'.")
        amount = int(amount)

        await self.get_economy_account(guild_id, user_id)

        quoted = _quote(field)
        earned = amount if amount > 0 else 0
        spent = -amount if amount < 0 else 0

        async with self.transaction() as conn:
            async with conn.execute(
                f"UPDATE economy_accounts "
                f"   SET {quoted} = {quoted} + ?, "
                f"       total_earned = total_earned + ?, "
                f"       total_spent = total_spent + ? "
                f" WHERE guild_id = ? AND user_id = ? AND {quoted} + ? >= 0",
                (amount, earned, spent, guild_id, user_id, amount),
            ) as cursor:
                changed = cursor.rowcount

            if changed == 0:
                raise InsufficientFundsError(
                    f"User {user_id} does not have {abs(amount)} available "
                    f"in {field}."
                )

            async with conn.execute(
                f"SELECT {quoted} FROM economy_accounts "
                f"WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()

        return int(row[0]) if row is not None else 0

    async def transfer_balance(
        self, guild_id: int, sender_id: int, recipient_id: int, amount: int
    ) -> None:
        """Moves currency between two wallets in a single transaction."""
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Transfer amount must be positive.")
        if sender_id == recipient_id:
            raise ValueError("Cannot transfer currency to the same account.")

        await self.get_economy_account(guild_id, sender_id)
        await self.get_economy_account(guild_id, recipient_id)

        async with self.transaction() as conn:
            async with conn.execute(
                "UPDATE economy_accounts "
                "   SET balance = balance - ?, total_spent = total_spent + ? "
                " WHERE guild_id = ? AND user_id = ? AND balance - ? >= 0",
                (amount, amount, guild_id, sender_id, amount),
            ) as cursor:
                if cursor.rowcount == 0:
                    raise InsufficientFundsError(
                        f"User {sender_id} cannot afford {amount}."
                    )

            await conn.execute(
                "UPDATE economy_accounts "
                "   SET balance = balance + ?, total_earned = total_earned + ? "
                " WHERE guild_id = ? AND user_id = ?",
                (amount, amount, guild_id, recipient_id),
            )

    async def economy_leaderboard(
        self, guild_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT guild_id, user_id, balance, bank, (balance + bank) AS net_worth "
            "FROM economy_accounts WHERE guild_id = ? "
            "ORDER BY net_worth DESC, user_id ASC LIMIT ?"
        )
        rows = await self.fetchall(query, (guild_id, int(limit)))
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # leveling_profiles / level_rewards
    # ------------------------------------------------------------------

    async def get_leveling_profile(
        self, guild_id: int, user_id: int
    ) -> dict[str, Any]:
        profile = await self.fetch_one(
            "leveling_profiles", {"guild_id": guild_id, "user_id": user_id}
        )
        if profile is not None:
            return profile

        await self.ensure_guild(guild_id)
        await self.execute(
            "INSERT OR IGNORE INTO leveling_profiles (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        profile = await self.fetch_one(
            "leveling_profiles", {"guild_id": guild_id, "user_id": user_id}
        )
        if profile is None:  # pragma: no cover - only on a failed insert
            raise DatabaseError(
                f"Could not create a leveling profile for {user_id} in {guild_id}."
            )
        return profile

    async def add_xp(
        self, guild_id: int, user_id: int, amount: int, *, count_message: bool = True
    ) -> dict[str, Any]:
        """Awards XP atomically and returns the updated profile.

        Level thresholds are a policy decision that belongs in the leveling cog;
        this method only stores what the cog computed, via ``set_level``.
        """
        amount = int(amount)
        if amount < 0:
            raise ValueError("XP awards cannot be negative.")

        await self.ensure_guild(guild_id)
        now = utc_now_iso()

        async with self.transaction() as conn:
            await conn.execute(
                "INSERT INTO leveling_profiles ("
                "    guild_id, user_id, xp, total_messages, last_message_at, last_xp_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                "    xp = xp + excluded.xp, "
                "    total_messages = total_messages + excluded.total_messages, "
                "    last_message_at = excluded.last_message_at, "
                "    last_xp_at = excluded.last_xp_at",
                (
                    guild_id,
                    user_id,
                    amount,
                    1 if count_message else 0,
                    now,
                    now,
                ),
            )
            async with conn.execute(
                "SELECT * FROM leveling_profiles WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:  # pragma: no cover - upsert already succeeded
            raise DatabaseError("Leveling profile disappeared after an XP award.")
        return dict(row)

    async def set_level(self, guild_id: int, user_id: int, level: int) -> None:
        level = int(level)
        if level < 0:
            raise ValueError("Level cannot be negative.")
        await self.update(
            "leveling_profiles",
            {"level": level},
            {"guild_id": guild_id, "user_id": user_id},
        )

    async def leveling_leaderboard(
        self, guild_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        return await self.fetch_many(
            "leveling_profiles",
            {"guild_id": guild_id},
            order_by=["xp DESC", "user_id ASC"],
            limit=limit,
        )

    async def get_level_rewards(
        self, guild_id: int, *, up_to_level: int | None = None
    ) -> list[dict[str, Any]]:
        """Returns reward rows, optionally only those already earned."""
        if up_to_level is None:
            return await self.fetch_many(
                "level_rewards", {"guild_id": guild_id}, order_by="level ASC"
            )

        query = (
            "SELECT * FROM level_rewards WHERE guild_id = ? AND level <= ? "
            "ORDER BY level ASC"
        )
        rows = await self.fetchall(query, (guild_id, int(up_to_level)))
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # giveaways
    # ------------------------------------------------------------------

    async def get_due_giveaways(self, limit: int = 25) -> list[dict[str, Any]]:
        """Returns active giveaways whose end time has passed."""
        query = (
            "SELECT * FROM giveaways WHERE status = 'active' AND ends_at <= ? "
            "ORDER BY ends_at ASC LIMIT ?"
        )
        rows = await self.fetchall(query, (utc_now_iso(), int(limit)))
        return [dict(row) for row in rows]

    async def add_giveaway_entry(
        self, giveaway_id: int, user_id: int, entries: int = 1
    ) -> bool:
        """Records an entry. Returns False when the user already entered.

        The unique index on ``(giveaway_id, user_id)`` is what actually enforces
        one entry per user, so a double click cannot create two rows.
        """
        entries = int(entries)
        if entries <= 0:
            raise ValueError("Entry weight must be positive.")

        async with self.transaction() as conn:
            async with conn.execute(
                "INSERT OR IGNORE INTO giveaway_entries "
                "(giveaway_id, user_id, entries) VALUES (?, ?, ?)",
                (giveaway_id, user_id, entries),
            ) as cursor:
                inserted = cursor.rowcount > 0

            if inserted:
                await conn.execute(
                    "UPDATE giveaways SET entry_count = entry_count + ? "
                    "WHERE giveaway_id = ?",
                    (entries, giveaway_id),
                )

        return inserted

    async def remove_giveaway_entry(self, giveaway_id: int, user_id: int) -> bool:
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT entries FROM giveaway_entries "
                "WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                return False

            weight = int(row[0])
            await conn.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, user_id),
            )
            await conn.execute(
                "UPDATE giveaways "
                "   SET entry_count = MAX(0, entry_count - ?) "
                " WHERE giveaway_id = ?",
                (weight, giveaway_id),
            )
        return True

    async def get_giveaway_entries(self, giveaway_id: int) -> list[dict[str, Any]]:
        return await self.fetch_many(
            "giveaway_entries", {"giveaway_id": giveaway_id}, order_by="entry_id ASC"
        )

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------

    async def create_support_ticket(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        subject: str | None = None,
        panel_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Opens a ticket with a per-guild sequential number."""
        await self.ensure_guild(guild_id)

        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT COALESCE(MAX(ticket_number), 0) + 1 FROM tickets "
                "WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                row = await cursor.fetchone()
            ticket_number = int(row[0]) if row is not None else 1

            async with conn.execute(
                "INSERT INTO tickets ("
                "    guild_id, ticket_number, channel_id, user_id, subject, "
                "    panel_message_id, status"
                ") VALUES (?, ?, ?, ?, ?, ?, 'open')",
                (
                    guild_id,
                    ticket_number,
                    channel_id,
                    user_id,
                    subject,
                    panel_message_id,
                ),
            ) as cursor:
                ticket_id = cursor.lastrowid

            async with conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ) as cursor:
                created = await cursor.fetchone()

        if created is None:  # pragma: no cover - insert already succeeded
            raise DatabaseError("Ticket disappeared after insertion.")
        return dict(created)

    async def get_open_ticket(
        self, guild_id: int, user_id: int
    ) -> dict[str, Any] | None:
        return await self.fetch_one(
            "tickets",
            {"guild_id": guild_id, "user_id": user_id, "status": ["open", "claimed"]},
            order_by="ticket_id DESC",
        )

    async def claim_ticket(self, channel_id: int, claimed_by: int) -> bool:
        changed = await self.update(
            "tickets",
            {"status": "claimed", "claimed_by": claimed_by},
            {"channel_id": channel_id, "status": "open"},
        )
        return changed > 0

    async def close_support_ticket(
        self,
        channel_id: int,
        *,
        closed_by: int | None = None,
        close_reason: str | None = None,
        transcript: str | None = None,
    ) -> bool:
        changed = await self.update(
            "tickets",
            {
                "status": "closed",
                "closed_by": closed_by,
                "close_reason": close_reason,
                "transcript": transcript,
                "closed_at": utc_now_iso(),
            },
            {"channel_id": channel_id, "status": ["open", "claimed"]},
        )
        return changed > 0

    # ------------------------------------------------------------------
    # custom_commands
    # ------------------------------------------------------------------

    async def get_custom_command(
        self, guild_id: int, name: str
    ) -> dict[str, Any] | None:
        # Names are stored lower-cased by the cog; normalize on read too so a
        # differently cased invocation still resolves.
        return await self.fetch_one(
            "custom_commands", {"guild_id": guild_id, "name": name.lower()}
        )

    async def bump_custom_command_uses(self, command_id: int) -> None:
        await self.increment(
            "custom_commands", "uses", 1, {"command_id": command_id}
        )

    # ------------------------------------------------------------------
    # reaction_roles
    # ------------------------------------------------------------------

    async def get_reaction_role(
        self, message_id: int, emoji: str
    ) -> dict[str, Any] | None:
        return await self.fetch_one(
            "reaction_roles", {"message_id": message_id, "emoji": emoji}
        )

    async def get_reaction_role_group(
        self, guild_id: int, group_key: str
    ) -> list[dict[str, Any]]:
        return await self.fetch_many(
            "reaction_roles",
            {"guild_id": guild_id, "group_key": group_key},
            order_by="entry_id ASC",
        )

    # ------------------------------------------------------------------
    # dashboard_sessions
    # ------------------------------------------------------------------

    async def create_dashboard_session(
        self,
        *,
        session_id: str,
        user_id: int,
        token_hash: str,
        expires_at: str,
        refresh_token_hash: str | None = None,
        scopes: str = "",
        ip_hash: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Stores a dashboard session.

        Only hashes are accepted: the caller must hash the session token, the
        refresh token and the client IP before calling this, so a database leak
        cannot be replayed as a valid login.
        """
        await self.insert(
            "dashboard_sessions",
            {
                "session_id": session_id,
                "user_id": user_id,
                "token_hash": token_hash,
                "refresh_token_hash": refresh_token_hash,
                "scopes": scopes,
                "ip_hash": ip_hash,
                "user_agent": user_agent,
                "expires_at": expires_at,
            },
        )

    async def get_active_session(self, token_hash: str) -> dict[str, Any] | None:
        """Returns a session only when it is neither revoked nor expired."""
        query = (
            "SELECT * FROM dashboard_sessions "
            "WHERE token_hash = ? AND revoked = 0 AND expires_at > ?"
        )
        row = await self.fetchrow(query, (token_hash, utc_now_iso()))
        return dict(row) if row is not None else None

    async def touch_session(self, token_hash: str) -> bool:
        changed = await self.update(
            "dashboard_sessions",
            {"last_seen_at": utc_now_iso()},
            {"token_hash": token_hash, "revoked": 0},
        )
        return changed > 0

    async def revoke_session(self, token_hash: str) -> bool:
        changed = await self.update(
            "dashboard_sessions",
            {"revoked": 1, "revoked_at": utc_now_iso()},
            {"token_hash": token_hash, "revoked": 0},
        )
        return changed > 0

    async def revoke_user_sessions(self, user_id: int) -> int:
        """Revokes every session for a user (logout everywhere)."""
        return await self.update(
            "dashboard_sessions",
            {"revoked": 1, "revoked_at": utc_now_iso()},
            {"user_id": user_id, "revoked": 0},
        )

    async def purge_expired_sessions(self) -> int:
        """Deletes expired and revoked sessions. Returns rows removed."""
        query = (
            "DELETE FROM dashboard_sessions "
            "WHERE expires_at <= ? OR (revoked = 1 AND revoked_at <= ?)"
        )
        now = utc_now_iso()
        # Revoked sessions are kept briefly for audit purposes, then dropped.
        cutoff = iso_from_now(-86400)
        return await self.execute(query, (now, cutoff))

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def maintenance(self) -> None:
        """Reclaims free pages, truncates the WAL and refreshes statistics.

        ``incremental_vacuum`` returns freed pages to the filesystem without the
        long exclusive lock a full ``VACUUM`` needs, which is why the database
        is configured with ``auto_vacuum = INCREMENTAL``.
        """
        if not self.is_connected:
            return

        removed = await self.purge_expired_sessions()
        if removed:
            log.info("Purged %d expired dashboard session(s).", removed)

        if self._in_memory:
            return

        try:
            async with self._write_lock:
                async with self.acquire() as conn:
                    await conn.commit()
                    await conn.execute("PRAGMA incremental_vacuum;")
                    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    await conn.execute("PRAGMA optimize;")
                    await conn.commit()
        except sqlite3.Error as exc:
            log.warning("Database maintenance pass failed: %s", exc)
        else:
            log.debug("Database maintenance pass complete.")

    def start_maintenance(
        self, interval: float = DEFAULT_MAINTENANCE_INTERVAL_SECONDS
    ) -> None:
        """Starts the periodic maintenance task. Idempotent."""
        if self._maintenance_task is not None and not self._maintenance_task.done():
            return
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(interval), name="fyrion-db-maintenance"
        )

    async def stop_maintenance(self) -> None:
        """Cancels the periodic maintenance task, if it is running."""
        task = self._maintenance_task
        self._maintenance_task = None
        if task is None or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _maintenance_loop(self, interval: float) -> None:
        delay = max(60.0, float(interval))
        while not self._closed:
            try:
                await asyncio.sleep(delay)
                await self.maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed pass must never kill the loop.
                log.exception("Unexpected error during database maintenance.")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Returns pool and database counters, for /botinfo and health checks."""
        info: dict[str, Any] = {
            "db_url": self.db_url,
            "pool_size": self.pool_size,
            "available_connections": self._pool.qsize(),
            "in_memory": self._in_memory,
            "schema_version": SCHEMA_VERSION,
        }

        if not self.is_connected:
            info["connected"] = False
            return info

        info["connected"] = True
        async with self.acquire() as conn:
            for pragma, key in (
                ("journal_mode", "journal_mode"),
                ("auto_vacuum", "auto_vacuum"),
                ("page_count", "page_count"),
                ("page_size", "page_size"),
                ("freelist_count", "freelist_count"),
            ):
                async with conn.execute(f"PRAGMA {pragma};") as cursor:
                    row = await cursor.fetchone()
                info[key] = row[0] if row is not None else None

        page_count = info.get("page_count")
        page_size = info.get("page_size")
        if isinstance(page_count, int) and isinstance(page_size, int):
            info["size_bytes"] = page_count * page_size

        return info


# Compatibility alias so existing call sites that expect ``DatabaseManager``
# can switch to the pooled implementation by changing only the import path.
DatabaseManager = DatabasePool

__all__ = [
    "DatabasePool",
    "DatabaseManager",
    "DatabaseError",
    "PoolNotConnectedError",
    "InsufficientFundsError",
    "utc_now_iso",
    "iso_from_now",
    "DEFAULT_POOL_SIZE",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_MAINTENANCE_INTERVAL_SECONDS",
    "MAX_FETCH_LIMIT",
]
