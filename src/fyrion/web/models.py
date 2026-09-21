"""
Request and response models for the dashboard API.

The update model mirrors the writable columns of ``guild_settings``. It is
strict on purpose:

* ``extra="forbid"`` rejects unknown keys instead of silently ignoring them,
* snowflake fields are range-checked so a client cannot store an impossible id,
* free-text fields are length-capped to Discord's message limit,
* ``exclude_unset`` semantics distinguish "leave alone" from "clear" (null).

Only fields declared here can ever reach the database, and the pool validates
the column names again before building SQL.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Snowflakes are 64-bit unsigned values in practice; anything larger is bogus.
MAX_SNOWFLAKE = (1 << 63) - 1
MIN_SNOWFLAKE = 1

SNOWFLAKE_FIELDS: tuple[str, ...] = (
    "mod_log_channel_id",
    "audit_log_channel_id",
    "message_log_channel_id",
    "welcome_channel_id",
    "goodbye_channel_id",
    "autorole_id",
    "mute_role_id",
    "ticket_category_id",
    "ticket_log_channel_id",
    "ticket_support_role_id",
    "leveling_announce_channel_id",
)

BOOLEAN_FIELDS: tuple[str, ...] = (
    "economy_enabled",
    "leveling_enabled",
    "leveling_stack_rewards",
    "automod_enabled",
    "dashboard_enabled",
)

# Fields that must reference an existing channel / category in the guild.
CHANNEL_FIELDS: frozenset[str] = frozenset(
    {
        "mod_log_channel_id",
        "audit_log_channel_id",
        "message_log_channel_id",
        "welcome_channel_id",
        "goodbye_channel_id",
        "ticket_category_id",
        "ticket_log_channel_id",
        "leveling_announce_channel_id",
    }
)

# Fields that must reference an existing role in the guild.
ROLE_FIELDS: frozenset[str] = frozenset(
    {"autorole_id", "mute_role_id", "ticket_support_role_id"}
)


class GuildSettingsUpdate(BaseModel):
    """Partial update of a guild's settings."""

    model_config = ConfigDict(extra="forbid")

    prefix: str | None = Field(default=None, min_length=1, max_length=8)
    locale: str | None = Field(default=None, min_length=2, max_length=16)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    mod_log_channel_id: int | None = None
    audit_log_channel_id: int | None = None
    message_log_channel_id: int | None = None

    welcome_channel_id: int | None = None
    welcome_message: str | None = Field(default=None, max_length=2000)
    goodbye_channel_id: int | None = None
    goodbye_message: str | None = Field(default=None, max_length=2000)
    autorole_id: int | None = None
    mute_role_id: int | None = None

    ticket_category_id: int | None = None
    ticket_log_channel_id: int | None = None
    ticket_support_role_id: int | None = None

    economy_enabled: bool | None = None
    economy_currency_symbol: str | None = Field(
        default=None, min_length=1, max_length=8
    )
    economy_daily_amount: int | None = Field(default=None, ge=0, le=1_000_000)
    economy_work_amount: int | None = Field(default=None, ge=0, le=1_000_000)

    leveling_enabled: bool | None = None
    leveling_announce_channel_id: int | None = None
    leveling_xp_per_message: int | None = Field(default=None, ge=0, le=10_000)
    leveling_cooldown_seconds: int | None = Field(default=None, ge=0, le=86_400)
    leveling_stack_rewards: bool | None = None

    automod_enabled: bool | None = None
    dashboard_enabled: bool | None = None

    @field_validator(*SNOWFLAKE_FIELDS)
    @classmethod
    def _validate_snowflake(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not MIN_SNOWFLAKE <= value <= MAX_SNOWFLAKE:
            raise ValueError("must be a valid Discord snowflake")
        return value

    @field_validator("prefix")
    @classmethod
    def _validate_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(char.isspace() for char in value):
            raise ValueError("must not contain whitespace")
        return value

    def to_columns(self) -> dict[str, Any]:
        """Returns the touched fields as database column values.

        ``exclude_unset`` keeps an explicit ``null`` (clear the setting) while
        omitting fields the client did not mention. Booleans become 0/1 because
        that is what SQLite stores and what the CHECK constraints expect.
        """
        values = self.model_dump(exclude_unset=True)
        for field in BOOLEAN_FIELDS:
            if field in values and values[field] is not None:
                values[field] = int(bool(values[field]))
        return values


__all__ = [
    "GuildSettingsUpdate",
    "SNOWFLAKE_FIELDS",
    "BOOLEAN_FIELDS",
    "CHANNEL_FIELDS",
    "ROLE_FIELDS",
    "MAX_SNOWFLAKE",
]
