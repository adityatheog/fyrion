"""
Warnings repository.

All queries are scoped by ``guild_id`` so warning history never leaks between
servers. Row counts are obtained with an explicit ``COUNT(*)`` rather than
``cursor.rowcount`` so the repository works with either the single-connection
manager or the pooled implementation.
"""
from __future__ import annotations

from typing import Any

import aiosqlite


class WarningsRepository:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def add_warning(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str,
    ) -> None:
        """Records a new warning for a user in a specific guild."""
        # The FK onto guild_configs is enforced, so make sure the parent exists.
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)",
            (guild_id,),
        )

        query = """
            INSERT INTO warnings (guild_id, user_id, moderator_id, reason)
            VALUES (?, ?, ?, ?)
        """
        await self.db.execute(query, (guild_id, user_id, moderator_id, reason))

    async def get_warnings(self, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        """Retrieves all warnings for a user in a specific guild, newest first."""
        query = """
            SELECT * FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
        """
        return await self.db.fetchall(query, (guild_id, user_id))

    async def count_warnings(self, guild_id: int, user_id: int) -> int:
        """Counts the warnings a user has in a specific guild."""
        query = "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?"
        row = await self.db.fetchrow(query, (guild_id, user_id))
        return int(row[0]) if row is not None else 0

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        """Clears every warning for a user in a guild. Returns rows removed."""
        removed = await self.count_warnings(guild_id, user_id)
        if removed:
            query = "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?"
            await self.db.execute(query, (guild_id, user_id))
        return removed

    async def delete_warning(self, guild_id: int, warning_id: int) -> bool:
        """Deletes a single warning by id, scoped to the guild. Returns success."""
        lookup = "SELECT id FROM warnings WHERE guild_id = ? AND id = ?"
        row = await self.db.fetchrow(lookup, (guild_id, warning_id))
        if row is None:
            return False

        query = "DELETE FROM warnings WHERE guild_id = ? AND id = ?"
        await self.db.execute(query, (guild_id, warning_id))
        return True


__all__ = ["WarningsRepository"]
