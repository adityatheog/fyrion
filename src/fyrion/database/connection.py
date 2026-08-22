"""
Asynchronous database connection management.
"""
import aiosqlite
import logging
from typing import Any, Iterable

from fyrion.config import Config
from fyrion.database.schema import INITIAL_SCHEMA

log = logging.getLogger("fyrion.database")

class DatabaseManager:
    def __init__(self) -> None:
        self.db_url: str = Config.DATABASE_URL
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Initializes the connection and verifies the schema."""
        log.info(f"Connecting to SQLite database at {self.db_url}")
        self._conn = await aiosqlite.connect(self.db_url)
        
        # row_factory allows accessing columns by name (like a dictionary)
        self._conn.row_factory = aiosqlite.Row
        
        # Enforce foreign key constraints (SQLite disables them by default)
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        
        await self._init_schema()
        log.info("Database connected and schema verified.")

    async def _init_schema(self) -> None:
        """Executes the DDL to ensure tables exist."""
        if not self._conn:
            raise RuntimeError("Database connection is not established.")
        
        # execute_script handles multiple semicolon-separated statements
        await self._conn.executescript(INITIAL_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        """Gracefully closes the connection."""
        if self._conn:
            await self._conn.close()
            log.info("Database connection closed.")

    async def execute(self, query: str, parameters: Iterable[Any] = ()) -> None:
        """Executes an INSERT/UPDATE/DELETE query and commits."""
        if not self._conn:
            raise RuntimeError("Database not connected.")
        
        async with self._conn.cursor() as cursor:
            await cursor.execute(query, parameters)
            await self._conn.commit()

    async def fetchrow(self, query: str, parameters: Iterable[Any] = ()) -> aiosqlite.Row | None:
        """Fetches a single row."""
        if not self._conn:
            raise RuntimeError("Database not connected.")
            
        async with self._conn.cursor() as cursor:
            await cursor.execute(query, parameters)
            return await cursor.fetchone()

    async def fetchall(self, query: str, parameters: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        """Fetches all matching rows."""
        if not self._conn:
            raise RuntimeError("Database not connected.")
            
        async with self._conn.cursor() as cursor:
            await cursor.execute(query, parameters)
            return await cursor.fetchall()
