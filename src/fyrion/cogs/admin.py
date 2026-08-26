"""
Server administration cog.

Three groups of functionality live here:

* **Settings** (``/set-*``) write to ``guild_settings`` through the pooled data
  layer, which validates every column name against the schema allow-list and
  binds every value as a SQL parameter. Where a legacy consumer still reads the
  old ``guild_configs`` row (welcome channel, autorole, log channel), the write
  is mirrored there too, so existing listeners keep working.
* **Role and channel management** (``/role-*``, ``/channel-*``) which always
  runs through ``fyrion.utils.permissions`` before touching the API.
* **Content tooling**: the interactive embed builder and reaction roles,
  including the listener that actually applies them.

Authorization model
-------------------
``app_commands.default_permissions`` only controls whether Discord *shows* a
command to a member, so it is never the gate. Every command re-checks the
invoker's effective guild (and, where relevant, channel) permissions
server-side, and every role operation additionally enforces Discord's role
hierarchy for both the invoker and the bot. Roles granting ``Administrator`` can
only be handled by an administrator, so the bot cannot be used as a
privilege-escalation path.

Destructive actions (deleting a channel or role, editing every member) require
an explicit button confirmation and are mirrored into the guild's moderation log
channel.

User-controlled text (role names, topics, templates) is echoed back with mention
parsing disabled, so a crafted name can never make Fyrion ping a role or
``@everyone``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.database.repositories.guild_config import GuildConfigRepository
from fyrion.utils.colors import parse_color
from fyrion.utils.modlog import send_log
from fyrion.utils.permissions import (
    can_manage_member_roles,
    can_manage_role,
    missing_channel_permissions,
)
from fyrion.views.confirm import ConfirmView
from fyrion.views.embeds import EmbedBuilderModal

log = logging.getLogger("fyrion.cogs.admin")

NO_MENTIONS = discord.AllowedMentions.none()

# Discord truncates audit log reasons at 512 characters.
AUDIT_REASON_LIMIT = 512
# Embed field values are capped at 1024 characters.
FIELD_LIMIT = 1024

MAX_PREFIX_LENGTH = 8
MAX_TEMPLATE_LENGTH = 2000
MAX_ROLE_NAME = 100
MAX_CHANNEL_NAME = 100
MAX_TOPIC = 1024
MAX_SLOWMODE = 21600
MAX_UNICODE_EMOJI_LENGTH = 16
MAX_GROUP_KEY = 32

# ``/role-all`` edits one member per API call. The cap keeps a single invocation
# inside the interaction token's lifetime and bounds the rate-limit burst; the
# reply tells the operator to run it again when more members remain.
MAX_ROLE_ALL_TARGETS = 500
# Upper bound on how many members are examined when the member cache is cold.
MAX_MEMBER_SCAN = 5000
# How often the in-progress reply is refreshed during a bulk role edit.
PROGRESS_EVERY = 100

MESSAGE_LINK_RE = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d{15,25})/(?P<channel>\d{15,25})/(?P<message>\d{15,25})"
)

GROUP_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")

ReactionMode = Literal["toggle", "add_only", "remove_only", "unique"]
ChannelKind = Literal["text", "voice", "category", "stage"]
RoleAction = Literal["add", "remove"]
RoleScope = Literal["humans", "bots", "everyone"]


def emoji_storage_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    """Returns the canonical database key for an emoji.

    Unicode emoji are stored verbatim; custom emoji as ``name:id``. The id is
    what actually identifies a custom emoji, so lookups fall back to matching on
    the id alone when an emoji has been renamed since the mapping was created.
    """
    if isinstance(emoji, str):
        return emoji
    if emoji.id is None:
        return emoji.name or ""
    return f"{emoji.name}:{emoji.id}"


def parse_emoji_input(
    bot: commands.Bot, raw: str
) -> tuple[discord.PartialEmoji | str, str]:
    """Parses operator input into ``(reaction_argument, storage_key)``.

    Raises:
        ValueError: when the value is not a usable emoji.
    """
    text = raw.strip()
    if not text:
        raise ValueError("No emoji was supplied.")

    partial = discord.PartialEmoji.from_str(text)

    if partial.id is None:
        candidate = partial.name or text
        # A standard emoji is never plain ASCII, and never long. Rejecting both
        # keeps arbitrary text out of the reaction API.
        if candidate.isascii() or len(candidate) > MAX_UNICODE_EMOJI_LENGTH:
            raise ValueError(
                "That is not an emoji. Provide a single standard emoji, or a "
                "custom emoji from a server I am also in."
            )
        return candidate, candidate

    if bot.get_emoji(partial.id) is None:
        raise ValueError(
            "I cannot use that custom emoji. I must be a member of the server "
            "it belongs to."
        )
    return partial, emoji_storage_key(partial)


def _truncate(text: str, limit: int = FIELD_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


class Admin(commands.Cog):
    """Server settings, role and channel management, embeds and reaction roles."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.legacy = GuildConfigRepository(self.db)

    # ------------------------------------------------------------------
    # Shared helpers
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

    async def _ok(self, interaction: discord.Interaction, message: str) -> None:
        await self._respond(interaction, f"\u2705 {message}")

    async def _authorize(
        self, interaction: discord.Interaction, *permissions: str
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Re-checks the invoker's permissions server-side.

        Returns ``(guild, member)`` when the command may proceed, otherwise
        replies with the refusal and returns ``None``.
        """
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

    @staticmethod
    def _audit_reason(actor: discord.abc.User, description: str) -> str:
        return f"{actor} ({actor.id}): {description}"[:AUDIT_REASON_LIMIT]

    async def _audit(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        action: str,
        detail: str,
    ) -> None:
        """Mirrors an administrative change into the guild's log channel.

        Best effort: a missing channel or permission never fails the command
        that has already succeeded.
        """
        embed = discord.Embed(
            title=f"Administration: {action}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Performed by", value=f"{actor} (`{actor.id}`)", inline=False
        )
        embed.add_field(name="Details", value=_truncate(detail), inline=False)
        await send_log(self.db, guild, embed)

    @staticmethod
    def _writable(
        guild: discord.Guild, channel: discord.abc.GuildChannel
    ) -> str | None:
        """Returns an error when Fyrion cannot post embeds in ``channel``."""
        me = guild.me
        if me is None:
            return "I could not resolve my own membership in this server."
        missing = missing_channel_permissions(
            me,
            channel,
            view_channel=True,
            send_messages=True,
            embed_links=True,
        )
        if missing:
            names = ", ".join(f"`{name}`" for name in missing)
            return f"I am missing {names} in {channel.mention}."
        return None

    async def _mirror_legacy(self, guild_id: int, key: str, value: Any) -> None:
        """Writes a setting to the legacy ``guild_configs`` row as well.

        The welcome/autorole listeners and the ``/logs`` commands still read
        that table, so mirroring keeps both views of the configuration in sync.
        """
        try:
            await self.legacy.update_config(guild_id, key, value)
        except Exception:
            log.exception(
                "Could not mirror %s into guild_configs for guild %s.", key, guild_id
            )

    def _invalidate_audit_cache(self, guild_id: int) -> None:
        """Drops the audit-log cog's cached destination for a guild."""
        cog = self.bot.get_cog("AuditLog")
        invalidate = getattr(cog, "invalidate", None)
        if callable(invalidate):
            invalidate(guild_id)

    async def _confirm(
        self,
        interaction: discord.Interaction,
        prompt: str,
        *,
        confirm_label: str = "Confirm",
    ) -> bool:
        """Shows a confirmation prompt and returns whether it was accepted."""
        view = ConfirmView(interaction.user.id, confirm_label=confirm_label)
        await interaction.response.send_message(
            prompt, view=view, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

        timed_out = await view.wait()
        if not view.value:
            reason = (
                "\u23f1\ufe0f Timed out; nothing was changed."
                if timed_out
                else "\u2139\ufe0f Cancelled; nothing was changed."
            )
            try:
                await interaction.edit_original_response(content=reason, view=None)
            except discord.HTTPException:
                pass
            return False
        return True

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @app_commands.command(
        name="set-prefix",
        description="Set the text prefix used by this server's custom commands.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(prefix="1-8 characters, no spaces (for example ! or ?)")
    async def set_prefix_cmd(
        self,
        interaction: discord.Interaction,
        prefix: app_commands.Range[str, 1, MAX_PREFIX_LENGTH],
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        candidate = prefix.strip()
        if not candidate:
            await self._reject(interaction, "The prefix cannot be blank.")
            return
        if any(char.isspace() for char in candidate):
            await self._reject(interaction, "The prefix cannot contain spaces.")
            return
        if candidate.startswith("<") or "@" in candidate or "#" in candidate:
            await self._reject(
                interaction,
                "The prefix cannot contain `@`, `#` or `<`, because Discord "
                "would read it as a mention.",
            )
            return

        await self.db.update_guild_settings(guild.id, prefix=candidate)
        await self._audit(guild, member, "Prefix changed", f"New prefix: `{candidate}`")
        await self._ok(
            interaction,
            f"Prefix set to `{candidate}`. Fyrion's own commands are slash "
            "commands; this prefix is used by custom commands.",
        )

    @app_commands.command(
        name="set-logchannel",
        description="Set the channel that receives audit log events.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Where audit events are posted (leave empty to disable)"
    )
    async def set_logchannel_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        if channel is None:
            await self.db.update_guild_settings(guild.id, audit_log_channel_id=None)
            await self._mirror_legacy(guild.id, "log_channel_id", None)
            self._invalidate_audit_cache(guild.id)
            await self._audit(guild, member, "Audit log disabled", "Channel cleared")
            await self._ok(interaction, "Audit logging is now **disabled**.")
            return

        if error := self._writable(guild, channel):
            await self._reject(interaction, error)
            return

        await self.db.update_guild_settings(
            guild.id, audit_log_channel_id=channel.id
        )
        await self._mirror_legacy(guild.id, "log_channel_id", channel.id)
        self._invalidate_audit_cache(guild.id)

        await self._audit(
            guild, member, "Audit log channel set", f"Channel: #{channel.name}"
        )
        await self._ok(
            interaction,
            f"Audit events will be sent to {channel.mention}. Moderation actions "
            "go there too unless `/set-modlog` points somewhere else.",
        )

    @app_commands.command(
        name="set-modlog",
        description="Set the channel that receives moderation and AutoMod actions.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Where moderation actions are posted (leave empty to disable)"
    )
    async def set_modlog_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        if channel is None:
            await self.db.update_guild_settings(guild.id, mod_log_channel_id=None)
            await self._audit(
                guild, member, "Moderation log cleared", "Falling back to the audit log"
            )
            await self._ok(
                interaction,
                "Moderation logging now falls back to the audit log channel, if "
                "one is configured.",
            )
            return

        if error := self._writable(guild, channel):
            await self._reject(interaction, error)
            return

        await self.db.update_guild_settings(guild.id, mod_log_channel_id=channel.id)
        await self._audit(
            guild, member, "Moderation log channel set", f"Channel: #{channel.name}"
        )
        await self._ok(
            interaction,
            f"Moderation actions and AutoMod enforcement will be logged in "
            f"{channel.mention}.",
        )

    @app_commands.command(
        name="set-welcome",
        description="Configure the welcome channel and message for new members.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Where welcome messages are posted (leave empty to disable)",
        message=(
            "Optional template. Placeholders: {user}, {user_mention}, "
            "{server}, {member_count}"
        ),
    )
    async def set_welcome_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        message: Optional[app_commands.Range[str, 1, MAX_TEMPLATE_LENGTH]] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        if channel is None:
            await self.db.update_guild_settings(guild.id, welcome_channel_id=None)
            await self._mirror_legacy(guild.id, "welcome_channel_id", None)
            await self._audit(guild, member, "Welcome disabled", "Channel cleared")
            await self._ok(interaction, "Welcome messages are now **disabled**.")
            return

        if error := self._writable(guild, channel):
            await self._reject(interaction, error)
            return

        values: dict[str, Any] = {"welcome_channel_id": channel.id}
        if message is not None:
            values["welcome_message"] = message.strip()

        await self.db.update_guild_settings(guild.id, **values)
        await self._mirror_legacy(guild.id, "welcome_channel_id", channel.id)

        await self._audit(
            guild,
            member,
            "Welcome channel set",
            f"Channel: #{channel.name}"
            + ("\nCustom template updated." if message is not None else ""),
        )

        note = (
            "\nYour template will be used verbatim."
            if message is not None
            else "\nNo template is set, so the default welcome embed is used. "
            "Pass `message` to customise it."
        )
        await self._ok(
            interaction, f"New members will be greeted in {channel.mention}.{note}"
        )

    @app_commands.command(
        name="set-leave",
        description="Configure the channel and message used when a member leaves.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Where leave messages are posted (leave empty to disable)",
        message=(
            "Optional template. Placeholders: {user}, {user_name}, "
            "{server}, {member_count}"
        ),
    )
    async def set_leave_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        message: Optional[app_commands.Range[str, 1, MAX_TEMPLATE_LENGTH]] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild")
        if context is None:
            return
        guild, member = context

        if channel is None:
            await self.db.update_guild_settings(guild.id, goodbye_channel_id=None)
            await self._audit(guild, member, "Leave messages disabled", "Channel cleared")
            await self._ok(interaction, "Leave messages are now **disabled**.")
            return

        if error := self._writable(guild, channel):
            await self._reject(interaction, error)
            return

        values: dict[str, Any] = {"goodbye_channel_id": channel.id}
        if message is not None:
            values["goodbye_message"] = message.strip()

        await self.db.update_guild_settings(guild.id, **values)
        await self._audit(
            guild,
            member,
            "Leave channel set",
            f"Channel: #{channel.name}"
            + ("\nCustom template updated." if message is not None else ""),
        )

        note = (
            "\nYour template will be used verbatim."
            if message is not None
            else "\nNo template is set, so the default leave embed is used."
        )
        await self._ok(
            interaction,
            f"Departures will be announced in {channel.mention}.{note}",
        )

    @app_commands.command(
        name="set-autorole",
        description="Set the role automatically given to new members.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="The role to grant on join (leave empty to disable)")
    async def set_autorole_cmd(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild", "manage_roles")
        if context is None:
            return
        guild, member = context

        if role is None:
            await self.db.update_guild_settings(guild.id, autorole_id=None)
            await self._mirror_legacy(guild.id, "autorole_id", None)
            await self._audit(guild, member, "Autorole disabled", "Role cleared")
            await self._ok(interaction, "Autorole is now **disabled**.")
            return

        # A role Fyrion cannot assign would fail silently on every join, so it
        # is refused at configuration time instead.
        if error := can_manage_role(member, role):
            await self._reject(interaction, error)
            return

        await self.db.update_guild_settings(guild.id, autorole_id=role.id)
        await self._mirror_legacy(guild.id, "autorole_id", role.id)

        await self._audit(
            guild, member, "Autorole set", f"Role: {role.name} (`{role.id}`)"
        )
        await self._ok(
            interaction, f"New members will automatically receive {role.mention}."
        )

    @app_commands.command(
        name="set-muterole",
        description="Set the mute role and optionally apply its channel denials.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        role="The mute role (leave empty to disable)",
        sync_permissions="Deny messaging and speaking for this role in every channel",
    )
    async def set_muterole_cmd(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        sync_permissions: bool = True,
    ) -> None:
        context = await self._authorize(interaction, "manage_guild", "manage_roles")
        if context is None:
            return
        guild, member = context

        if role is None:
            await self.db.update_guild_settings(guild.id, mute_role_id=None)
            await self._audit(guild, member, "Mute role cleared", "Role cleared")
            await self._ok(interaction, "The mute role has been **cleared**.")
            return

        if error := can_manage_role(member, role):
            await self._reject(interaction, error)
            return

        await interaction.response.defer(ephemeral=True)
        await self.db.update_guild_settings(guild.id, mute_role_id=role.id)

        applied = 0
        failed = 0
        if sync_permissions:
            applied, failed = await self._apply_mute_overwrites(guild, role, member)

        detail = f"Role: {role.name} (`{role.id}`)"
        if sync_permissions:
            detail += f"\nOverwrites applied to {applied} channel(s); {failed} failed."
        await self._audit(guild, member, "Mute role set", detail)

        lines = [f"\u2705 Mute role set to {role.mention}."]
        if sync_permissions:
            lines.append(
                f"Denied messaging and speaking in {applied} channel(s)"
                + (f"; {failed} could not be updated." if failed else ".")
            )
        else:
            lines.append(
                "Channel overwrites were left untouched, so the role only "
                "denies what you have configured manually."
            )
        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def _apply_mute_overwrites(
        self, guild: discord.Guild, role: discord.Role, actor: discord.abc.User
    ) -> tuple[int, int]:
        """Denies speech permissions for ``role`` across the guild.

        Only categories and channels that do *not* inherit from their category
        are edited: synced children pick the denial up automatically, which
        keeps the number of API calls proportional to the guild's real
        permission structure rather than to its channel count.
        """
        overwrite = discord.PermissionOverwrite(
            send_messages=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
            add_reactions=False,
            speak=False,
            request_to_speak=False,
        )
        reason = self._audit_reason(actor, "Mute role permission sync")

        targets: list[discord.abc.GuildChannel] = list(guild.categories)
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            if channel.category is not None and channel.permissions_synced:
                continue
            targets.append(channel)

        applied = 0
        failed = 0
        for channel in targets:
            try:
                await channel.set_permissions(role, overwrite=overwrite, reason=reason)
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException as exc:
                failed += 1
                log.warning(
                    "Could not apply mute overwrites in channel %s: %s",
                    channel.id,
                    exc,
                )
            else:
                applied += 1

        return applied, failed

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------

    @app_commands.command(name="role-add", description="Give a role to a member.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        member="The member to grant the role to",
        role="The role to grant",
        reason="Reason recorded in the audit log",
    )
    async def role_add_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        reason: str = "No reason provided",
    ) -> None:
        await self._edit_member_role(
            interaction, member, role, reason=reason, grant=True
        )

    @app_commands.command(
        name="role-remove", description="Remove a role from a member."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        member="The member to take the role from",
        role="The role to remove",
        reason="Reason recorded in the audit log",
    )
    async def role_remove_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        reason: str = "No reason provided",
    ) -> None:
        await self._edit_member_role(
            interaction, member, role, reason=reason, grant=False
        )

    async def _edit_member_role(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        role: discord.Role,
        *,
        reason: str,
        grant: bool,
    ) -> None:
        context = await self._authorize(interaction, "manage_roles")
        if context is None:
            return
        guild, invoker = context

        if target.guild.id != guild.id or role.guild.id != guild.id:
            await self._reject(
                interaction, "That member or role does not belong to this server."
            )
            return

        if error := can_manage_role(invoker, role):
            await self._reject(interaction, error)
            return
        if error := can_manage_member_roles(invoker, target):
            await self._reject(interaction, error)
            return

        has_role = any(existing.id == role.id for existing in target.roles)
        if grant and has_role:
            await self._respond(
                interaction,
                f"\u2139\ufe0f **{target}** already has {role.mention}.",
            )
            return
        if not grant and not has_role:
            await self._respond(
                interaction,
                f"\u2139\ufe0f **{target}** does not have {role.mention}.",
            )
            return

        audit = self._audit_reason(invoker, reason)
        try:
            if grant:
                await target.add_roles(role, reason=audit)
            else:
                await target.remove_roles(role, reason=audit)
        except discord.Forbidden:
            await self._reject(
                interaction,
                "Discord refused the change. Check my `Manage Roles` permission "
                "and my position in the role hierarchy.",
            )
            return
        except discord.HTTPException as exc:
            log.warning(
                "Role edit failed for member %s in guild %s: %s",
                target.id,
                guild.id,
                exc,
            )
            await self._reject(
                interaction, f"Discord rejected the change (HTTP {exc.status})."
            )
            return

        verb = "Granted" if grant else "Removed"
        await self._audit(
            guild,
            invoker,
            f"Role {verb.lower()}",
            f"Member: {target} (`{target.id}`)\nRole: {role.name} (`{role.id}`)\n"
            f"Reason: {reason}",
        )
        preposition = "to" if grant else "from"
        await self._ok(
            interaction,
            f"{verb} {role.mention} {preposition} **{target}**. Reason: {reason}",
        )

    @app_commands.command(
        name="role-all",
        description="Add or remove a role for every member in the server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        role="The role to add or remove",
        action="Whether to add or remove the role",
        scope="Which members to include",
        reason="Reason recorded in the audit log",
    )
    async def role_all_cmd(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        action: RoleAction,
        scope: RoleScope = "humans",
        reason: str = "Bulk role update",
    ) -> None:
        context = await self._authorize(interaction, "manage_roles")
        if context is None:
            return
        guild, invoker = context

        if role.guild.id != guild.id:
            await self._reject(interaction, "That role does not belong to this server.")
            return
        if error := can_manage_role(invoker, role):
            await self._reject(interaction, error)
            return

        grant = action == "add"
        prompt = (
            f"\u26a0\ufe0f This will **{action}** {role.mention} for every matching "
            f"member (`{scope}`) in **{guild.name}**.\n"
            f"At most {MAX_ROLE_ALL_TARGETS} members are processed per run, and "
            "the change cannot be undone automatically.\nContinue?"
        )
        if not await self._confirm(interaction, prompt, confirm_label=f"Yes, {action}"):
            return

        await interaction.edit_original_response(
            content="\u23f3 Collecting members\u2026", view=None
        )

        members = await self._all_members(guild)
        me = guild.me
        assert me is not None  # guaranteed by _authorize

        targets: list[discord.Member] = []
        skipped_hierarchy = 0
        for candidate in members:
            if scope == "humans" and candidate.bot:
                continue
            if scope == "bots" and not candidate.bot:
                continue
            has_role = any(existing.id == role.id for existing in candidate.roles)
            if grant == has_role:
                continue
            if me.top_role <= candidate.top_role or candidate.id == guild.owner_id:
                # Discord would refuse these edits; counting them is more useful
                # than firing doomed requests.
                skipped_hierarchy += 1
                continue
            targets.append(candidate)

        if not targets:
            await interaction.edit_original_response(
                content=(
                    "\u2139\ufe0f No members needed that change"
                    + (
                        f"; {skipped_hierarchy} were above me in the hierarchy."
                        if skipped_hierarchy
                        else "."
                    )
                ),
                view=None,
            )
            return

        remaining = max(0, len(targets) - MAX_ROLE_ALL_TARGETS)
        batch = targets[:MAX_ROLE_ALL_TARGETS]

        audit = self._audit_reason(invoker, reason)
        changed = 0
        failed = 0

        for index, candidate in enumerate(batch, start=1):
            try:
                if grant:
                    await candidate.add_roles(role, reason=audit)
                else:
                    await candidate.remove_roles(role, reason=audit)
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException as exc:
                failed += 1
                log.warning(
                    "Bulk role edit failed for member %s in guild %s: %s",
                    candidate.id,
                    guild.id,
                    exc,
                )
            else:
                changed += 1

            if index % PROGRESS_EVERY == 0:
                try:
                    await interaction.edit_original_response(
                        content=(
                            f"\u23f3 Processed {index}/{len(batch)} member(s)\u2026"
                        ),
                        view=None,
                    )
                except discord.HTTPException:
                    pass

        summary = [
            f"\u2705 {'Granted' if grant else 'Removed'} {role.mention} "
            f"{'to' if grant else 'from'} **{changed}** member(s)."
        ]
        if failed:
            summary.append(f"{failed} member(s) could not be updated.")
        if skipped_hierarchy:
            summary.append(
                f"{skipped_hierarchy} member(s) were skipped because they rank "
                "at or above me."
            )
        if remaining:
            summary.append(
                f"{remaining} member(s) still need this change \u2014 run the "
                "command again to continue."
            )

        await interaction.edit_original_response(
            content="\n".join(summary), view=None
        )
        await self._audit(
            guild,
            invoker,
            "Bulk role update",
            f"Role: {role.name} (`{role.id}`)\nAction: {action}\nScope: {scope}\n"
            f"Changed: {changed}, failed: {failed}, skipped: {skipped_hierarchy}\n"
            f"Reason: {reason}",
        )

    async def _all_members(self, guild: discord.Guild) -> Sequence[discord.Member]:
        """Returns the guild's members, filling the cache when necessary.

        ``chunk_guilds_at_startup`` is disabled, so the member cache is normally
        cold. ``Guild.chunk`` fills it over the gateway; a REST fallback keeps
        the command usable if chunking is unavailable.
        """
        if guild.chunked:
            return list(guild.members)

        try:
            return await guild.chunk(cache=True)
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            log.info(
                "Gateway chunking unavailable for guild %s (%s); falling back to "
                "the REST member list.",
                guild.id,
                exc,
            )

        collected: list[discord.Member] = []
        try:
            async for member in guild.fetch_members(limit=MAX_MEMBER_SCAN):
                collected.append(member)
        except discord.HTTPException as exc:
            log.warning("Could not list members for guild %s: %s", guild.id, exc)
        return collected

    @app_commands.command(
        name="role-create", description="Create a new role with no permissions."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        name="Name of the new role",
        color="Hex value such as #5865F2, a named color, or random",
        hoist="Show members with this role separately in the member list",
        mentionable="Allow anyone to mention this role",
        reason="Reason recorded in the audit log",
    )
    async def role_create_cmd(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_ROLE_NAME],
        color: Optional[str] = None,
        hoist: bool = False,
        mentionable: bool = False,
        reason: str = "Created via Fyrion",
    ) -> None:
        context = await self._authorize(interaction, "manage_roles")
        if context is None:
            return
        guild, invoker = context

        me = guild.me
        assert me is not None
        if not me.guild_permissions.manage_roles:
            await self._reject(interaction, "I need the `Manage Roles` permission.")
            return

        cleaned = name.strip()
        if not cleaned:
            await self._reject(interaction, "The role name cannot be blank.")
            return
        if cleaned.lower() == "everyone":
            await self._reject(interaction, "That name is reserved by Discord.")
            return

        try:
            parsed_color = parse_color(color)
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return

        try:
            role = await guild.create_role(
                name=cleaned,
                colour=parsed_color if parsed_color is not None else discord.Color.default(),
                hoist=hoist,
                mentionable=mentionable,
                # Deliberately empty: a role created by a bot command must never
                # arrive with permissions attached.
                permissions=discord.Permissions.none(),
                reason=self._audit_reason(invoker, reason),
            )
        except discord.Forbidden:
            await self._reject(
                interaction, "Discord refused to create the role."
            )
            return
        except discord.HTTPException as exc:
            log.warning("Role creation failed in guild %s: %s", guild.id, exc)
            await self._reject(
                interaction,
                f"Discord rejected the role (HTTP {exc.status}). Servers are "
                "limited to 250 roles.",
            )
            return

        await self._audit(
            guild,
            invoker,
            "Role created",
            f"Role: {role.name} (`{role.id}`)\nReason: {reason}",
        )
        await self._ok(
            interaction,
            f"Created {role.mention} with **no permissions**. It sits at the "
            "bottom of the hierarchy \u2014 move it and grant permissions in "
            "Server Settings.",
        )

    @app_commands.command(name="role-delete", description="Delete a role.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        role="The role to delete", reason="Reason recorded in the audit log"
    )
    async def role_delete_cmd(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        reason: str = "Deleted via Fyrion",
    ) -> None:
        context = await self._authorize(interaction, "manage_roles")
        if context is None:
            return
        guild, invoker = context

        if role.guild.id != guild.id:
            await self._reject(interaction, "That role does not belong to this server.")
            return
        if error := can_manage_role(invoker, role):
            await self._reject(interaction, error)
            return

        prompt = (
            f"\u26a0\ufe0f Deleting **{role.name}** removes it from "
            f"{len(role.members)} member(s) and cannot be undone. Continue?"
        )
        if not await self._confirm(interaction, prompt, confirm_label="Delete role"):
            return

        name = role.name
        role_id = role.id
        member_count = len(role.members)

        try:
            await role.delete(reason=self._audit_reason(invoker, reason))
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="\u274c Discord refused to delete that role.", view=None
            )
            return
        except discord.HTTPException as exc:
            log.warning("Role deletion failed in guild %s: %s", guild.id, exc)
            await interaction.edit_original_response(
                content=f"\u274c Discord rejected the deletion (HTTP {exc.status}).",
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=(
                f"\u2705 Deleted **{name}**, which was held by "
                f"{member_count} member(s)."
            ),
            view=None,
        )
        await self._audit(
            guild,
            invoker,
            "Role deleted",
            f"Role: {name} (`{role_id}`)\nMembers affected: {member_count}\n"
            f"Reason: {reason}",
        )

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    @app_commands.command(name="channel-create", description="Create a channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        name="Name of the new channel",
        channel_type="What kind of channel to create",
        category="Category to create the channel in",
        topic="Channel topic (text channels only)",
        nsfw="Mark the channel as age restricted (text channels only)",
        slowmode="Slowmode delay in seconds (text channels only)",
        reason="Reason recorded in the audit log",
    )
    async def channel_create_cmd(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_CHANNEL_NAME],
        channel_type: ChannelKind = "text",
        category: Optional[discord.CategoryChannel] = None,
        topic: Optional[app_commands.Range[str, 1, MAX_TOPIC]] = None,
        nsfw: bool = False,
        slowmode: Optional[app_commands.Range[int, 0, MAX_SLOWMODE]] = None,
        reason: str = "Created via Fyrion",
    ) -> None:
        context = await self._authorize(interaction, "manage_channels")
        if context is None:
            return
        guild, invoker = context

        me = guild.me
        assert me is not None
        if not me.guild_permissions.manage_channels:
            await self._reject(interaction, "I need the `Manage Channels` permission.")
            return

        if category is not None:
            if category.guild.id != guild.id:
                await self._reject(
                    interaction, "That category does not belong to this server."
                )
                return
            missing = missing_channel_permissions(
                me, category, view_channel=True, manage_channels=True
            )
            if missing:
                names = ", ".join(f"`{item}`" for item in missing)
                await self._reject(
                    interaction, f"I am missing {names} in {category.mention}."
                )
                return

        cleaned = name.strip()
        if not cleaned:
            await self._reject(interaction, "The channel name cannot be blank.")
            return

        audit = self._audit_reason(invoker, reason)
        created: discord.abc.GuildChannel
        try:
            if channel_type == "text":
                created = await guild.create_text_channel(
                    name=cleaned,
                    category=category,
                    topic=topic,
                    nsfw=nsfw,
                    slowmode_delay=slowmode or 0,
                    reason=audit,
                )
            elif channel_type == "voice":
                created = await guild.create_voice_channel(
                    name=cleaned, category=category, reason=audit
                )
            elif channel_type == "stage":
                created = await guild.create_stage_channel(
                    name=cleaned, category=category, reason=audit
                )
            else:
                created = await guild.create_category(name=cleaned, reason=audit)
        except discord.Forbidden:
            await self._reject(interaction, "Discord refused to create the channel.")
            return
        except discord.HTTPException as exc:
            log.warning("Channel creation failed in guild %s: %s", guild.id, exc)
            await self._reject(
                interaction,
                f"Discord rejected the channel (HTTP {exc.status}). Check the "
                "name and the server's channel limit.",
            )
            return

        await self._audit(
            guild,
            invoker,
            "Channel created",
            f"Channel: #{created.name} (`{created.id}`)\nType: {channel_type}\n"
            f"Reason: {reason}",
        )

        note = ""
        if channel_type in {"voice", "stage", "category"} and (
            topic is not None or slowmode is not None or nsfw
        ):
            note = (
                "\n\u2139\ufe0f Topic, slowmode and the age restriction only apply "
                "to text channels and were ignored."
            )

        await self._ok(
            interaction,
            f"Created {created.mention} as a {channel_type} channel.{note}",
        )

    @app_commands.command(name="channel-delete", description="Delete a channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        channel="The channel to delete",
        reason="Reason recorded in the audit log",
    )
    async def channel_delete_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        reason: str = "Deleted via Fyrion",
    ) -> None:
        context = await self._authorize(interaction, "manage_channels")
        if context is None:
            return
        guild, invoker = context

        if getattr(channel, "guild", None) is None or channel.guild.id != guild.id:
            await self._reject(
                interaction, "That channel does not belong to this server."
            )
            return

        me = guild.me
        assert me is not None
        missing = missing_channel_permissions(
            me, channel, view_channel=True, manage_channels=True
        )
        if missing:
            names = ", ".join(f"`{item}`" for item in missing)
            await self._reject(interaction, f"I am missing {names} in that channel.")
            return

        warnings: list[str] = []
        if isinstance(channel, discord.CategoryChannel) and channel.channels:
            warnings.append(
                f"This category still contains {len(channel.channels)} channel(s), "
                "which will be moved out of it, not deleted."
            )

        settings: dict[str, Any] = {}
        try:
            settings = await self.db.get_guild_settings(guild.id)
        except Exception:
            log.exception("Could not read guild settings for %s.", guild.id)

        configured = {
            key
            for key, value in settings.items()
            if key.endswith("_channel_id") and value and int(value) == channel.id
        }
        if configured:
            warnings.append(
                "This channel is currently configured for: "
                + ", ".join(f"`{key}`" for key in sorted(configured))
                + ". Those features will stop working until you reconfigure them."
            )

        prompt = (
            f"\u26a0\ufe0f Deleting **#{channel.name}** is permanent and removes "
            "its message history."
        )
        if warnings:
            prompt += "\n" + "\n".join(f"\u2022 {item}" for item in warnings)
        prompt += "\nContinue?"

        if not await self._confirm(interaction, prompt, confirm_label="Delete channel"):
            return

        name = channel.name
        channel_id = channel.id

        try:
            await channel.delete(reason=self._audit_reason(invoker, reason))
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="\u274c Discord refused to delete that channel.", view=None
            )
            return
        except discord.HTTPException as exc:
            log.warning("Channel deletion failed in guild %s: %s", guild.id, exc)
            await interaction.edit_original_response(
                content=f"\u274c Discord rejected the deletion (HTTP {exc.status}).",
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"\u2705 Deleted **#{name}**.", view=None
        )
        await self._audit(
            guild,
            invoker,
            "Channel deleted",
            f"Channel: #{name} (`{channel_id}`)\nReason: {reason}",
        )

    # ------------------------------------------------------------------
    # Embed builder
    # ------------------------------------------------------------------

    @app_commands.command(
        name="embed-builder",
        description="Build and post a rich embed using an interactive form.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(channel="Where to post the embed (defaults to this channel)")
    async def embed_builder_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_messages")
        if context is None:
            return
        guild, invoker = context

        target = channel if channel is not None else interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await self._reject(
                interaction,
                "Embeds can only be posted to text channels and threads.",
            )
            return
        if getattr(target, "guild", None) is None or target.guild.id != guild.id:
            await self._reject(
                interaction, "That channel does not belong to this server."
            )
            return

        # The invoker must be able to post there themselves; the bot's own
        # permission is not a substitute for theirs.
        invoker_missing = missing_channel_permissions(
            invoker, target, view_channel=True, send_messages=True
        )
        if invoker_missing:
            names = ", ".join(f"`{item}`" for item in invoker_missing)
            await self._reject(
                interaction, f"You are missing {names} in {target.mention}."
            )
            return

        if error := self._writable(guild, target):
            await self._reject(interaction, error)
            return

        await interaction.response.send_modal(EmbedBuilderModal(target))

    # ------------------------------------------------------------------
    # Reaction roles
    # ------------------------------------------------------------------

    async def _resolve_message(
        self, guild: discord.Guild, reference: str
    ) -> tuple[discord.Message | None, str | None]:
        """Resolves a message link into a fetched message."""
        match = MESSAGE_LINK_RE.search(reference.strip())
        if match is None:
            return None, (
                "That is not a message link. Right-click a message and choose "
                "**Copy Message Link**."
            )

        if int(match.group("guild")) != guild.id:
            return None, "That message belongs to a different server."

        channel = guild.get_channel_or_thread(int(match.group("channel")))
        if channel is None:
            return None, "I cannot see the channel that message is in."
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None, "Reaction roles only work on text channels and threads."

        me = guild.me
        if me is None:
            return None, "I could not resolve my own membership in this server."

        missing = missing_channel_permissions(
            me,
            channel,
            view_channel=True,
            read_message_history=True,
            add_reactions=True,
        )
        if missing:
            names = ", ".join(f"`{item}`" for item in missing)
            return None, f"I am missing {names} in {channel.mention}."

        try:
            message = await channel.fetch_message(int(match.group("message")))
        except discord.NotFound:
            return None, "That message no longer exists."
        except discord.Forbidden:
            return None, "Discord refused to let me read that message."
        except discord.HTTPException as exc:
            log.warning("Could not fetch a reaction-role message: %s", exc)
            return None, f"Discord rejected the request (HTTP {exc.status})."

        return message, None

    @app_commands.command(
        name="reactionrole-add",
        description="Map a reaction on a message to a role.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        message_link="Link to the message members will react to",
        emoji="The emoji to react with",
        role="The role granted for that reaction",
        mode="toggle, add_only, remove_only or unique (one per group)",
        group="Group name for unique mode (letters, digits, - and _)",
        required_role="Only members with this role may use the reaction",
    )
    async def reactionrole_add_cmd(
        self,
        interaction: discord.Interaction,
        message_link: str,
        emoji: str,
        role: discord.Role,
        mode: ReactionMode = "toggle",
        group: Optional[app_commands.Range[str, 1, MAX_GROUP_KEY]] = None,
        required_role: Optional[discord.Role] = None,
    ) -> None:
        context = await self._authorize(interaction, "manage_roles")
        if context is None:
            return
        guild, invoker = context

        if role.guild.id != guild.id:
            await self._reject(interaction, "That role does not belong to this server.")
            return
        if error := can_manage_role(invoker, role):
            await self._reject(interaction, error)
            return
        if required_role is not None and required_role.guild.id != guild.id:
            await self._reject(
                interaction, "The required role does not belong to this server."
            )
            return

        group_key: str | None = None
        if group is not None:
            candidate = group.strip()
            if not GROUP_KEY_RE.match(candidate):
                await self._reject(
                    interaction,
                    "The group name may only contain letters, digits, hyphens "
                    f"and underscores, up to {MAX_GROUP_KEY} characters.",
                )
                return
            group_key = candidate

        try:
            reaction, storage_key = parse_emoji_input(self.bot, emoji)
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)

        message, error = await self._resolve_message(guild, message_link)
        if message is None:
            await self._reject(interaction, error or "That message could not be read.")
            return

        # 'unique' needs a group to be exclusive against; defaulting to the
        # message makes every mapping on it mutually exclusive, which is what
        # people expect from a single self-assign panel.
        if mode == "unique" and group_key is None:
            group_key = f"msg{message.id}"

        try:
            await message.add_reaction(reaction)
        except discord.Forbidden:
            await self._reject(
                interaction,
                "Discord refused to add that reaction. I need `Add Reactions` "
                "in that channel.",
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not add a reaction-role emoji: %s", exc)
            await self._reject(
                interaction,
                f"Discord rejected that emoji (HTTP {exc.status}). Messages are "
                "limited to 20 distinct reactions.",
            )
            return

        values: dict[str, Any] = {
            "guild_id": guild.id,
            "channel_id": message.channel.id,
            "message_id": message.id,
            "emoji": storage_key,
            "role_id": role.id,
            "mode": mode,
            "group_key": group_key,
            "required_role_id": required_role.id if required_role is not None else None,
            "created_by": invoker.id,
        }

        existing = await self.db.get_reaction_role(message.id, storage_key)
        if existing is None:
            await self.db.insert("reaction_roles", values, on_conflict="ignore")
            verb = "Created"
        else:
            await self.db.update(
                "reaction_roles",
                {
                    "role_id": role.id,
                    "mode": mode,
                    "group_key": group_key,
                    "required_role_id": values["required_role_id"],
                    "channel_id": message.channel.id,
                },
                {"entry_id": existing["entry_id"]},
            )
            verb = "Updated"

        detail = (
            f"Message: {message.jump_url}\nEmoji: {storage_key}\n"
            f"Role: {role.name} (`{role.id}`)\nMode: {mode}"
        )
        if group_key:
            detail += f"\nGroup: {group_key}"
        await self._audit(guild, invoker, f"Reaction role {verb.lower()}", detail)

        lines = [
            f"\u2705 {verb} the reaction role: reacting on "
            f"[that message]({message.jump_url}) now affects {role.mention}.",
            f"Mode: `{mode}`",
        ]
        if group_key:
            lines.append(
                f"Group: `{group_key}` \u2014 members may hold only one role from "
                "this group at a time."
            )
        if required_role is not None:
            lines.append(f"Only members with {required_role.mention} may use it.")
        if mode == "add_only":
            lines.append("Removing the reaction will not remove the role.")
        elif mode == "remove_only":
            lines.append("Reacting removes the role instead of granting it.")

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(
        name="reactionrole-remove",
        description="Remove one or all reaction role mappings from a message.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(
        message_link="Link to the message the mapping belongs to",
        emoji="The emoji to unmap (leave empty to remove every mapping)",
        clear_reactions="Also remove the reactions from the message",
    )
    async def reactionrole_remove_cmd(
        self,
        interaction: discord.Interaction,
        message_link: str,
        emoji: Optional[str] = None,
        clear_reactions: bool = True,
    ) -> None:
        context = await self._authorize(interaction, "manage_roles")
        if context is None:
            return
        guild, invoker = context

        storage_key: str | None = None
        reaction: discord.PartialEmoji | str | None = None
        if emoji is not None:
            try:
                reaction, storage_key = parse_emoji_input(self.bot, emoji)
            except ValueError as exc:
                await self._reject(interaction, str(exc))
                return

        await interaction.response.defer(ephemeral=True)

        message, error = await self._resolve_message(guild, message_link)
        if message is None:
            await self._reject(interaction, error or "That message could not be read.")
            return

        where: dict[str, Any] = {"guild_id": guild.id, "message_id": message.id}
        if storage_key is not None:
            where["emoji"] = storage_key

        removed = await self.db.delete("reaction_roles", where)
        if not removed:
            await interaction.followup.send(
                "\u2139\ufe0f There were no matching reaction role mappings on "
                "that message.",
                ephemeral=True,
            )
            return

        cleared = False
        if clear_reactions:
            cleared = await self._clear_reactions(message, reaction)

        await self._audit(
            guild,
            invoker,
            "Reaction role removed",
            f"Message: {message.jump_url}\n"
            f"Emoji: {storage_key or 'all'}\nMappings removed: {removed}",
        )

        lines = [
            f"\u2705 Removed {removed} reaction role mapping(s) from "
            f"[that message]({message.jump_url})."
        ]
        if clear_reactions:
            lines.append(
                "The reactions were cleared."
                if cleared
                else "The reactions could not be cleared \u2014 I need "
                "`Manage Messages` there."
            )
        lines.append("Roles members already hold were left untouched.")

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    @staticmethod
    async def _clear_reactions(
        message: discord.Message, reaction: discord.PartialEmoji | str | None
    ) -> bool:
        """Removes reactions from a message. Returns True on success."""
        try:
            if reaction is None:
                await message.clear_reactions()
            else:
                await message.clear_reaction(reaction)
        except (discord.Forbidden, discord.NotFound):
            return False
        except discord.HTTPException as exc:
            log.debug("Could not clear reactions on message %s: %s", message.id, exc)
            return False
        return True


class ReactionRoleEvents(commands.Cog):
    """Applies the reaction role mappings created by ``/reactionrole-add``.

    Raw events are used so mappings keep working after a restart, when the
    message is no longer in discord.py's cache.

    Every branch re-validates the state of the world before editing a member:
    the role must still exist, must not be managed, and must sit below Fyrion's
    highest role. A stale mapping is logged and ignored rather than retried.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        await self._handle(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        await self._handle(payload, added=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """Drops mappings whose message no longer exists."""
        if payload.guild_id is None:
            return
        try:
            await self.db.delete(
                "reaction_roles",
                {"guild_id": payload.guild_id, "message_id": payload.message_id},
            )
        except Exception:
            log.exception(
                "Could not clean up reaction roles for deleted message %s.",
                payload.message_id,
            )

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    async def _lookup(
        self, message_id: int, emoji: discord.PartialEmoji
    ) -> dict[str, Any] | None:
        """Finds the mapping for a reaction, tolerating renamed custom emoji."""
        key = emoji_storage_key(emoji)
        try:
            row = await self.db.get_reaction_role(message_id, key)
            if row is not None:
                return row
            if emoji.id is None:
                return None

            # Custom emoji are identified by their id; the stored name may be
            # out of date if the emoji has since been renamed.
            rows = await self.db.fetch_many(
                "reaction_roles", {"message_id": message_id}
            )
        except Exception:
            log.exception("Could not read reaction roles for message %s.", message_id)
            return None

        suffix = f":{emoji.id}"
        for candidate in rows:
            if str(candidate.get("emoji") or "").endswith(suffix):
                return candidate
        return None

    async def _handle(
        self, payload: discord.RawReactionActionEvent, *, added: bool
    ) -> None:
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        mapping = await self._lookup(payload.message_id, payload.emoji)
        if mapping is None:
            return
        if int(mapping.get("guild_id") or 0) != guild.id:
            return

        role = guild.get_role(int(mapping["role_id"]))
        if role is None:
            log.info(
                "Reaction role mapping %s points at a deleted role; ignoring.",
                mapping.get("entry_id"),
            )
            return
        if role.is_default() or role.managed or me.top_role <= role:
            log.warning(
                "Cannot apply reaction role %s in guild %s: the role is managed "
                "or ranks at or above mine.",
                role.id,
                guild.id,
            )
            return

        member = await self._member(guild, payload)
        if member is None or member.bot:
            return

        required = mapping.get("required_role_id")
        if required and not any(int(required) == item.id for item in member.roles):
            return

        mode = str(mapping.get("mode") or "toggle")
        reason = f"Reaction role ({mode}) on message {payload.message_id}"[
            :AUDIT_REASON_LIMIT
        ]

        if added:
            if mode == "remove_only":
                await self._remove(member, role, reason)
                return

            await self._add(member, role, reason)
            if mode == "unique":
                await self._enforce_unique(guild, member, mapping, role, payload)
            return

        # Reaction removed: only 'toggle' reverses the grant. 'add_only' and
        # 'unique' are sticky by design, and 'remove_only' must not re-grant a
        # role the member deliberately dropped.
        if mode == "toggle":
            await self._remove(member, role, reason)

    async def _member(
        self, guild: discord.Guild, payload: discord.RawReactionActionEvent
    ) -> discord.Member | None:
        if payload.member is not None:
            return payload.member

        member = guild.get_member(payload.user_id)
        if member is not None:
            return member

        try:
            return await guild.fetch_member(payload.user_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        except discord.HTTPException as exc:
            log.debug(
                "Could not fetch member %s in guild %s: %s",
                payload.user_id,
                guild.id,
                exc,
            )
            return None

    @staticmethod
    async def _add(member: discord.Member, role: discord.Role, reason: str) -> None:
        if any(existing.id == role.id for existing in member.roles):
            return
        try:
            await member.add_roles(role, reason=reason)
        except discord.Forbidden:
            log.warning(
                "Discord refused to grant role %s to member %s.", role.id, member.id
            )
        except discord.HTTPException as exc:
            log.warning("Reaction role grant failed for %s: %s", member.id, exc)

    @staticmethod
    async def _remove(member: discord.Member, role: discord.Role, reason: str) -> None:
        if not any(existing.id == role.id for existing in member.roles):
            return
        try:
            await member.remove_roles(role, reason=reason)
        except discord.Forbidden:
            log.warning(
                "Discord refused to remove role %s from member %s.", role.id, member.id
            )
        except discord.HTTPException as exc:
            log.warning("Reaction role removal failed for %s: %s", member.id, exc)

    async def _enforce_unique(
        self,
        guild: discord.Guild,
        member: discord.Member,
        mapping: dict[str, Any],
        granted: discord.Role,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Removes the other roles (and reactions) in an exclusive group."""
        group_key = mapping.get("group_key")
        if not group_key:
            return

        try:
            siblings = await self.db.get_reaction_role_group(guild.id, str(group_key))
        except Exception:
            log.exception(
                "Could not read reaction role group %s in guild %s.",
                group_key,
                guild.id,
            )
            return

        me = guild.me
        if me is None:
            return

        reason = f"Reaction role group '{group_key}' is exclusive"[
            :AUDIT_REASON_LIMIT
        ]
        message: discord.Message | None = None

        for sibling in siblings:
            role_id = int(sibling.get("role_id") or 0)
            if role_id == granted.id:
                continue

            role = guild.get_role(role_id)
            if role is not None and not role.managed and me.top_role > role:
                await self._remove(member, role, reason)

            # Also drop the member's stale reaction so the panel reflects the
            # single role they now hold.
            if int(sibling.get("message_id") or 0) != payload.message_id:
                continue
            if message is None:
                message = await self._fetch_message(guild, payload)
                if message is None:
                    continue
            await self._remove_reaction(
                message, str(sibling.get("emoji") or ""), member
            )

    async def _fetch_message(
        self, guild: discord.Guild, payload: discord.RawReactionActionEvent
    ) -> discord.Message | None:
        channel = guild.get_channel_or_thread(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None

        me = guild.me
        if me is None:
            return None
        permissions = channel.permissions_for(me)
        if not (permissions.read_message_history and permissions.manage_messages):
            return None

        try:
            return await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        except discord.HTTPException as exc:
            log.debug("Could not fetch message %s: %s", payload.message_id, exc)
            return None

    @staticmethod
    async def _remove_reaction(
        message: discord.Message, stored_emoji: str, member: discord.Member
    ) -> None:
        if not stored_emoji:
            return

        if ":" in stored_emoji:
            name, _, raw_id = stored_emoji.rpartition(":")
            if raw_id.isdigit():
                emoji: discord.PartialEmoji | str = discord.PartialEmoji(
                    name=name, id=int(raw_id)
                )
            else:
                emoji = stored_emoji
        else:
            emoji = stored_emoji

        try:
            await message.remove_reaction(emoji, member)
        except (discord.Forbidden, discord.NotFound):
            pass
        except discord.HTTPException as exc:
            log.debug(
                "Could not remove reaction %s for member %s: %s",
                stored_emoji,
                member.id,
                exc,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
    await bot.add_cog(ReactionRoleEvents(bot))


__all__ = [
    "Admin",
    "ReactionRoleEvents",
    "MAX_ROLE_ALL_TARGETS",
    "MESSAGE_LINK_RE",
    "emoji_storage_key",
    "parse_emoji_input",
    "setup",
]
