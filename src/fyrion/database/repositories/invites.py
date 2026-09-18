"""
Invite tracking repository.
"""
from typing import Any

import aiosqlite

class InviteRepository:
    def __init__(self, db: Any):
        self.db = db

    async def add_join(self, guild_id: int, joined_user_id: int, inviter_id: int) -> None:
        """Records a user join and credits the inviter."""
        # ``member_inviters`` and ``invite_stats`` both hold a foreign key onto
        # ``guild_configs``. On a brand-new guild that row may not exist yet, so
        # create it defensively before the child inserts.
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)",
            (guild_id,),
        )

        # 1. Record who invited this user
        query_link = """
            INSERT OR REPLACE INTO member_inviters (guild_id, user_id, inviter_id)
            VALUES (?, ?, ?)
        """
        await self.db.execute(query_link, (guild_id, joined_user_id, inviter_id))

        # 2. Increment the inviter's join count
        query_stats = """
            INSERT INTO invite_stats (guild_id, user_id, joins, leaves)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET joins = joins + 1
        """
        await self.db.execute(query_stats, (guild_id, inviter_id))

    async def add_leave(self, guild_id: int, leaving_user_id: int) -> None:
        """Processes a user leave and penalizes the original inviter."""
        # 1. Find who invited them
        query_find = "SELECT inviter_id FROM member_inviters WHERE guild_id = ? AND user_id = ?"
        row = await self.db.fetchrow(query_find, (guild_id, leaving_user_id))
        
        if row:
            inviter_id = row["inviter_id"]
            # 2. Increment the inviter's leave count
            query_stats = """
                UPDATE invite_stats SET leaves = leaves + 1 
                WHERE guild_id = ? AND user_id = ?
            """
            await self.db.execute(query_stats, (guild_id, inviter_id))
            
            # 3. Clean up the link to save space
            query_clean = "DELETE FROM member_inviters WHERE guild_id = ? AND user_id = ?"
            await self.db.execute(query_clean, (guild_id, leaving_user_id))

    async def get_stats(self, guild_id: int, user_id: int) -> dict[str, int]:
        """Retrieves a user's invite statistics."""
        query = "SELECT joins, leaves FROM invite_stats WHERE guild_id = ? AND user_id = ?"
        row = await self.db.fetchrow(query, (guild_id, user_id))
        
        if row:
            joins, leaves = row["joins"], row["leaves"]
            return {"joins": joins, "leaves": leaves, "net": joins - leaves}
        return {"joins": 0, "leaves": 0, "net": 0}
