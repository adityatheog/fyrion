"""
Tickets repository.
"""
from typing import Any
import aiosqlite
from fyrion.database.connection import DatabaseManager

class TicketRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        """Retrieves ticket configuration for a guild."""
        query = "SELECT * FROM ticket_configs WHERE guild_id = ?"
        row = await self.db.fetchrow(query, (guild_id,))
        return dict(row) if row else {}

    async def set_config(self, guild_id: int, category_id: int, log_channel_id: int | None) -> None:
        """Sets or updates the ticket configuration."""
        query = """
            INSERT INTO ticket_configs (guild_id, category_id, log_channel_id) 
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET 
            category_id = excluded.category_id,
            log_channel_id = excluded.log_channel_id
        """
        await self.db.execute(query, (guild_id, category_id, log_channel_id))

    async def create_ticket(self, guild_id: int, channel_id: int, user_id: int) -> None:
        """Registers a new open ticket."""
        query = "INSERT INTO tickets (guild_id, channel_id, user_id, status) VALUES (?, ?, ?, 'open')"
        await self.db.execute(query, (guild_id, channel_id, user_id))

    async def get_open_ticket_for_user(self, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        """Checks if a user already has an open ticket."""
        query = "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'"
        return await self.db.fetchrow(query, (guild_id, user_id))

    async def close_ticket(self, channel_id: int) -> None:
        """Marks a ticket as closed."""
        query = "UPDATE tickets SET status = 'closed' WHERE channel_id = ?"
        await self.db.execute(query, (channel_id,))
