"""
Leveling and reputation repository.

Experience, levels and level rewards live in the core schema
(``leveling_profiles``, ``level_rewards``, plus the ``leveling_*`` columns of
``guild_settings``), so those reads and writes go through the pooled data layer,
which validates every identifier against the schema allow-list and binds every
value as a SQL parameter.

Reputation has no core table, so this module owns one. The DDL is idempotent and
is applied on cog load, exactly like the rest of the schema, and the table
cascades from ``guild_settings`` so removing a guild leaves no orphaned rows.
One row per ``(guild_id, user_id)`` carries both sides of the feature: ``points``
is what the member has received, ``given`` and ``last_given_at`` are what they
have handed out and when. Keeping both on one row means a ``/rep`` invocation
touches two rows rather than four.

The level curve is the classic quadratic step: level *n* costs
``5n² + 50n + 100`` XP, and cumulative thresholds are memoised in a module-level
list so ``level_from_xp`` stays cheap on the message hot path.

Concurrency notes:

* XP awards are an upsert with ``xp = xp + excluded.xp`` inside the pool, so two
  messages processed concurrently cannot lose an award.
* The ``/rep`` cooldown is enforced by a conditional ``UPDATE`` rather than a
  read-then-write, so two simultaneous invocations cannot both pass the check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Final

from fyrion.database.manager import iso_from_now, utc_now_iso

log = logging.getLogger("fyrion.database.repositories.leveling")

# Hard ceiling on the curve. Bounds every loop in this module and keeps a
# corrupt XP value from turning into an unbounded computation.
MAX_LEVEL: Final[int] = 500

# Cumulative XP required to *reach* each level. Index 0 is always 0.
_THRESHOLDS: list[int] = [0]

# Idempotent DDL for the reputation table. Executed statement by statement so
# the repository only needs a database object exposing ``execute``.
REPUTATION_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS reputation (
        guild_id         INTEGER NOT NULL,
        user_id          INTEGER NOT NULL,
        points           INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
        given            INTEGER NOT NULL DEFAULT 0 CHECK (given >= 0),
        last_given_at    TEXT,
        last_received_at TEXT,
        updated_at       TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reputation_top
        ON reputation (guild_id, points DESC)
    """,
)


# ---------------------------------------------------------------------------
# Curve
# ---------------------------------------------------------------------------


def xp_to_next_level(level: int) -> int:
    """Returns the XP needed to advance from ``level`` to ``level + 1``."""
    level = max(0, int(level))
    return 5 * level * level + 50 * level + 100


def _extend_thresholds(up_to: int) -> None:
    while len(_THRESHOLDS) <= up_to:
        current = len(_THRESHOLDS) - 1
        _THRESHOLDS.append(_THRESHOLDS[-1] + xp_to_next_level(current))


def total_xp_for_level(level: int) -> int:
    """Returns the cumulative XP required to reach ``level``."""
    level = max(0, min(int(level), MAX_LEVEL))
    _extend_thresholds(level)
    return _THRESHOLDS[level]


def level_from_xp(xp: int) -> int:
    """Returns the level a member with ``xp`` total experience has reached."""
    xp = max(0, int(xp))
    level = 0
    while level < MAX_LEVEL and xp >= total_xp_for_level(level + 1):
        level += 1
    return level


def level_progress(xp: int) -> tuple[int, int, int]:
    """Returns ``(level, xp_into_level, xp_required_for_next)``.

    At :data:`MAX_LEVEL` the last two values are zero, which callers render as a
    completed bar rather than dividing by zero.
    """
    xp = max(0, int(xp))
    level = level_from_xp(xp)
    base = total_xp_for_level(level)
    if level >= MAX_LEVEL:
        return level, 0, 0
    required = total_xp_for_level(level + 1) - base
    return level, xp - base, required


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LevelingSettings:
    """A guild's leveling configuration, pre-coerced for the hot path."""

    enabled: bool = False
    xp_per_message: int = 15
    cooldown_seconds: int = 60
    announce_channel_id: int | None = None
    stack_rewards: bool = True


@dataclass(frozen=True)
class RepOutcome:
    """Result of a ``/rep`` attempt."""

    granted: bool
    retry_after: float
    total: int


def _parse_iso(value: Any) -> datetime | None:
    """Parses the ISO-8601 UTC format the schema stores, tolerating junk."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class LevelingRepository:
    """Reads and writes leveling profiles, level rewards and reputation."""

    # Applying the reputation DDL once per process is enough: it is idempotent,
    # and every pooled connection sees the same database file.
    _schema_ready: ClassVar[bool] = False

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def ensure_schema(self, *, force: bool = False) -> None:
        """Creates the reputation table if it does not exist yet."""
        if LevelingRepository._schema_ready and not force:
            return
        for statement in REPUTATION_STATEMENTS:
            await self.db.execute(statement)
        LevelingRepository._schema_ready = True

    @classmethod
    def reset_schema_flag(cls) -> None:
        """Forces the next :meth:`ensure_schema` call to run. Used by tests."""
        cls._schema_ready = False

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_settings(self, guild_id: int) -> LevelingSettings:
        settings = await self.db.get_guild_settings(int(guild_id))

        announce = settings.get("leveling_announce_channel_id")
        return LevelingSettings(
            enabled=bool(settings.get("leveling_enabled")),
            xp_per_message=max(0, _as_int(settings.get("leveling_xp_per_message"), 15)),
            cooldown_seconds=max(
                0, _as_int(settings.get("leveling_cooldown_seconds"), 60)
            ),
            announce_channel_id=int(announce) if announce else None,
            stack_rewards=bool(settings.get("leveling_stack_rewards")),
        )

    async def update_settings(self, guild_id: int, **values: Any) -> None:
        """Updates one or more ``leveling_*`` columns on ``guild_settings``."""
        if not values:
            return
        await self.db.update_guild_settings(int(guild_id), **values)

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    async def get_profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        return await self.db.get_leveling_profile(int(guild_id), int(user_id))

    async def award_xp(
        self, guild_id: int, user_id: int, amount: int
    ) -> dict[str, Any]:
        """Adds XP atomically and returns the refreshed profile."""
        return await self.db.add_xp(int(guild_id), int(user_id), max(0, int(amount)))

    async def set_level(self, guild_id: int, user_id: int, level: int) -> None:
        await self.db.set_level(int(guild_id), int(user_id), max(0, int(level)))

    async def leaderboard(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        return await self.db.leveling_leaderboard(int(guild_id), max(1, int(limit)))

    async def tracked_members(self, guild_id: int) -> int:
        return await self.db.count("leveling_profiles", {"guild_id": int(guild_id)})

    async def rank_of(self, guild_id: int, user_id: int, xp: int) -> int:
        """Returns a member's 1-based rank inside their guild.

        The tie-break matches the leaderboard ordering (``xp DESC, user_id
        ASC``), so the rank shown by ``/rank`` always agrees with the position
        shown by ``/leaderboard-levels``.
        """
        query = (
            "SELECT COUNT(*) + 1 FROM leveling_profiles "
            "WHERE guild_id = ? AND (xp > ? OR (xp = ? AND user_id < ?))"
        )
        value = await self.db.fetchval(
            query, (int(guild_id), int(xp), int(xp), int(user_id)), default=1
        )
        return max(1, _as_int(value, 1))

    async def reset_profile(self, guild_id: int, user_id: int) -> bool:
        changed = await self.db.update(
            "leveling_profiles",
            {"xp": 0, "level": 0},
            {"guild_id": int(guild_id), "user_id": int(user_id)},
        )
        return bool(changed)

    # ------------------------------------------------------------------
    # Level rewards
    # ------------------------------------------------------------------

    async def list_rewards(self, guild_id: int) -> list[dict[str, Any]]:
        return await self.db.fetch_many(
            "level_rewards", {"guild_id": int(guild_id)}, order_by="level ASC"
        )

    async def count_rewards(self, guild_id: int) -> int:
        return await self.db.count("level_rewards", {"guild_id": int(guild_id)})

    async def rewards_up_to(self, guild_id: int, level: int) -> list[dict[str, Any]]:
        """Returns every reward a member at ``level`` has earned, lowest first."""
        return await self.db.get_level_rewards(
            int(guild_id), up_to_level=max(0, int(level))
        )

    async def add_reward(
        self,
        guild_id: int,
        level: int,
        role_id: int,
        *,
        remove_previous: bool = False,
    ) -> None:
        """Creates or updates a level reward."""
        level = int(level)
        if not 1 <= level <= MAX_LEVEL:
            raise ValueError(f"level must be between 1 and {MAX_LEVEL}.")

        await self.db.upsert(
            "level_rewards",
            {
                "guild_id": int(guild_id),
                "level": level,
                "role_id": int(role_id),
                "remove_previous": int(bool(remove_previous)),
            },
            conflict_columns=("guild_id", "level", "role_id"),
            update_columns=("remove_previous",),
        )

    async def remove_reward(
        self, guild_id: int, level: int, role_id: int | None = None
    ) -> int:
        """Deletes reward rows. Returns how many were removed."""
        where: dict[str, Any] = {"guild_id": int(guild_id), "level": int(level)}
        if role_id is not None:
            where["role_id"] = int(role_id)
        return await self.db.delete("level_rewards", where)

    # ------------------------------------------------------------------
    # Reputation
    # ------------------------------------------------------------------

    async def get_rep_row(self, guild_id: int, user_id: int) -> dict[str, Any]:
        await self.ensure_schema()
        row = await self.db.fetchrow(
            "SELECT guild_id, user_id, points, given, last_given_at, "
            "last_received_at FROM reputation WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
        if row is None:
            return {
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "points": 0,
                "given": 0,
                "last_given_at": None,
                "last_received_at": None,
            }
        return dict(row)

    async def get_rep(self, guild_id: int, user_id: int) -> int:
        row = await self.get_rep_row(guild_id, user_id)
        return _as_int(row.get("points"), 0)

    async def rep_leaderboard(
        self, guild_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        await self.ensure_schema()
        rows = await self.db.fetchall(
            "SELECT user_id, points, given FROM reputation "
            "WHERE guild_id = ? AND points > 0 "
            "ORDER BY points DESC, user_id ASC LIMIT ?",
            (int(guild_id), max(1, int(limit))),
        )
        return [dict(row) for row in rows]

    async def give_rep(
        self,
        guild_id: int,
        giver_id: int,
        target_id: int,
        *,
        cooldown_seconds: int,
    ) -> RepOutcome:
        """Awards one reputation point, honouring the giver's cooldown.

        The cooldown is enforced by the ``WHERE`` clause of a single ``UPDATE``
        rather than by a read followed by a write, so two concurrent
        invocations from the same member cannot both be accepted.
        """
        guild_id = int(guild_id)
        giver_id = int(giver_id)
        target_id = int(target_id)
        cooldown = max(0, int(cooldown_seconds))

        await self.ensure_schema()
        await self.db.ensure_guild(guild_id)

        now = utc_now_iso()
        cutoff = iso_from_now(-cooldown)

        # Claim the giver's cooldown and credit the recipient in one transaction
        # so a failure between the two cannot burn the cooldown while losing the
        # point. Use the yielded connection for every statement — calling
        # self.db.execute() inside the write lock would deadlock.
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO reputation (guild_id, user_id) VALUES (?, ?)",
                (guild_id, giver_id),
            )
            async with conn.execute(
                "UPDATE reputation "
                "   SET given = given + 1, last_given_at = ?, updated_at = ? "
                " WHERE guild_id = ? AND user_id = ? "
                "   AND (last_given_at IS NULL OR last_given_at <= ?)",
                (now, now, guild_id, giver_id, cutoff),
            ) as cursor:
                changed = cursor.rowcount

            if changed:
                await conn.execute(
                    "INSERT INTO reputation "
                    "    (guild_id, user_id, points, last_received_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?) "
                    "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                    "    points = points + 1, "
                    "    last_received_at = excluded.last_received_at, "
                    "    updated_at = excluded.updated_at",
                    (guild_id, target_id, now, now),
                )

        # Reads for the response run outside the write lock.
        if not changed:
            row = await self.get_rep_row(guild_id, giver_id)
            retry = self._retry_after(row.get("last_given_at"), cooldown)
            return RepOutcome(
                granted=False,
                retry_after=retry,
                total=await self.get_rep(guild_id, target_id),
            )

        return RepOutcome(
            granted=True,
            retry_after=0.0,
            total=await self.get_rep(guild_id, target_id),
        )

    @staticmethod
    def _retry_after(last_given_at: Any, cooldown_seconds: int) -> float:
        moment = _parse_iso(last_given_at)
        if moment is None or cooldown_seconds <= 0:
            return 0.0
        elapsed = (datetime.now(timezone.utc) - moment).total_seconds()
        return max(0.0, float(cooldown_seconds) - elapsed)


__all__ = [
    "LevelingRepository",
    "LevelingSettings",
    "RepOutcome",
    "MAX_LEVEL",
    "REPUTATION_STATEMENTS",
    "level_from_xp",
    "level_progress",
    "total_xp_for_level",
    "xp_to_next_level",
]
