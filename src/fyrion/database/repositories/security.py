"""
Security and whitelist repository.
"""
import aiosqlite
from fyrion.database.connection import DatabaseManager

class SecurityRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add_whitelist(self, guild_id: int, entity_id: int, entity_type: str) -> None:
        """Adds an entity to the anti-link whitelist. Ignores duplicates."""
        query = """
            INSERT OR IGNORE INTO whitelists (guild_id, entity_id, entity_type) 
            VALUES (?, ?, ?)
        """
        await self.db.execute(query, (guild_id, entity_id, entity_type))

    async def remove_whitelist(self, guild_id: int, entity_id: int, entity_type: str) -> None:
        """Removes an entity from the anti-link whitelist."""
        query = "DELETE FROM whitelists WHERE guild_id = ? AND entity_id = ? AND entity_type = ?"
        await self.db.execute(query, (guild_id, entity_id, entity_type))

    async def get_whitelists(self, guild_id: int) -> list[aiosqlite.Row]:
        """Retrieves all whitelisted entities for a specific guild."""
        query = "SELECT entity_id, entity_type FROM whitelists WHERE guild_id = ?"
        return await self.db.fetchall(query, (guild_id,))
