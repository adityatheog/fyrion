"""
Core moderation cog.

Every member-targeting command runs ``fyrion.utils.permissions.can_moderate``
before touching the Discord API. ``default_permissions`` only affects
client-side visibility, so the hierarchy check is the authoritative gate: it
confirms that the invoker outranks the target and that the bot does too.

Reasons are attacker-controlled text, so every reply that echoes one disables
mention parsing.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.cogs._base import NO_MENTIONS, FyrionCog
from fyrion.database.repositories.guild_config import GuildConfigRepository
from fyrion.database.repositories.warnings import WarningsRepository
from fyrion.utils.permissions import can_moderate

log = logging.getLogger("fyrion.cogs.moderation")

# Discord rejects communication timeouts longer than 28 days.
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
# Embeds allow 25 fields; stay comfortably below the limit.
WARNINGS_DISPLAY_LIMIT = 10
# Discord truncates audit log reasons at 512 characters.
AUDIT_REASON_LIMIT = 512
# Slowmode is capped at six hours by the API.
MAX_SLOWMODE_SECONDS = 21600


class Moderation(FyrionCog, commands.GroupCog, name="mod"):
    """Server moderation commands."""

    # Moderation replies are public by default (mod actions are announced);
    # only refusals are ephemeral.
    DEFAULT_EPHEMERAL = False

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self.warnings = WarningsRepository(self.db)
        self.configs = GuildConfigRepository(self.db)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_reason(moderator: discord.abc.User, reason: str) -> str:
        return f"{moderator} ({moderator.id}): {reason}"[:AUDIT_REASON_LIMIT]

    @staticmethod
    def _hierarchy_error(
        interaction: discord.Interaction, target: discord.Member
    ) -> str | None:
        """Returns an error string when the action must be refused."""
        invoker = interaction.user
        if not isinstance(invoker, discord.Member):
            return "This command can only be used inside a server."
        return can_moderate(invoker, target)

    async def _log_action(
        self,
        guild: discord.Guild,
        action: str,
        target: discord.abc.User | discord.Object,
        moderator: discord.abc.User,
        reason: str,
        detail: str | None = None,
    ) -> None:
        """Mirrors a completed action into the configured log channel, if any.

        Logging is best effort: a missing channel or permission never fails the
        command that already succeeded.
        """
        try:
            config = await self.configs.get_config(guild.id)
        except Exception:
            log.exception("Could not read guild config while logging %s", action)
            return

        channel_id = config.get("log_channel_id")
        if not channel_id:
            return

        channel = guild.get_channel(int(channel_id))
        me = guild.me
        if not isinstance(channel, discord.TextChannel) or me is None:
            return
        if not channel.permissions_for(me).send_messages:
            return

        embed = discord.Embed(
            title=f"Moderation: {action}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Target", value=f"{target} (`{target.id}`)", inline=False)
        embed.add_field(
            name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=False
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        if detail:
            embed.add_field(name="Details", value=detail, inline=False)

        try:
            await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("Failed to write moderation log in guild %s: %s", guild.id, exc)

    # ------------------------------------------------------------------
    # Removal commands
    # ------------------------------------------------------------------

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.describe(
        member="The member to kick",
        reason="Reason recorded in the audit log",
    )
    async def kick_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        if error := self._hierarchy_error(interaction, member):
            await self._reject(interaction, error)
            return

        guild = member.guild
        try:
            await member.kick(reason=self._audit_reason(interaction.user, reason))
        except discord.Forbidden:
            await self._reject(
                interaction, "Discord refused the kick (missing permission or hierarchy)."
            )
            return

        await self._respond(interaction, f"\u2705 Kicked **{member}**. Reason: {reason}")
        await self._log_action(guild, "Kick", member, interaction.user, reason)

    @app_commands.command(
        name="ban", description="Ban a user, whether or not they are in the server."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(
        user="The user to ban (accepts an ID for users who already left)",
        reason="Reason recorded in the audit log",
        delete_days="Days of the user's recent messages to delete (0-7)",
    )
    async def ban_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "No reason provided",
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._reject(interaction, "This command can only be used in a server.")
            return

        # Hierarchy only applies to users currently in the guild; banning by ID
        # a user who already left has no role to compare against.
        member = guild.get_member(user.id)
        if member is not None:
            if error := self._hierarchy_error(interaction, member):
                await self._reject(interaction, error)
                return

        try:
            await guild.ban(
                user,
                reason=self._audit_reason(interaction.user, reason),
                delete_message_seconds=delete_days * 86400,
            )
        except discord.Forbidden:
            await self._reject(
                interaction, "Discord refused the ban (missing permission or hierarchy)."
            )
            return
        except discord.NotFound:
            await self._reject(interaction, "That user does not exist.")
            return

        await self._respond(interaction, f"\u2705 Banned **{user}**. Reason: {reason}")
        await self._log_action(
            guild,
            "Ban",
            user,
            interaction.user,
            reason,
            detail=f"Deleted {delete_days} day(s) of messages",
        )

    @app_commands.command(name="unban", description="Lift a ban from a user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(user="The banned user (accepts a user ID)", reason="Reason")
    async def unban_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "No reason provided",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._reject(interaction, "This command can only be used in a server.")
            return

        try:
            await guild.unban(user, reason=self._audit_reason(interaction.user, reason))
        except discord.NotFound:
            await self._reject(interaction, f"**{user}** is not banned in this server.")
            return
        except discord.Forbidden:
            await self._reject(interaction, "I lack the `Ban Members` permission.")
            return

        await self._respond(interaction, f"\u2705 Unbanned **{user}**. Reason: {reason}")
        await self._log_action(guild, "Unban", user, interaction.user, reason)

    # ------------------------------------------------------------------
    # Timeouts
    # ------------------------------------------------------------------

    @app_commands.command(
        name="timeout", description="Temporarily mute a member using Discord timeouts."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        member="The member to time out",
        minutes="Duration in minutes (max 28 days)",
        reason="Reason recorded in the audit log",
    )
    async def timeout_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
        reason: str = "No reason provided",
    ) -> None:
        if error := self._hierarchy_error(interaction, member):
            await self._reject(interaction, error)
            return

        try:
            await member.timeout(
                timedelta(minutes=minutes),
                reason=self._audit_reason(interaction.user, reason),
            )
        except discord.Forbidden:
            await self._reject(
                interaction,
                "Discord refused the timeout (missing permission or hierarchy).",
            )
            return

        await self._respond(
            interaction,
            f"\u2705 **{member}** is timed out for {minutes} minute(s). Reason: {reason}",
        )
        await self._log_action(
            member.guild,
            "Timeout",
            member,
            interaction.user,
            reason,
            detail=f"{minutes} minute(s)",
        )

    @app_commands.command(
        name="untimeout", description="Remove an active timeout from a member."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        if error := self._hierarchy_error(interaction, member):
            await self._reject(interaction, error)
            return

        if member.timed_out_until is None:
            await self._reject(interaction, f"**{member}** is not timed out.")
            return

        try:
            await member.timeout(None, reason=self._audit_reason(interaction.user, reason))
        except discord.Forbidden:
            await self._reject(interaction, "Discord refused to lift the timeout.")
            return

        await self._respond(interaction, f"\u2705 Timeout removed from **{member}**.")
        await self._log_action(
            member.guild, "Timeout removed", member, interaction.user, reason
        )

    # ------------------------------------------------------------------
    # Warnings
    # ------------------------------------------------------------------

    @app_commands.command(name="warn", description="Issue a formal warning to a member.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(member="The member to warn", reason="Why they are warned")
    async def warn_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
    ) -> None:
        if error := self._hierarchy_error(interaction, member):
            await self._reject(interaction, error)
            return

        guild = member.guild
        await self.warnings.add_warning(guild.id, member.id, interaction.user.id, reason)
        total = await self.warnings.count_warnings(guild.id, member.id)

        # Notifying the member is best effort: closed DMs are not an error.
        try:
            await member.send(
                f"\u26a0\ufe0f You were warned in **{guild.name}**. Reason: {reason}",
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            pass

        await self._respond(
            interaction,
            f"\u2705 Warned **{member}** (warning #{total}). Reason: {reason}",
        )
        await self._log_action(
            guild, "Warn", member, interaction.user, reason, detail=f"Total: {total}"
        )

    @app_commands.command(name="warnings", description="List a member's warnings.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warnings_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        guild = member.guild
        records = await self.warnings.get_warnings(guild.id, member.id)

        if not records:
            await self._respond(
                interaction, f"\u2705 **{member}** has no warnings.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Warnings for {member}",
            description=f"{len(records)} warning(s) on record.",
            color=discord.Color.orange(),
        )

        for row in records[:WARNINGS_DISPLAY_LIMIT]:
            created = str(row["created_at"])[:19]
            embed.add_field(
                name=f"#{row['id']} \u2022 {created}",
                value=(
                    f"Reason: {row['reason'] or 'No reason provided'}\n"
                    f"Moderator: <@{row['moderator_id']}>"
                ),
                inline=False,
            )

        if len(records) > WARNINGS_DISPLAY_LIMIT:
            embed.set_footer(
                text=f"Showing the {WARNINGS_DISPLAY_LIMIT} most recent warnings."
            )

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(
        name="delwarn", description="Delete a single warning by its ID."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(warning_id="The warning ID shown by /mod warnings")
    async def delwarn_cmd(
        self, interaction: discord.Interaction, warning_id: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._reject(interaction, "This command can only be used in a server.")
            return

        # Scoped by guild inside the repository, so IDs from other servers
        # cannot be deleted from here.
        if not await self.warnings.delete_warning(guild.id, warning_id):
            await self._reject(
                interaction, f"No warning with ID `{warning_id}` exists in this server."
            )
            return

        await self._respond(
            interaction, f"\u2705 Deleted warning `{warning_id}`.", ephemeral=True
        )

    @app_commands.command(
        name="clearwarns", description="Clear every warning for a member."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def clearwarns_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        # Wiping someone's record is a privileged action, so it obeys the same
        # hierarchy rules as any other moderation command.
        if error := self._hierarchy_error(interaction, member):
            await self._reject(interaction, error)
            return

        removed = await self.warnings.clear_warnings(member.guild.id, member.id)
        if not removed:
            await self._respond(
                interaction, f"\u2139\ufe0f **{member}** had no warnings.", ephemeral=True
            )
            return

        await self._respond(
            interaction, f"\u2705 Cleared {removed} warning(s) for **{member}**."
        )
        await self._log_action(
            member.guild,
            "Warnings cleared",
            member,
            interaction.user,
            "Manual clear",
            detail=f"{removed} warning(s) removed",
        )

    # ------------------------------------------------------------------
    # Channel commands
    # ------------------------------------------------------------------

    @app_commands.command(name="purge", description="Bulk delete recent messages.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many messages to scan and delete (1-100)",
        member="Only delete messages from this member",
    )
    async def purge_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: discord.Member | None = None,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._reject(
                interaction, "Messages can only be purged in text channels and threads."
            )
            return

        # Bulk deletion can take a while; acknowledge before starting.
        await interaction.response.defer(ephemeral=True)

        def _matches(candidate: discord.Message) -> bool:
            return member is None or candidate.author.id == member.id

        try:
            deleted = await channel.purge(
                limit=amount,
                check=_matches,
                reason=self._audit_reason(interaction.user, "Purge"),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "\u274c I need `Manage Messages` and `Read Message History` here.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("Purge failed in channel %s: %s", channel.id, exc)
            await interaction.followup.send(
                "\u274c Discord rejected the purge. Messages older than 14 days "
                "cannot be bulk deleted.",
                ephemeral=True,
            )
            return

        scope = f" from **{member}**" if member is not None else ""
        await interaction.followup.send(
            f"\u2705 Deleted {len(deleted)} message(s){scope}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @app_commands.command(
        name="slowmode", description="Set the slowmode delay for this channel."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(seconds="Delay in seconds; 0 disables slowmode")
    async def slowmode_cmd(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, MAX_SLOWMODE_SECONDS],
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._reject(
                interaction, "Slowmode can only be set on text channels and threads."
            )
            return

        try:
            await channel.edit(
                slowmode_delay=seconds,
                reason=self._audit_reason(interaction.user, "Slowmode change"),
            )
        except discord.Forbidden:
            await self._reject(interaction, "I lack `Manage Channels` here.")
            return

        if seconds == 0:
            await self._respond(interaction, "\u2705 Slowmode disabled.")
        else:
            await self._respond(interaction, f"\u2705 Slowmode set to {seconds}s.")

    @app_commands.command(
        name="lock", description="Prevent @everyone from sending messages here."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def lock_cmd(
        self,
        interaction: discord.Interaction,
        reason: str = "No reason provided",
    ) -> None:
        await self._set_lock(interaction, locked=True, reason=reason)

    @app_commands.command(name="unlock", description="Re-open a locked channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def unlock_cmd(
        self,
        interaction: discord.Interaction,
        reason: str = "No reason provided",
    ) -> None:
        await self._set_lock(interaction, locked=False, reason=reason)

    async def _set_lock(
        self, interaction: discord.Interaction, *, locked: bool, reason: str
    ) -> None:
        """Toggles ``send_messages`` for the default role in the current channel."""
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await self._reject(
                interaction, "This command only works in server text channels."
            )
            return

        overwrite = channel.overwrites_for(guild.default_role)
        # None restores inheritance instead of pinning an explicit allow.
        overwrite.send_messages = False if locked else None

        try:
            await channel.set_permissions(
                guild.default_role,
                overwrite=overwrite,
                reason=self._audit_reason(interaction.user, reason),
            )
        except discord.Forbidden:
            await self._reject(
                interaction, "I lack `Manage Roles`/`Manage Channels` in this channel."
            )
            return

        state = "locked" if locked else "unlocked"
        await self._respond(
            interaction, f"\u2705 {channel.mention} is now **{state}**. Reason: {reason}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
