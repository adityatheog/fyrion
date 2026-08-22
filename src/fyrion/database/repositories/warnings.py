"""
Warnings repository.
"""
import aiosqlite
from fyrion.database.connection import DatabaseManager

class WarningsRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> None:
        """Records a new warning for a user in a specific guild."""
        query = """
            INSERT INTO warnings (guild_id, user_id, moderator_id, reason)
            VALUES (?, ?, ?, ?)
        """
        await self.db.execute(query, (guild_id, user_id, moderator_id, reason))

    async def get_warnings(self, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        """Retrieves all warnings for a specific user in a specific guild."""
        query = "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC"
        return await self.db.fetchall(query, (guild_id, user_id))

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        """Clears all warnings for a user in a specific guild. Returns rows affected."""
        if not self.db._conn:
            raise RuntimeError("Database not connected.")
            
        query = "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?"
        async with self.db._conn.cursor() as cursor:
            await cursor.execute(query, (guild_id, user_id))
            affected = cursor.rowcount
            await self.db._conn.commit()
            return affected
