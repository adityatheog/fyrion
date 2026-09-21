"""
Audit logging cog.

Mirrors gateway events into an audit channel as embeds. The destination is
``guild_settings.audit_log_channel_id``, configured with ``/logs channel`` (or
``/set-logchannel``). It is independent of the moderation log
(``mod_log_channel_id``, set with ``/set-modlog``), so a server can route raw
events and moderator actions to different channels.

Limitations worth knowing:
- ``on_message_delete`` and ``on_message_edit`` only fire for messages that are
  still in discord.py's message cache. Older messages produce no event; that is
  a gateway limitation, not a bug.
- Events that happen inside the audit channel itself are ignored, so the log
  cannot feed on its own output.
- Message content is attacker controlled: every embed is sent with mentions
  disabled and user text is wrapped in a code fence.
- Writing to the log is best effort. A missing channel or permission is logged
  locally and never raises back into the event dispatcher.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("fyrion.cogs.logging")

NO_MENTIONS = discord.AllowedMentions.none()

# Discord allows 1024 characters per embed field value; stay well below it.
EXCERPT_LIMIT = 900
LIST_LIMIT = 1000

COLOR_CREATE = discord.Color.green()
COLOR_DELETE = discord.Color.red()
COLOR_UPDATE = discord.Color.blurple()
COLOR_MODERATION = discord.Color.orange()


def _truncate(text: str, limit: int = EXCERPT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _fence(text: str) -> str:
    """Wraps user supplied text so markdown and mentions stay inert."""
    # A zero-width space defuses code fences embedded in the content itself.
    # Escape *before* truncating: the replace expands each ``` to four chars,
    # so truncating first could push the fenced field past Discord's 1024-char
    # per-field limit and make the whole audit embed fail to send.
    safe = _truncate(text.replace("```", "`\u200b``"))
    return f"```\n{safe}\n```"


def _mention_of(target: Any) -> str:
    """Best effort mention for channels and roles, falling back to the id."""
    mention = getattr(target, "mention", None)
    if mention:
        return str(mention)
    return f"`{getattr(target, 'id', 'unknown')}`"


class AuditLog(commands.Cog):
    """Gateway event auditing."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        # guild_id -> channel id, or None when logging is switched off. Cached
        # because several of these listeners run on the message hot path.
        self._channels: dict[int, int | None] = {}

    # ------------------------------------------------------------------
    # Destination resolution
    # ------------------------------------------------------------------

    def invalidate(self, guild_id: int) -> None:
        """Called by the configuration commands after a channel change."""
        self._channels.pop(guild_id, None)

    async def _channel_id(self, guild_id: int) -> int | None:
        if guild_id in self._channels:
            return self._channels[guild_id]

        try:
            settings = await self.db.get_guild_settings(guild_id)
        except Exception:
            # A database hiccup must not break unrelated event handling.
            log.exception("Could not read the guild settings for %s", guild_id)
            return None

        raw = settings.get("audit_log_channel_id")
        channel_id = int(raw) if raw else None
        self._channels[guild_id] = channel_id
        return channel_id

    async def _destination(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = await self._channel_id(guild.id)
        if channel_id is None:
            return None

        channel = guild.get_channel(channel_id)
        me = guild.me
        if not isinstance(channel, discord.TextChannel) or me is None:
            return None

        permissions = channel.permissions_for(me)
        if not (permissions.send_messages and permissions.embed_links):
            return None
        return channel

    async def _in_log_channel(self, guild: discord.Guild, channel_id: int) -> bool:
        return await self._channel_id(guild.id) == channel_id

    async def _emit(self, guild: discord.Guild | None, embed: discord.Embed) -> None:
        if guild is None:
            return

        channel = await self._destination(guild)
        if channel is None:
            return

        try:
            await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("Failed to write the audit log in guild %s: %s", guild.id, exc)

    @staticmethod
    def _embed(
        title: str,
        color: discord.Color,
        subject: discord.abc.User | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title, color=color, timestamp=discord.utils.utcnow()
        )
        if subject is not None:
            embed.set_author(name=str(subject), icon_url=subject.display_avatar.url)
            embed.set_footer(text=f"ID: {subject.id}")
        return embed

    # ------------------------------------------------------------------
    # Message events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if await self._in_log_channel(guild, message.channel.id):
            return

        embed = self._embed("Message deleted", COLOR_DELETE, message.author)
        embed.add_field(
            name="Author",
            value=f"{message.author.mention} (`{message.author.id}`)",
            inline=False,
        )
        embed.add_field(
            name="Channel", value=_mention_of(message.channel), inline=False
        )
        if message.content:
            embed.add_field(name="Content", value=_fence(message.content), inline=False)
        if message.attachments:
            names = ", ".join(f"`{item.filename}`" for item in message.attachments)
            embed.add_field(
                name="Attachments", value=_truncate(names, LIST_LIMIT), inline=False
            )

        await self._emit(guild, embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: Sequence[discord.Message]) -> None:
        if not messages:
            return

        first = messages[0]
        guild = first.guild
        if guild is None:
            return
        if await self._in_log_channel(guild, first.channel.id):
            return

        # Individual contents are deliberately omitted: a purge of 100 messages
        # would otherwise flood the audit channel.
        embed = self._embed("Messages bulk deleted", COLOR_DELETE)
        embed.add_field(name="Channel", value=_mention_of(first.channel), inline=False)
        embed.add_field(name="Count", value=str(len(messages)), inline=False)
        await self._emit(guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        guild = after.guild
        if guild is None or after.author.bot:
            return
        if before.content == after.content:
            # Embed unfurling and pin changes also raise this event.
            return
        if await self._in_log_channel(guild, after.channel.id):
            return

        embed = self._embed("Message edited", COLOR_UPDATE, after.author)
        embed.add_field(
            name="Author",
            value=f"{after.author.mention} (`{after.author.id}`)",
            inline=False,
        )
        embed.add_field(name="Channel", value=_mention_of(after.channel), inline=False)
        embed.add_field(
            name="Before",
            value=_fence(before.content) if before.content else "empty",
            inline=False,
        )
        embed.add_field(
            name="After",
            value=_fence(after.content) if after.content else "empty",
            inline=False,
        )
        embed.add_field(
            name="Jump", value=f"[Open message]({after.jump_url})", inline=False
        )

        await self._emit(guild, embed)

    # ------------------------------------------------------------------
    # Member events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        embed = self._embed("Member joined", COLOR_CREATE, member)
        embed.add_field(
            name="Member", value=f"{member.mention} (`{member.id}`)", inline=False
        )
        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, style="F"),
            inline=False,
        )
        if member.guild.member_count is not None:
            embed.add_field(
                name="Member count", value=str(member.guild.member_count), inline=False
            )
        await self._emit(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        # This fires for kicks and bans as well; the moderation cog records the
        # moderator, this entry records the departure itself.
        embed = self._embed("Member left", COLOR_DELETE, member)
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        if member.joined_at is not None:
            embed.add_field(
                name="Joined",
                value=discord.utils.format_dt(member.joined_at, style="R"),
                inline=False,
            )

        roles = [role.mention for role in member.roles if not role.is_default()]
        if roles:
            embed.add_field(
                name="Roles",
                value=_truncate(", ".join(roles), LIST_LIMIT),
                inline=False,
            )

        await self._emit(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.nick != after.nick:
            embed = self._embed("Nickname changed", COLOR_UPDATE, after)
            embed.add_field(
                name="Member", value=f"{after.mention} (`{after.id}`)", inline=False
            )
            embed.add_field(
                name="Before",
                value=_fence(before.nick) if before.nick else "none",
                inline=False,
            )
            embed.add_field(
                name="After",
                value=_fence(after.nick) if after.nick else "none",
                inline=False,
            )
            await self._emit(after.guild, embed)

        added = [role for role in after.roles if role not in before.roles]
        removed = [role for role in before.roles if role not in after.roles]
        if added or removed:
            embed = self._embed("Roles updated", COLOR_UPDATE, after)
            embed.add_field(
                name="Member", value=f"{after.mention} (`{after.id}`)", inline=False
            )
            if added:
                embed.add_field(
                    name="Added",
                    value=_truncate(
                        ", ".join(role.mention for role in added), LIST_LIMIT
                    ),
                    inline=False,
                )
            if removed:
                embed.add_field(
                    name="Removed",
                    value=_truncate(
                        ", ".join(role.mention for role in removed), LIST_LIMIT
                    ),
                    inline=False,
                )
            await self._emit(after.guild, embed)

        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until is None:
                embed = self._embed("Timeout lifted", COLOR_MODERATION, after)
            else:
                embed = self._embed("Member timed out", COLOR_MODERATION, after)
                embed.add_field(
                    name="Expires",
                    value=discord.utils.format_dt(after.timed_out_until, style="F"),
                    inline=False,
                )
            embed.add_field(
                name="Member", value=f"{after.mention} (`{after.id}`)", inline=False
            )
            await self._emit(after.guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(
        self, guild: discord.Guild, user: discord.User | discord.Member
    ) -> None:
        embed = self._embed("Member banned", COLOR_MODERATION, user)
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
        await self._emit(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        embed = self._embed("Member unbanned", COLOR_CREATE, user)
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
        await self._emit(guild, embed)

    # ------------------------------------------------------------------
    # Channel and role events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        embed = self._embed("Channel created", COLOR_CREATE)
        embed.add_field(
            name="Channel",
            value=f"{_mention_of(channel)} (`{channel.id}`)",
            inline=False,
        )
        embed.add_field(name="Type", value=str(channel.type), inline=False)
        if channel.category is not None:
            embed.add_field(name="Category", value=channel.category.name, inline=False)
        await self._emit(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        was_log_channel = await self._in_log_channel(guild, channel.id)

        embed = self._embed("Channel deleted", COLOR_DELETE)
        embed.add_field(
            name="Channel", value=f"#{channel.name} (`{channel.id}`)", inline=False
        )
        embed.add_field(name="Type", value=str(channel.type), inline=False)
        await self._emit(guild, embed)

        if was_log_channel:
            # The destination is gone; drop the cache so a reconfiguration is
            # picked up on the next event.
            self.invalidate(guild.id)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        embed = self._embed("Role created", COLOR_CREATE)
        embed.add_field(
            name="Role", value=f"{role.mention} (`{role.id}`)", inline=False
        )
        await self._emit(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        embed = self._embed("Role deleted", COLOR_DELETE)
        embed.add_field(name="Role", value=f"@{role.name} (`{role.id}`)", inline=False)
        await self._emit(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.permissions != after.permissions:
            # Permission changes are security relevant, so record the delta.
            gained = [
                name
                for name, value in after.permissions
                if value and not getattr(before.permissions, name)
            ]
            lost = [
                name
                for name, value in before.permissions
                if value and not getattr(after.permissions, name)
            ]
            if gained:
                changes.append("Granted: " + ", ".join(f"`{name}`" for name in gained))
            if lost:
                changes.append("Revoked: " + ", ".join(f"`{name}`" for name in lost))

        if not changes:
            return

        embed = self._embed("Role updated", COLOR_UPDATE)
        embed.add_field(
            name="Role", value=f"{after.mention} (`{after.id}`)", inline=False
        )
        embed.add_field(
            name="Changes",
            value=_truncate("\n".join(changes), LIST_LIMIT),
            inline=False,
        )
        await self._emit(after.guild, embed)


class LoggingCommands(commands.GroupCog, name="logs"):
    """Audit log configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]

    def _invalidate(self, guild_id: int) -> None:
        """Clears the listener cache so a change applies immediately."""
        cog = self.bot.get_cog("AuditLog")
        if isinstance(cog, AuditLog):
            cog.invalidate(guild_id)

    @app_commands.command(
        name="channel", description="Set the channel that receives audit logs."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Where audit embeds are posted")
    async def channel_cmd(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        assert interaction.guild_id is not None

        guild = interaction.guild
        me = guild.me if guild is not None else None
        if me is None:
            await interaction.response.send_message(
                "\u274c This command can only be used in a server.", ephemeral=True
            )
            return

        permissions = channel.permissions_for(me)
        if not (permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                f"\u274c I need `Send Messages` and `Embed Links` in {channel.mention}.",
                ephemeral=True,
            )
            return

        await self.db.update_guild_settings(
            interaction.guild_id, audit_log_channel_id=channel.id
        )
        self._invalidate(interaction.guild_id)

        await interaction.response.send_message(
            f"\u2705 Audit logs will be sent to {channel.mention}. "
            "Moderation actions have their own channel; set it with `/set-modlog`.",
            ephemeral=True,
        )

    @app_commands.command(name="disable", description="Stop sending audit logs.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def disable_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None

        await self.db.update_guild_settings(
            interaction.guild_id, audit_log_channel_id=None
        )
        self._invalidate(interaction.guild_id)

        await interaction.response.send_message(
            "\u2705 Audit logging **disabled**. Moderation logging is unaffected; "
            "manage it with `/set-modlog`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="status", description="Show the current audit log channel."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        settings = await self.db.get_guild_settings(interaction.guild_id)
        channel_id = settings.get("audit_log_channel_id")

        if not channel_id:
            await interaction.response.send_message(
                "\u2139\ufe0f Audit logging is disabled. Use `/logs channel` to enable it.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        channel = guild.get_channel(int(channel_id)) if guild is not None else None
        me = guild.me if guild is not None else None

        embed = discord.Embed(title="Audit Logging", color=discord.Color.blurple())
        embed.add_field(
            name="Channel",
            value=(
                channel.mention if channel is not None else f"missing (`{channel_id}`)"
            ),
            inline=False,
        )

        if isinstance(channel, discord.TextChannel) and me is not None:
            permissions = channel.permissions_for(me)
            writable = permissions.send_messages and permissions.embed_links
            embed.add_field(
                name="Writable",
                value="yes" if writable else "no (missing Send Messages/Embed Links)",
                inline=False,
            )

        embed.set_footer(text="Deleted and edited messages require the message cache.")
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuditLog(bot))
    await bot.add_cog(LoggingCommands(bot))
