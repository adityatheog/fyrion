"""
Guild configuration repository.
"""
from typing import Any
import aiosqlite
from fyrion.database.connection import DatabaseManager

class GuildConfigRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        """
        Retrieves the configuration for a guild. 
        Creates a default record safely if one does not exist.
        """
        query = "SELECT * FROM guild_configs WHERE guild_id = ?"
        row = await self.db.fetchrow(query, (guild_id,))
        
        if not row:
            insert_query = "INSERT INTO guild_configs (guild_id) VALUES (?)"
            await self.db.execute(insert_query, (guild_id,))
            
            # Fetch again to return the full default representation
            row = await self.db.fetchrow(query, (guild_id,))
            
        # Returning a standard dict ensures UI logic isn't tied to SQLite Row objects
        return dict(row) if row else {}

    async def update_config(self, guild_id: int, key: str, value: Any) -> None:
        """
        Updates a specific configuration key for a guild.
        Whitelists the column names to prevent SQL injection in the column identifier.
        """
        allowed_columns = {"welcome_channel_id", "log_channel_id", "autorole_id", "anti_link_enabled"}
        
        if key not in allowed_columns:
            raise ValueError(f"Invalid configuration key: {key}")
            
        # Ensure guild config exists before updating
        await self.get_config(guild_id)
        
        # Safe string formatting for the column, parameterized value
        query = f"UPDATE guild_configs SET {key} = ? WHERE guild_id = ?"
        await self.db.execute(query, (value, guild_id))
