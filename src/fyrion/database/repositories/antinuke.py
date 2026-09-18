"""
AntiNuke configuration repository.

Storage layout
--------------
Per-guild configuration lives in ``antinuke_settings`` (one row per guild) and
the trusted-actor list in ``antinuke_whitelist`` (one row per actor). Both
tables are declared by :mod:`fyrion.database.schema` and validated against the
generic-CRUD allow-list before any statement runs.

A settings row carries the master ``enabled`` flag, the detection
``window_seconds``, the ``punishment`` to apply to an offending actor, and one
nullable threshold column per watched action. A ``NULL`` threshold means that
action is not watched, so an operator can arm mass-ban detection without also
arming, say, channel-creation detection.

Caching
-------
The listener cannot afford a database round trip per audit-log event, so the cog
caches a compiled policy. Every write bumps a per-guild generation counter; the
cog compares generations and rebuilds when they differ. The counter is class
level because each cog builds its own repository instance while all of them talk
to the same database. This mirrors
:class:`~fyrion.database.repositories.automod_rules.AutoModRuleRepository`.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Final, Mapping

from fyrion.database.schema import ANTINUKE_ACTIONS, ANTINUKE_PUNISHMENTS

log = logging.getLogger("fyrion.database.repositories.antinuke")

# The watched actions, in a stable order for status embeds. Keys match the
# ``<action>_threshold`` columns on antinuke_settings.
ANTINUKE_ACTION_KEYS: Final[tuple[str, ...]] = tuple(ANTINUKE_ACTIONS)

# Columns a command is allowed to write. Anything else is a programming error.
_THRESHOLD_COLUMNS: Final[frozenset[str]] = frozenset(
    f"{action}_threshold" for action in ANTINUKE_ACTION_KEYS
)
WRITABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"enabled", "window_seconds", "punishment"} | _THRESHOLD_COLUMNS
)

# Bounds enforced here as well as by the slash-command Range annotations, so a
# dashboard or a future caller cannot store an unusable value.
MIN_WINDOW_SECONDS: Final[int] = 5
MAX_WINDOW_SECONDS: Final[int] = 600
MIN_THRESHOLD: Final[int] = 2
MAX_THRESHOLD: Final[int] = 100

# Seeded on first creation so a freshly enabled guild has sensible detection
# without the operator having to set every threshold by hand.
DEFAULTS: Final[dict[str, Any]] = {
    "enabled": 1,
    "window_seconds": 30,
    "punishment": "strip_roles",
    "ban_threshold": 3,
    "kick_threshold": 5,
    "channel_delete_threshold": 3,
    "channel_create_threshold": 5,
    "role_delete_threshold": 3,
    "role_create_threshold": 5,
    "webhook_create_threshold": 5,
}


def threshold_column(action: str) -> str:
    """Returns the settings column that holds ``action``'s threshold."""
    if action not in ANTINUKE_ACTIONS:
        raise ValueError(f"Unsupported AntiNuke action: {action!r}")
    return f"{action}_threshold"


class AntiNukeRepository:
    """Reads and writes AntiNuke settings and the trusted-actor whitelist."""

    # guild_id -> monotonically increasing revision of that guild's policy.
    _generation: ClassVar[dict[int, int]] = {}

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Cache coordination
    # ------------------------------------------------------------------

    @classmethod
    def generation(cls, guild_id: int) -> int:
        """Returns the current policy revision for a guild."""
        return cls._generation.get(int(guild_id), 0)

    @classmethod
    def bump(cls, guild_id: int) -> int:
        """Marks a guild's cached policy as stale."""
        key = int(guild_id)
        revision = cls._generation.get(key, 0) + 1
        cls._generation[key] = revision
        return revision

    @classmethod
    def reset_generations(cls) -> None:
        """Clears every revision counter. Used by the test suite."""
        cls._generation.clear()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_settings(self, guild_id: int) -> dict[str, Any] | None:
        """Returns the guild's settings row, or None when unconfigured."""
        return await self.db.fetch_one(
            "antinuke_settings", {"guild_id": int(guild_id)}
        )

    async def save_settings(
        self, guild_id: int, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Creates or updates the guild's settings.

        ``None`` values are ignored so a command can pass every optional
        parameter through without clobbering settings the operator did not
        mention. Missing fields are seeded from :data:`DEFAULTS` the first time
        a row is created.
        """
        unknown = set(values) - WRITABLE_FIELDS
        if unknown:
            raise ValueError(f"Unwritable AntiNuke field(s): {sorted(unknown)}")

        existing = await self.get_settings(guild_id)

        payload: dict[str, Any] = {"guild_id": int(guild_id)}
        if existing is None:
            payload.update(DEFAULTS)

        for key, value in values.items():
            if value is None:
                continue
            if key == "punishment":
                if value not in ANTINUKE_PUNISHMENTS:
                    raise ValueError(f"Unsupported AntiNuke punishment: {value!r}")
                payload[key] = value
            elif key == "enabled":
                payload[key] = int(bool(value))
            elif key == "window_seconds":
                payload[key] = self._bounded(
                    int(value), MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS, key
                )
            else:  # a *_threshold column
                payload[key] = self._bounded(
                    int(value), MIN_THRESHOLD, MAX_THRESHOLD, key
                )

        await self.db.upsert(
            "antinuke_settings", payload, conflict_columns=("guild_id",)
        )
        self.bump(guild_id)

        saved = await self.get_settings(guild_id)
        if saved is None:  # pragma: no cover - the upsert just succeeded
            raise RuntimeError("AntiNuke settings disappeared after being saved.")
        return saved

    async def set_enabled(self, guild_id: int, enabled: bool) -> dict[str, Any]:
        return await self.save_settings(guild_id, {"enabled": enabled})

    async def set_threshold(
        self, guild_id: int, action: str, threshold: int
    ) -> dict[str, Any]:
        return await self.save_settings(
            guild_id, {threshold_column(action): threshold}
        )

    async def clear_threshold(self, guild_id: int, action: str) -> dict[str, Any]:
        """Stops watching one action by nulling its threshold column."""
        column = threshold_column(action)
        existing = await self.get_settings(guild_id)
        if existing is None:
            # Nothing to clear; return a seeded row so callers get a shape.
            return await self.save_settings(guild_id, {})
        await self.db.update(
            "antinuke_settings", {column: None}, {"guild_id": int(guild_id)}
        )
        self.bump(guild_id)
        saved = await self.get_settings(guild_id)
        assert saved is not None
        return saved

    # ------------------------------------------------------------------
    # Whitelist (trusted actors)
    # ------------------------------------------------------------------

    async def add_whitelist(
        self, guild_id: int, actor_id: int, *, added_by: int | None = None
    ) -> bool:
        """Trusts an actor. Returns False when it was already trusted."""
        await self.db.ensure_guild(int(guild_id))
        rowid = await self.db.insert(
            "antinuke_whitelist",
            {
                "guild_id": int(guild_id),
                "actor_id": int(actor_id),
                "added_by": int(added_by) if added_by is not None else None,
            },
            on_conflict="ignore",
        )
        added = rowid is not None
        if added:
            self.bump(guild_id)
        return added

    async def remove_whitelist(self, guild_id: int, actor_id: int) -> bool:
        """Removes an actor's trust. Returns False when there was none."""
        removed = await self.db.delete(
            "antinuke_whitelist",
            {"guild_id": int(guild_id), "actor_id": int(actor_id)},
        )
        if removed:
            self.bump(guild_id)
        return bool(removed)

    async def get_whitelist(self, guild_id: int) -> frozenset[int]:
        """Returns the set of trusted actor ids for a guild."""
        rows = await self.db.fetch_many(
            "antinuke_whitelist",
            {"guild_id": int(guild_id)},
            columns=("actor_id",),
        )
        result: set[int] = set()
        for row in rows:
            try:
                result.add(int(row["actor_id"]))
            except (TypeError, ValueError, KeyError):
                continue
        return frozenset(result)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _bounded(value: int, low: int, high: int, field: str) -> int:
        if not low <= value <= high:
            raise ValueError(
                f"{field} must be between {low} and {high}, got {value}."
            )
        return value


__all__ = [
    "AntiNukeRepository",
    "ANTINUKE_ACTION_KEYS",
    "DEFAULTS",
    "MAX_THRESHOLD",
    "MAX_WINDOW_SECONDS",
    "MIN_THRESHOLD",
    "MIN_WINDOW_SECONDS",
    "WRITABLE_FIELDS",
    "threshold_column",
]
