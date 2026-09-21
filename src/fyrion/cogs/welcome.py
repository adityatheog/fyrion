"""
Welcome, leave and autorole handling.

The listeners read from ``guild_settings`` first and fall back to the legacy
``guild_configs`` row, so servers configured with either ``/set-welcome`` /
``/set-autorole`` (new) or ``/config welcome`` / ``/config autorole`` (original)
keep working without a migration step.

Greeting templates are operator supplied, so:

* rendering only substitutes a fixed set of placeholders — no expression
  evaluation and no arbitrary attribute access;
* messages are sent with ``@everyone`` and role mentions disabled, so a template
  can ping the joining member and nothing else;
* the rendered text is truncated to Discord's message limit before sending.

Autorole assignment is validated before the API call: Discord refuses to assign
a role at or above the bot's highest role, so that case is logged as a
configuration problem rather than retried on every join.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.database.repositories.guild_config import GuildConfigRepository

log = logging.getLogger("fyrion.cogs.welcome")

MAX_MESSAGE_LENGTH = 2000

DEFAULT_WELCOME = "Welcome to **{server}**, {user_mention}!"
DEFAULT_GOODBYE = "**{user}** has left **{server}**."

# Greetings address exactly one member, so user mentions are allowed and nothing
# else is: a crafted template or nickname can never ping a role or @everyone.
GREETING_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)
NO_MENTIONS = discord.AllowedMentions.none()

PLACEHOLDER_HELP = (
    "Placeholders: `{user}`, `{user_mention}`, `{user_name}`, `{user_id}`, "
    "`{server}`, `{member_count}`."
)


def render_template(template: str, member: discord.Member) -> str:
    """Substitutes the supported placeholders in a greeting template."""
    guild = member.guild
    count = guild.member_count if guild.member_count is not None else len(guild.members)

    replacements = {
        "{user}": str(member),
        "{user_mention}": member.mention,
        "{user_name}": member.display_name,
        "{user_id}": str(member.id),
        "{server}": guild.name,
        "{member_count}": str(count),
    }

    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered[:MAX_MESSAGE_LENGTH]


class WelcomeEvents(commands.Cog):
    """Background listeners for member joins and leaves."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.legacy = GuildConfigRepository(self.db)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def _settings(self, guild_id: int) -> dict[str, Any]:
        """Returns the effective configuration, merging both storage layers."""
        settings: dict[str, Any] = {}

        getter = getattr(self.db, "get_guild_settings", None)
        if getter is not None:
            try:
                settings = dict(await getter(guild_id))
            except Exception:
                log.exception("Could not read guild settings for %s.", guild_id)

        try:
            legacy = await self.legacy.get_config(guild_id)
        except Exception:
            log.exception("Could not read the legacy guild config for %s.", guild_id)
            legacy = {}

        # The legacy row only fills gaps; an explicit new-style value wins.
        for key in ("welcome_channel_id", "autorole_id"):
            if not settings.get(key) and legacy.get(key):
                settings[key] = legacy[key]

        return settings

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        config = await self._settings(guild.id)

        await self._apply_autorole(member, config.get("autorole_id"))
        await self._greet(
            member,
            channel_id=config.get("welcome_channel_id"),
            template=config.get("welcome_message"),
            default_template=DEFAULT_WELCOME,
            title="Member joined",
            color=discord.Color.green(),
            footer=f"Member #{guild.member_count}" if guild.member_count else None,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        config = await self._settings(member.guild.id)
        await self._greet(
            member,
            channel_id=config.get("goodbye_channel_id"),
            template=config.get("goodbye_message"),
            default_template=DEFAULT_GOODBYE,
            title="Member left",
            color=discord.Color.dark_grey(),
            footer=None,
        )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _apply_autorole(self, member: discord.Member, role_id: Any) -> None:
        if not role_id:
            return

        guild = member.guild
        me = guild.me
        if me is None:
            return

        role = guild.get_role(int(role_id))
        if role is None:
            log.info("Autorole %s in guild %s no longer exists.", role_id, guild.id)
            return

        if role.is_default() or role.managed:
            log.warning(
                "Autorole %s in guild %s is managed by Discord and cannot be "
                "assigned.",
                role.id,
                guild.id,
            )
            return

        if not me.guild_permissions.manage_roles or me.top_role <= role:
            log.warning(
                "Cannot assign autorole %s in guild %s: missing Manage Roles or "
                "the role ranks at or above mine.",
                role.id,
                guild.id,
            )
            return

        try:
            await member.add_roles(role, reason="Fyrion autorole")
        except discord.Forbidden:
            log.warning(
                "Discord refused the autorole %s in guild %s.", role.id, guild.id
            )
        except discord.HTTPException as exc:
            log.warning("Autorole assignment failed in guild %s: %s", guild.id, exc)

    async def _greet(
        self,
        member: discord.Member,
        *,
        channel_id: Any,
        template: Any,
        default_template: str,
        title: str,
        color: discord.Color,
        footer: str | None,
    ) -> None:
        if not channel_id:
            return

        guild = member.guild
        me = guild.me
        if me is None:
            return

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            log.debug(
                "Greeting channel %s in guild %s is missing or not a text channel.",
                channel_id,
                guild.id,
            )
            return

        permissions = channel.permissions_for(me)
        if not permissions.send_messages:
            log.debug(
                "Cannot greet in #%s (guild %s): missing Send Messages.",
                channel.name,
                guild.id,
            )
            return

        custom = str(template).strip() if template else ""

        try:
            if custom:
                # A configured template is used verbatim so mentions inside it
                # actually notify the member.
                await channel.send(
                    render_template(custom, member),
                    allowed_mentions=GREETING_MENTIONS,
                )
                return

            if not permissions.embed_links:
                await channel.send(
                    render_template(default_template, member),
                    allowed_mentions=GREETING_MENTIONS,
                )
                return

            embed = discord.Embed(
                title=title,
                description=render_template(default_template, member),
                color=color,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            if footer:
                embed.set_footer(text=footer)

            await channel.send(embed=embed, allowed_mentions=GREETING_MENTIONS)
        except discord.Forbidden:
            log.warning(
                "Discord refused the greeting in #%s (guild %s).",
                channel.name,
                guild.id,
            )
        except discord.HTTPException as exc:
            log.warning("Greeting failed in guild %s: %s", guild.id, exc)


class ServerConfig(commands.GroupCog, name="config"):
    """Server configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.legacy = GuildConfigRepository(self.db)

    async def _save(self, guild_id: int, key: str, value: Any) -> None:
        """Writes a setting to both the new and the legacy storage."""
        await self.legacy.update_config(guild_id, key, value)

        updater = getattr(self.db, "update_guild_settings", None)
        if updater is None:
            return
        try:
            await updater(guild_id, **{key: value})
        except Exception:
            log.exception(
                "Could not mirror %s into guild_settings for guild %s.", key, guild_id
            )

    @app_commands.command(
        name="welcome", description="Set or disable the welcome channel."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="The channel to send welcome messages in (leave blank to disable)"
    )
    async def config_welcome_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "\u274c This command can only be used in a server.", ephemeral=True
            )
            return

        permissions = member.guild_permissions
        if not (permissions.administrator or permissions.manage_guild):
            await interaction.response.send_message(
                "\u274c You need the `Manage Server` permission for that.",
                ephemeral=True,
            )
            return

        if channel is None:
            await self._save(guild.id, "welcome_channel_id", None)
            await interaction.response.send_message(
                "\u2705 Welcome messages have been **disabled**.", ephemeral=True
            )
            return

        me = guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            await interaction.response.send_message(
                f"\u274c I cannot send messages in {channel.mention}.",
                ephemeral=True,
            )
            return

        await self._save(guild.id, "welcome_channel_id", channel.id)
        await interaction.response.send_message(
            f"\u2705 Welcome messages will now be sent in {channel.mention}.\n"
            f"Use `/set-welcome` to supply a custom template. {PLACEHOLDER_HELP}",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @app_commands.command(
        name="autorole",
        description="Set or disable the automatic role given to new members.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="The role to give new members (leave blank to disable)")
    async def config_autorole_cmd(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "\u274c This command can only be used in a server.", ephemeral=True
            )
            return

        permissions = member.guild_permissions
        if not (permissions.administrator or permissions.manage_guild):
            await interaction.response.send_message(
                "\u274c You need the `Manage Server` permission for that.",
                ephemeral=True,
            )
            return

        if role is None:
            await self._save(guild.id, "autorole_id", None)
            await interaction.response.send_message(
                "\u2705 Autorole has been **disabled**.", ephemeral=True
            )
            return

        if role.managed or role.is_default():
            await interaction.response.send_message(
                "\u274c Managed, integration and `@everyone` roles cannot be "
                "used as an autorole.",
                ephemeral=True,
            )
            return

        me = guild.me
        if me is None or not me.guild_permissions.manage_roles or me.top_role <= role:
            await interaction.response.send_message(
                "\u274c I cannot assign that role: it is equal to or above my "
                "highest role, or I lack `Manage Roles`.",
                ephemeral=True,
            )
            return

        await self._save(guild.id, "autorole_id", role.id)
        await interaction.response.send_message(
            f"\u2705 Autorole set to **{role.name}**.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeEvents(bot))
    await bot.add_cog(ServerConfig(bot))


__all__ = [
    "WelcomeEvents",
    "ServerConfig",
    "DEFAULT_WELCOME",
    "DEFAULT_GOODBYE",
    "PLACEHOLDER_HELP",
    "render_template",
    "setup",
]
