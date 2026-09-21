"""
Leveling, level rewards and reputation.

The cog has two halves:

* a listener that awards XP for guild messages. It runs for every message, so it
  is written to be cheap: a cached settings lookup, an in-memory cooldown check,
  then a single atomic upsert. Nothing else touches the database unless the
  member actually levelled up.
* the ``/rank``, ``/leaderboard-levels``, ``/level-rewards``, ``/level-toggle``,
  ``/set-levelchannel`` and ``/rep`` commands.

Design notes worth stating explicitly:

* The level curve and every storage detail live in
  :mod:`fyrion.database.repositories.leveling`, so the XP maths is testable
  without a Discord client.
* Configuration is cached with a short TTL and invalidated on every write, so a
  ``/level-toggle`` takes effect on the next message rather than after a timeout.
* Reward roles are validated before every assignment: a role that is managed by
  Discord, or that ranks at or above Fyrion's highest role, is reported once and
  skipped instead of producing a failed API call per level-up.
* ``default_permissions`` only decides whether Discord *shows* a command, so
  every configuration command re-checks the invoker's permissions server side.
* Display names are attacker controlled, so every reply disables mention parsing
  except for the single member a level-up announcement addresses.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from fyrion.database.repositories.leveling import (
    MAX_LEVEL,
    LevelingRepository,
    LevelingSettings,
    level_from_xp,
    level_progress,
    total_xp_for_level,
)
from fyrion.utils.modlog import send_log
from fyrion.utils.permissions import can_manage_role, missing_channel_permissions

log = logging.getLogger("fyrion.cogs.leveling")

NO_MENTIONS = discord.AllowedMentions.none()
# Announcements address exactly one member, so user mentions are allowed and
# nothing else is: a crafted nickname can never make Fyrion ping a role.
NOTICE_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)

# How long a settings snapshot is trusted. Writes invalidate it immediately;
# the TTL only covers changes made outside this cog (for example the dashboard).
SETTINGS_TTL_SECONDS = 60.0
# How long an idle XP cooldown entry is kept before the pruning loop drops it.
COOLDOWN_IDLE_TTL_SECONDS = 3600.0

PROGRESS_SEGMENTS = 14
FILLED_BLOCK = "\u2588"
EMPTY_BLOCK = "\u2591"

LEADERBOARD_MIN = 3
LEADERBOARD_MAX = 25
MEDALS = ("\U0001f947", "\U0001f948", "\U0001f949")

# A server with hundreds of reward roles is a configuration mistake, and each
# level-up would have to reconcile all of them.
MAX_REWARDS_PER_GUILD = 50

# One reputation point every twelve hours per member.
REP_COOLDOWN_SECONDS = 12 * 3600


def progress_bar(into: int, required: int) -> str:
    """Renders a fixed-width progress bar for the current level."""
    if required <= 0:
        return FILLED_BLOCK * PROGRESS_SEGMENTS
    fraction = min(1.0, max(0.0, into / required))
    filled = int(round(fraction * PROGRESS_SEGMENTS))
    filled = min(PROGRESS_SEGMENTS, max(0, filled))
    return FILLED_BLOCK * filled + EMPTY_BLOCK * (PROGRESS_SEGMENTS - filled)


def format_delay(seconds: float) -> str:
    """Renders a retry delay as a short human readable string."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class Leveling(commands.Cog):
    """Experience, levels, level rewards and reputation."""

    rewards = app_commands.Group(
        name="level-rewards",
        description="Manage the roles granted when members reach a level.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = LevelingRepository(self.db)

        # guild_id -> (expires_at, settings)
        self._settings_cache: dict[int, tuple[float, LevelingSettings]] = {}
        # (guild_id, user_id) -> monotonic timestamp of the last award
        self._cooldowns: dict[tuple[int, int], float] = {}
        # Reward roles that cannot be assigned, so the warning is logged once.
        self._unassignable: set[tuple[int, int]] = set()

        self._prune_cooldowns.start()

    async def cog_load(self) -> None:
        try:
            await self.repo.ensure_schema()
        except Exception:
            # Reputation is optional; levels must still work if the DDL fails.
            log.exception("Could not prepare the reputation table.")

    async def cog_unload(self) -> None:
        self._prune_cooldowns.cancel()

    # ------------------------------------------------------------------
    # Settings cache
    # ------------------------------------------------------------------

    def invalidate(self, guild_id: int) -> None:
        self._settings_cache.pop(int(guild_id), None)

    async def _settings(self, guild_id: int) -> LevelingSettings:
        now = time.monotonic()
        cached = self._settings_cache.get(guild_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        try:
            settings = await self.repo.get_settings(guild_id)
        except Exception:
            # Cache a disabled snapshot briefly: a broken database must not turn
            # into one failing query per message.
            log.exception("Could not load leveling settings for guild %s.", guild_id)
            settings = LevelingSettings()

        self._settings_cache[guild_id] = (now + SETTINGS_TTL_SECONDS, settings)
        return settings

    @tasks.loop(minutes=10)
    async def _prune_cooldowns(self) -> None:
        """Drops idle cooldown entries and expired settings snapshots.

        Without this the cooldown dictionary would grow with every member who
        has ever spoken in any guild.
        """
        now = time.monotonic()

        stale_cooldowns = [
            key
            for key, stamp in self._cooldowns.items()
            if now - stamp > COOLDOWN_IDLE_TTL_SECONDS
        ]
        for key in stale_cooldowns:
            del self._cooldowns[key]

        stale_settings = [
            guild_id
            for guild_id, (expires_at, _) in self._settings_cache.items()
            if expires_at <= now
        ]
        for guild_id in stale_settings:
            del self._settings_cache[guild_id]

        if stale_cooldowns or stale_settings:
            log.debug(
                "Leveling pruned %d cooldown(s) and %d settings snapshot(s).",
                len(stale_cooldowns),
                len(stale_settings),
            )

    @_prune_cooldowns.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Authorization helpers
    # ------------------------------------------------------------------

    async def _respond(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )

    async def _reject(self, interaction: discord.Interaction, reason: str) -> None:
        await self._respond(interaction, f"\u274c {reason}")

    async def _authorize(
        self, interaction: discord.Interaction, *permissions: str
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Re-checks the invoker's permissions server side."""
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return None

        effective = member.guild_permissions
        if not effective.administrator:
            missing = [
                name for name in permissions if not getattr(effective, name, False)
            ]
            if missing:
                readable = ", ".join(
                    f"`{name.replace('_', ' ').title()}`" for name in missing
                )
                await self._reject(
                    interaction, f"You need {readable} to use that command."
                )
                return None

        if guild.me is None:
            await self._reject(
                interaction, "I could not resolve my own membership in this server."
            )
            return None

        return guild, member

    async def _audit(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        action: str,
        detail: str,
    ) -> None:
        """Mirrors a configuration change into the guild's log channel."""
        embed = discord.Embed(
            title=f"Leveling: {action}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Performed by", value=f"{actor} (`{actor.id}`)", inline=False
        )
        embed.add_field(name="Details", value=detail[:1024], inline=False)
        await send_log(self.db, guild, embed)

    # ------------------------------------------------------------------
    # XP listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        author = message.author

        if guild is None or message.webhook_id is not None:
            return
        if not isinstance(author, discord.Member) or author.bot:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            # System messages (pins, joins, boosts) carry no member effort.
            return

        settings = await self._settings(guild.id)
        if not settings.enabled or settings.xp_per_message <= 0:
            return

        key = (guild.id, author.id)
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and now - last < settings.cooldown_seconds:
            return
        self._cooldowns[key] = now

        try:
            profile = await self.repo.award_xp(
                guild.id, author.id, settings.xp_per_message
            )
        except Exception:
            log.exception(
                "Could not award XP to member %s in guild %s.", author.id, guild.id
            )
            return

        stored_level = int(profile.get("level") or 0)
        total_xp = int(profile.get("xp") or 0)
        new_level = level_from_xp(total_xp)
        if new_level <= stored_level:
            return

        try:
            await self.repo.set_level(guild.id, author.id, new_level)
        except Exception:
            log.exception(
                "Could not store level %d for member %s in guild %s.",
                new_level,
                author.id,
                guild.id,
            )
            return

        granted = await self._sync_rewards(author, new_level, settings)
        await self._announce(message, author, new_level, granted, settings)

    async def _sync_rewards(
        self,
        member: discord.Member,
        level: int,
        settings: LevelingSettings,
    ) -> list[discord.Role]:
        """Applies the level reward roles a member has earned.

        With ``leveling_stack_rewards`` enabled the member keeps every earned
        role. Otherwise (or when the highest earned reward is flagged
        ``remove_previous``) only the highest tier is kept and the lower ones are
        stripped, which is what a rank-ladder setup expects.
        """
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return []

        try:
            earned = await self.repo.rewards_up_to(guild.id, level)
        except Exception:
            log.exception("Could not read level rewards for guild %s.", guild.id)
            return []

        if not earned:
            return []

        top_level = max(int(row.get("level") or 0) for row in earned)
        top_tier = [row for row in earned if int(row.get("level") or 0) == top_level]
        strip_lower = not settings.stack_rewards or any(
            bool(row.get("remove_previous")) for row in top_tier
        )

        keep_rows = top_tier if strip_lower else earned
        keep_ids = {int(row["role_id"]) for row in keep_rows}
        drop_ids = (
            {
                int(row["role_id"])
                for row in earned
                if int(row["role_id"]) not in keep_ids
            }
            if strip_lower
            else set()
        )

        held = {role.id for role in member.roles}

        to_add: list[discord.Role] = []
        for role_id in keep_ids:
            if role_id in held:
                continue
            role = self._assignable_role(guild, role_id)
            if role is not None:
                to_add.append(role)

        to_remove: list[discord.Role] = []
        for role_id in drop_ids:
            if role_id not in held:
                continue
            role = self._assignable_role(guild, role_id)
            if role is not None:
                to_remove.append(role)

        reason = f"Level reward: reached level {level}"

        if to_add:
            try:
                await member.add_roles(*to_add, reason=reason)
            except discord.Forbidden:
                log.warning(
                    "Discord refused level reward roles for member %s in guild %s.",
                    member.id,
                    guild.id,
                )
                to_add = []
            except discord.HTTPException as exc:
                log.warning("Level reward assignment failed: %s", exc)
                to_add = []

        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason=reason)
            except discord.HTTPException as exc:
                log.warning("Could not strip superseded level roles: %s", exc)

        return to_add

    def _assignable_role(
        self, guild: discord.Guild, role_id: int
    ) -> discord.Role | None:
        """Returns a reward role only when Fyrion can actually manage it."""
        me = guild.me
        if me is None:
            return None

        role = guild.get_role(int(role_id))
        key = (guild.id, int(role_id))

        if role is None or role.is_default() or role.managed or me.top_role <= role:
            if key not in self._unassignable:
                self._unassignable.add(key)
                log.warning(
                    "Level reward role %s in guild %s cannot be assigned: it is "
                    "missing, managed by Discord, or ranks at or above my "
                    "highest role.",
                    role_id,
                    guild.id,
                )
            return None

        self._unassignable.discard(key)
        return role

    async def _announce(
        self,
        message: discord.Message,
        member: discord.Member,
        level: int,
        granted: list[discord.Role],
        settings: LevelingSettings,
    ) -> None:
        """Posts the level-up notice, if a writable destination exists."""
        guild = member.guild
        me = guild.me
        if me is None:
            return

        channel: discord.abc.Messageable | None = None
        if settings.announce_channel_id:
            candidate = guild.get_channel(settings.announce_channel_id)
            if isinstance(candidate, discord.TextChannel):
                permissions = candidate.permissions_for(me)
                if permissions.send_messages and permissions.embed_links:
                    channel = candidate
        elif isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            permissions = message.channel.permissions_for(me)
            if permissions.send_messages and permissions.embed_links:
                channel = message.channel

        if channel is None:
            return

        embed = discord.Embed(
            title="Level up",
            description=f"{member.mention} reached **level {level}**.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if granted:
            embed.add_field(
                name="Roles unlocked",
                value=", ".join(role.mention for role in granted)[:1024],
                inline=False,
            )

        try:
            await channel.send(embed=embed, allowed_mentions=NOTICE_MENTIONS)
        except discord.HTTPException:
            # The announcement is cosmetic; the XP and roles are already stored.
            pass

    # ------------------------------------------------------------------
    # /rank
    # ------------------------------------------------------------------

    @app_commands.command(
        name="rank", description="Show a member's level, XP and reputation."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to inspect (defaults to you)")
    async def rank_cmd(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return

        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await self._reject(interaction, "That member is not in this server.")
            return
        if target.bot:
            await self._reject(interaction, "Bots do not earn experience.")
            return

        await interaction.response.defer()

        profile = await self.repo.get_profile(guild.id, target.id)
        total_xp = int(profile.get("xp") or 0)
        stored_level = int(profile.get("level") or 0)
        level, into, required = level_progress(total_xp)

        # Heal drift if the curve changed or a level write was lost.
        if stored_level != level:
            try:
                await self.repo.set_level(guild.id, target.id, level)
            except Exception:
                log.exception(
                    "Could not reconcile the stored level for member %s.", target.id
                )

        rank = await self.repo.rank_of(guild.id, target.id, total_xp)
        tracked = await self.repo.tracked_members(guild.id)
        reputation = await self.repo.get_rep(guild.id, target.id)
        settings = await self._settings(guild.id)

        embed = discord.Embed(
            title=f"Rank: {target.display_name}",
            color=target.color if target.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=str(level), inline=True)
        embed.add_field(name="Rank", value=f"#{rank} of {max(tracked, 1)}", inline=True)
        embed.add_field(name="Reputation", value=str(reputation), inline=True)

        if required > 0:
            embed.add_field(
                name=f"Progress to level {level + 1}",
                value=(
                    f"`{progress_bar(into, required)}`\n"
                    f"{into:,} / {required:,} XP "
                    f"({required - into:,} to go)"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Progress",
                value=f"`{progress_bar(1, 1)}`\nMaximum level reached.",
                inline=False,
            )

        embed.add_field(name="Total XP", value=f"{total_xp:,}", inline=True)
        embed.add_field(
            name="Messages counted",
            value=f"{int(profile.get('total_messages') or 0):,}",
            inline=True,
        )

        if not settings.enabled:
            embed.set_footer(
                text=(
                    "Leveling is currently disabled in this server, so no new XP "
                    "is being awarded."
                )
            )

        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------------
    # /leaderboard-levels
    # ------------------------------------------------------------------

    @app_commands.command(
        name="leaderboard-levels",
        description="Show the members with the most experience in this server.",
    )
    @app_commands.guild_only()
    @app_commands.describe(limit="How many members to list (3-25)")
    async def leaderboard_levels_cmd(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, LEADERBOARD_MIN, LEADERBOARD_MAX] = 10,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return

        await interaction.response.defer()

        rows = await self.repo.leaderboard(guild.id, limit)
        if not rows:
            await interaction.followup.send(
                "\u2139\ufe0f Nobody has earned experience in this server yet.",
                allowed_mentions=NO_MENTIONS,
            )
            return

        tracked = await self.repo.tracked_members(guild.id)
        settings = await self._settings(guild.id)

        lines: list[str] = []
        for index, row in enumerate(rows, start=1):
            user_id = int(row.get("user_id") or 0)
            total_xp = int(row.get("xp") or 0)
            level = level_from_xp(total_xp)
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"**{index}.**"
            lines.append(
                f"{prefix} <@{user_id}> \u2014 level **{level}** " f"({total_xp:,} XP)"
            )

        embed = discord.Embed(
            title=f"Level leaderboard: {guild.name}",
            description="\n".join(lines)[:4000],
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text=(
                f"{tracked} member(s) tracked"
                + ("" if settings.enabled else " \u2022 leveling is disabled")
            )
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)

        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------------
    # /rep
    # ------------------------------------------------------------------

    @app_commands.command(
        name="rep", description="Give a reputation point to another member."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to thank")
    async def rep_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        guild = interaction.guild
        giver = interaction.user
        if guild is None or not isinstance(giver, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return

        if member.id == giver.id:
            await self._reject(interaction, "You cannot give reputation to yourself.")
            return
        if member.bot:
            await self._reject(interaction, "Bots cannot receive reputation.")
            return
        if member.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return

        await interaction.response.defer()

        try:
            outcome = await self.repo.give_rep(
                guild.id,
                giver.id,
                member.id,
                cooldown_seconds=REP_COOLDOWN_SECONDS,
            )
        except Exception:
            log.exception("Could not record reputation in guild %s.", guild.id)
            await interaction.followup.send(
                "\u274c Reputation could not be recorded right now. Please try "
                "again in a moment.",
                ephemeral=True,
            )
            return

        if not outcome.granted:
            await interaction.followup.send(
                "\u23f3 You have already given reputation recently. You can give "
                f"another point in **{format_delay(outcome.retry_after)}**.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Reputation given",
            description=(
                f"{giver.mention} gave a reputation point to {member.mention}."
            ),
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Total reputation", value=str(outcome.total), inline=True)
        embed.set_footer(
            text=f"One point every {REP_COOLDOWN_SECONDS // 3600} hours per member."
        )

        await interaction.followup.send(embed=embed, allowed_mentions=NOTICE_MENTIONS)

    # ------------------------------------------------------------------
    # /level-toggle
    # ------------------------------------------------------------------

    @app_commands.command(
        name="level-toggle",
        description="Enable or disable leveling and tune its XP settings.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="True awards XP for messages, False stops awarding it",
        xp_per_message="XP granted per counted message (default 15)",
        cooldown_seconds="Seconds between two counted messages (default 60)",
        stack_rewards="Keep every earned reward role instead of only the highest",
    )
    async def level_toggle_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        xp_per_message: Optional[app_commands.Range[int, 1, 500]] = None,
        cooldown_seconds: Optional[app_commands.Range[int, 0, 3600]] = None,
        stack_rewards: Optional[bool] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        values: dict[str, Any] = {"leveling_enabled": int(bool(enabled))}
        if xp_per_message is not None:
            values["leveling_xp_per_message"] = int(xp_per_message)
        if cooldown_seconds is not None:
            values["leveling_cooldown_seconds"] = int(cooldown_seconds)
        if stack_rewards is not None:
            values["leveling_stack_rewards"] = int(bool(stack_rewards))

        await self.repo.update_settings(guild.id, **values)
        self.invalidate(guild.id)

        settings = await self.repo.get_settings(guild.id)
        state = "enabled" if settings.enabled else "disabled"

        await self._audit(
            guild,
            member,
            "Settings changed",
            f"Leveling: {state}\nXP per message: {settings.xp_per_message}\n"
            f"Cooldown: {settings.cooldown_seconds}s\n"
            f"Stack rewards: {settings.stack_rewards}",
        )

        lines = [
            f"\u2705 Leveling is now **{state}**.",
            f"XP per message: **{settings.xp_per_message}** "
            f"\u2022 cooldown: **{settings.cooldown_seconds}s** "
            f"\u2022 stacking rewards: **{settings.stack_rewards}**",
        ]
        if settings.enabled and settings.announce_channel_id is None:
            lines.append(
                "Level-ups are announced in the channel the message was sent in. "
                "Use `/set-levelchannel` to send them somewhere specific."
            )
        await self._respond(interaction, "\n".join(lines))

    # ------------------------------------------------------------------
    # /set-levelchannel
    # ------------------------------------------------------------------

    @app_commands.command(
        name="set-levelchannel",
        description="Choose where level-up announcements are posted.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel=(
            "Where level-ups are announced (leave empty to announce in the "
            "channel the member was talking in)"
        )
    )
    async def set_levelchannel_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        if channel is None:
            await self.repo.update_settings(guild.id, leveling_announce_channel_id=None)
            self.invalidate(guild.id)
            await self._audit(
                guild, member, "Announcement channel cleared", "Channel cleared"
            )
            await self._respond(
                interaction,
                "\u2705 Level-ups will now be announced in the channel the member "
                "was talking in.",
            )
            return

        if channel.guild.id != guild.id:
            await self._reject(
                interaction, "That channel does not belong to this server."
            )
            return

        me = guild.me
        assert me is not None  # guaranteed by _authorize
        missing = missing_channel_permissions(
            me, channel, view_channel=True, send_messages=True, embed_links=True
        )
        if missing:
            names = ", ".join(f"`{name}`" for name in missing)
            await self._reject(
                interaction, f"I am missing {names} in {channel.mention}."
            )
            return

        await self.repo.update_settings(
            guild.id, leveling_announce_channel_id=channel.id
        )
        self.invalidate(guild.id)

        await self._audit(
            guild,
            member,
            "Announcement channel set",
            f"Channel: #{channel.name} (`{channel.id}`)",
        )
        await self._respond(
            interaction, f"\u2705 Level-ups will be announced in {channel.mention}."
        )

    # ------------------------------------------------------------------
    # /level-rewards
    # ------------------------------------------------------------------

    @rewards.command(name="add", description="Grant a role when members reach a level.")
    @app_commands.describe(
        level="The level that unlocks the role",
        role="The role to grant",
        remove_previous="Strip lower level reward roles when this one is granted",
    )
    async def rewards_add_cmd(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, MAX_LEVEL],
        role: discord.Role,
        remove_previous: bool = False,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild", "manage_roles")
        if context is None:
            return
        guild, member = context

        if role.guild.id != guild.id:
            await self._reject(interaction, "That role does not belong to this server.")
            return

        # A role Fyrion cannot assign would fail silently on every level-up, so
        # it is refused at configuration time instead.
        if error := can_manage_role(member, role):
            await self._reject(interaction, error)
            return

        existing = await self.repo.list_rewards(guild.id)
        already = any(
            int(row.get("level") or 0) == int(level)
            and int(row.get("role_id") or 0) == role.id
            for row in existing
        )
        if not already and len(existing) >= MAX_REWARDS_PER_GUILD:
            await self._reject(
                interaction,
                f"This server already has {MAX_REWARDS_PER_GUILD} level rewards, "
                "which is the maximum. Remove one first.",
            )
            return

        try:
            await self.repo.add_reward(
                guild.id, int(level), role.id, remove_previous=remove_previous
            )
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return

        self._unassignable.discard((guild.id, role.id))

        await self._audit(
            guild,
            member,
            "Level reward saved",
            f"Level: {level}\nRole: {role.name} (`{role.id}`)\n"
            f"Remove previous: {remove_previous}",
        )

        needed = total_xp_for_level(int(level))
        verb = "Updated" if already else "Added"
        lines = [
            f"\u2705 {verb} the level reward: {role.mention} is granted at "
            f"**level {level}** ({needed:,} total XP)."
        ]
        if remove_previous:
            lines.append(
                "Lower level reward roles will be removed when this one is granted."
            )
        lines.append(
            "Existing members receive it the next time they level up; it is not "
            "applied retroactively."
        )
        await self._respond(interaction, "\n".join(lines))

    @rewards.command(name="remove", description="Stop granting a role at a level.")
    @app_commands.describe(
        level="The level the reward is attached to",
        role="The specific role to unmap (leave empty to clear the whole level)",
    )
    async def rewards_remove_cmd(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, MAX_LEVEL],
        role: Optional[discord.Role] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild", "manage_roles")
        if context is None:
            return
        guild, member = context

        if role is not None and role.guild.id != guild.id:
            await self._reject(interaction, "That role does not belong to this server.")
            return

        removed = await self.repo.remove_reward(
            guild.id, int(level), role.id if role is not None else None
        )
        if not removed:
            await self._respond(
                interaction,
                "\u2139\ufe0f There was no matching level reward configured.",
            )
            return

        await self._audit(
            guild,
            member,
            "Level reward removed",
            f"Level: {level}\nRole: "
            + (f"{role.name} (`{role.id}`)" if role is not None else "all")
            + f"\nMappings removed: {removed}",
        )

        await self._respond(
            interaction,
            f"\u2705 Removed {removed} level reward(s) for level {level}. Roles "
            "members already hold were left untouched.",
        )

    @rewards.command(name="list", description="List this server's level rewards.")
    async def rewards_list_cmd(self, interaction: discord.Interaction) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, _ = context

        rows = await self.repo.list_rewards(guild.id)
        if not rows:
            await self._respond(
                interaction,
                "\u2139\ufe0f No level rewards are configured. Add one with "
                "`/level-rewards add`.",
            )
            return

        settings = await self._settings(guild.id)
        me = guild.me

        lines: list[str] = []
        for row in rows:
            level = int(row.get("level") or 0)
            role_id = int(row.get("role_id") or 0)
            role = guild.get_role(role_id)
            label = role.mention if role is not None else f"missing role `{role_id}`"

            flags: list[str] = []
            if row.get("remove_previous"):
                flags.append("removes lower tiers")
            if (
                role is not None
                and me is not None
                and (role.managed or role.is_default() or me.top_role <= role)
            ):
                flags.append("\u26a0\ufe0f not assignable by me")

            suffix = f" \u2014 {', '.join(flags)}" if flags else ""
            lines.append(
                f"**Level {level}** ({total_xp_for_level(level):,} XP): "
                f"{label}{suffix}"
            )

        embed = discord.Embed(
            title="Level rewards",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                "Stacking: "
                + (
                    "every earned role is kept"
                    if settings.stack_rewards
                    else "only the highest tier is kept"
                )
            )
        )

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leveling(bot))


__all__ = [
    "Leveling",
    "MAX_REWARDS_PER_GUILD",
    "REP_COOLDOWN_SECONDS",
    "format_delay",
    "progress_bar",
    "setup",
]
