"""
Warnings repository.

All queries are scoped by ``guild_id`` so warning history never leaks between
servers. Where a method reports how many warnings *exist* it counts them with an
explicit ``COUNT(*)``; where it reports how many it just *removed* it uses the
``DELETE``'s own row count. The two are only equal if nothing else touches the
rows in between, so the distinction is kept rather than assumed.
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
        """Clears every warning for a user in a guild. Returns rows removed.

        The count is the ``DELETE``'s own row count. Counting first and deleting
        second reported a number that a concurrent delete or warning could
        invalidate before the statement ran, so the value could differ from what
        was actually removed -- which is what the docstring promised.
        """
        query = "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?"
        return await self.db.execute(query, (guild_id, user_id))

    async def delete_warning(self, guild_id: int, warning_id: int) -> bool:
        """Deletes a single warning by id, scoped to the guild. Returns success.

        One statement rather than a lookup followed by a delete: the row can
        disappear between the two, and the delete's own row count already
        answers exactly the question the lookup was asked.
        """
        query = "DELETE FROM warnings WHERE guild_id = ? AND id = ?"
        return await self.db.execute(query, (guild_id, warning_id)) > 0


__all__ = ["WarningsRepository"]
