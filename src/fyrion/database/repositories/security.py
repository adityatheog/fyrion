"""
Security and whitelist repository.
"""

from typing import Any

import aiosqlite


class SecurityRepository:
    def __init__(self, db: Any):
        self.db = db

    async def add_whitelist(
        self, guild_id: int, entity_id: int, entity_type: str
    ) -> None:
        """Adds an entity to the anti-link whitelist. Ignores duplicates."""
        # ``whitelists`` has a foreign key onto ``guild_configs``, so make sure
        # the parent row exists before inserting. A brand-new guild may never
        # have had one created, and an INSERT OR IGNORE would otherwise silently
        # drop the whitelist entry when the constraint fails.
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)",
            (guild_id,),
        )
        query = """
            INSERT OR IGNORE INTO whitelists (guild_id, entity_id, entity_type)
            VALUES (?, ?, ?)
        """
        await self.db.execute(query, (guild_id, entity_id, entity_type))

    async def remove_whitelist(
        self, guild_id: int, entity_id: int, entity_type: str
    ) -> None:
        """Removes an entity from the anti-link whitelist."""
        query = "DELETE FROM whitelists WHERE guild_id = ? AND entity_id = ? AND entity_type = ?"
        await self.db.execute(query, (guild_id, entity_id, entity_type))

    async def get_whitelists(self, guild_id: int) -> list[aiosqlite.Row]:
        """Retrieves all whitelisted entities for a specific guild."""
        query = "SELECT entity_id, entity_type FROM whitelists WHERE guild_id = ?"
        return await self.db.fetchall(query, (guild_id,))
