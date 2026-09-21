"""
Moderation log destination resolution.

Several features (AutoMod enforcement, bulk deletion, moderation commands) need
to answer the same question: "where does this guild want audit output?". The
answer is resolved in one place so an operator configures a single channel and
receives everything there.

Resolution order:

1. ``guild_settings.mod_log_channel_id``  - the modern, dashboard-managed field
2. ``guild_settings.audit_log_channel_id`` - fallback when only auditing is set
3. ``guild_configs.log_channel_id``        - the legacy column the ``/logs``
   and ``/mod`` commands still write to

Writing to the log is always best effort: a deleted channel, a revoked
permission or a database hiccup must never fail the moderation action that has
already happened.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger("fyrion.utils.modlog")

NO_MENTIONS = discord.AllowedMentions.none()

# Checked in order; the first non-null value wins.
SETTINGS_PRIORITY: tuple[str, ...] = ("mod_log_channel_id", "audit_log_channel_id")

LEGACY_QUERY = "SELECT log_channel_id FROM guild_configs WHERE guild_id = ?"


async def resolve_log_channel_id(db: Any, guild_id: int) -> int | None:
    """Returns the configured log channel id for a guild, or None."""
    getter = getattr(db, "get_guild_settings", None)
    if getter is not None:
        try:
            settings = await getter(guild_id)
        except Exception:
            log.exception("Could not read guild settings for %s.", guild_id)
            settings = {}
        for key in SETTINGS_PRIORITY:
            raw = settings.get(key) if isinstance(settings, dict) else None
            if raw:
                return int(raw)

    fetchrow = getattr(db, "fetchrow", None)
    if fetchrow is None:
        return None

    try:
        row = await fetchrow(LEGACY_QUERY, (int(guild_id),))
    except Exception:
        log.exception("Could not read the legacy log channel for %s.", guild_id)
        return None

    if row is None:
        return None

    try:
        raw = row["log_channel_id"]
    except (KeyError, IndexError, TypeError):
        return None
    return int(raw) if raw else None


async def resolve_log_channel(
    db: Any, guild: discord.Guild
) -> discord.TextChannel | None:
    """Returns the guild's log channel when it exists and is writable."""
    channel_id = await resolve_log_channel_id(db, guild.id)
    if channel_id is None:
        return None

    channel = guild.get_channel(channel_id)
    me = guild.me
    if not isinstance(channel, discord.TextChannel) or me is None:
        return None

    permissions = channel.permissions_for(me)
    if not (permissions.send_messages and permissions.embed_links):
        log.debug(
            "Cannot write the moderation log in guild %s: missing Send Messages "
            "or Embed Links in #%s.",
            guild.id,
            channel.name,
        )
        return None
    return channel


async def send_log(db: Any, guild: discord.Guild, embed: discord.Embed) -> bool:
    """Posts an embed to the guild's log channel. Returns True when delivered."""
    channel = await resolve_log_channel(db, guild)
    if channel is None:
        return False

    try:
        await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException as exc:
        log.warning("Failed to write the moderation log in %s: %s", guild.id, exc)
        return False
    return True


__all__ = [
    "NO_MENTIONS",
    "SETTINGS_PRIORITY",
    "resolve_log_channel",
    "resolve_log_channel_id",
    "send_log",
]
