"""
AutoMod configuration repository.

These settings are read for every guild message, so a database round trip per
message would be wasteful. Values are therefore cached in a process-wide
dictionary keyed by guild id. Writes must go through this repository, which
invalidates the cache, so operators see configuration changes immediately.

Column names are validated against an explicit allow-list before being used in
an UPDATE statement; values are always parameterized.
"""
from __future__ import annotations

from typing import Any, ClassVar


class AutoModConfigRepository:
    """Reads and writes rows in ``automod_configs``."""

    # Mirrors the column defaults declared in the schema. Used both as the
    # allow-list for writes and as a fallback if a row cannot be created.
    DEFAULTS: ClassVar[dict[str, Any]] = {
        "enabled": 1,
        "anti_spam_enabled": 0,
        "spam_message_limit": 5,
        "spam_interval_seconds": 5,
        "spam_strike_limit": 3,
        "spam_timeout_seconds": 300,
        "anti_invite_enabled": 0,
        "link_filter_enabled": 0,
        "caps_filter_enabled": 0,
        "caps_threshold_percent": 70,
        "caps_min_length": 10,
        "log_channel_id": None,
    }

    ALLOWED_COLUMNS: ClassVar[frozenset[str]] = frozenset(DEFAULTS)

    # Shared across instances on purpose: every instance talks to the same
    # database, and each cog builds its own repository object.
    _cache: ClassVar[dict[int, dict[str, Any]]] = {}

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Cache control
    # ------------------------------------------------------------------

    @classmethod
    def invalidate(cls, guild_id: int) -> None:
        """Drops the cached configuration for a single guild."""
        cls._cache.pop(guild_id, None)

    @classmethod
    def invalidate_all(cls) -> None:
        """Drops every cached configuration. Mainly useful in tests."""
        cls._cache.clear()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        """Returns the AutoMod configuration, creating defaults when missing."""
        cached = self._cache.get(guild_id)
        if cached is not None:
            return dict(cached)

        query = "SELECT * FROM automod_configs WHERE guild_id = ?"
        row = await self.db.fetchrow(query, (guild_id,))

        if row is None:
            await self._ensure_rows(guild_id)
            row = await self.db.fetchrow(query, (guild_id,))

        config: dict[str, Any] = dict(self.DEFAULTS)
        config["guild_id"] = guild_id
        if row is not None:
            config.update(dict(row))

        self._cache[guild_id] = config
        # Hand out a copy so callers cannot mutate the cached mapping.
        return dict(config)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def update_config(self, guild_id: int, key: str, value: Any) -> None:
        """Updates a single AutoMod setting."""
        await self.update_many(guild_id, {key: value})

    async def update_many(self, guild_id: int, values: dict[str, Any]) -> None:
        """Updates several AutoMod settings in one statement."""
        if not values:
            return

        invalid = set(values) - self.ALLOWED_COLUMNS
        if invalid:
            raise ValueError(
                f"Invalid AutoMod configuration key(s): {sorted(invalid)}"
            )

        await self._ensure_rows(guild_id)

        # Column identifiers come from the allow-list above; only values are
        # interpolated, and those stay parameterized.
        assignments = ", ".join(f"{column} = ?" for column in values)
        query = f"UPDATE automod_configs SET {assignments} WHERE guild_id = ?"
        await self.db.execute(query, (*values.values(), guild_id))

        self.invalidate(guild_id)

    async def _ensure_rows(self, guild_id: int) -> None:
        """Creates the parent guild row and the AutoMod row if either is absent.

        ``automod_configs`` has a foreign key onto ``guild_configs``, and
        foreign keys are enforced, so the parent row must exist first.
        """
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)",
            (guild_id,),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO automod_configs (guild_id) VALUES (?)",
            (guild_id,),
        )


__all__ = ["AutoModConfigRepository"]
