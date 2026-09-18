"""
Guild configuration repository.

Backed by the core ``guild_settings`` table. Historically this repository read
and wrote the legacy ``guild_configs`` table; it now delegates to the pooled
``guild_settings`` helpers so there is a single source of truth for guild
configuration.

The public surface is kept stable so existing callers need no changes:

* :meth:`get_config` returns a dict that still carries the legacy key names.
  ``log_channel_id`` maps onto the core ``mod_log_channel_id`` column, while
  ``welcome_channel_id`` and ``autorole_id`` are direct.
* :meth:`update_config` accepts those same legacy keys and validates them, so
  an unknown or injected key still raises ``ValueError`` before any SQL runs.
"""
from typing import Any

# Legacy configuration key -> ``guild_settings`` column.
_KEY_TO_COLUMN: dict[str, str] = {
    "welcome_channel_id": "welcome_channel_id",
    "log_channel_id": "mod_log_channel_id",
    "autorole_id": "autorole_id",
}


class GuildConfigRepository:
    def __init__(self, db: Any):
        self.db = db

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        """
        Retrieves the configuration for a guild, creating defaults on first
        access. The returned dict uses the legacy key names so callers that
        predate the schema consolidation keep working.
        """
        settings = await self.db.get_guild_settings(int(guild_id))
        return {
            "guild_id": settings.get("guild_id", int(guild_id)),
            "welcome_channel_id": settings.get("welcome_channel_id"),
            "log_channel_id": settings.get("mod_log_channel_id"),
            "autorole_id": settings.get("autorole_id"),
        }

    async def update_config(self, guild_id: int, key: str, value: Any) -> None:
        """
        Updates a single configuration key for a guild.

        The key is validated against a fixed allow-list and translated to its
        ``guild_settings`` column, so an injected identifier can never reach a
        SQL statement.
        """
        column = _KEY_TO_COLUMN.get(key)
        if column is None:
            raise ValueError(f"Invalid configuration key: {key}")

        await self.db.update_guild_settings(int(guild_id), **{column: value})
