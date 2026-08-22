"""
Welcome and Autorole cog.
Handles member join events and server configuration commands.
"""
import logging
import discord
from discord import app_commands
from discord.ext import commands

from fyrion.bot import Fyrion
from fyrion.database.repositories.guild_config import GuildConfigRepository

log = logging.getLogger("fyrion.cogs.welcome")

class WelcomeEvents(commands.Cog):
    """Background listener for member join events."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.configs = GuildConfigRepository(bot.db)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        # Ignore if the bot hasn't fully loaded the guild cache
        if not member.guild:
            return
            
        config = await self.configs.get_config(member.guild.id)
        
        # 1. Autorole Processing
        autorole_id = config.get("autorole_id")
        if autorole_id:
            role = member.guild.get_role(autorole_id)
            bot_member = member.guild.me
            
            if role:
                # Strictly validate permissions and hierarchy before attempting assignment
                if bot_member.guild_permissions.manage_roles and bot_member.top_role > role:
                    try:
                        await member.add_roles(role, reason="Fyrion Autorole")
                    except discord.HTTPException as e:
                        log.warning(f"Failed to assign autorole in {member.guild.id}: {e}")
                else:
                    log.warning(f"Cannot assign autorole in {member.guild.id}: Missing permissions or role hierarchy too low.")

        # 2. Welcome Message Processing
        welcome_channel_id = config.get("welcome_channel_id")
        if welcome_channel_id:
            channel = member.guild.get_channel(welcome_channel_id)
            
            if isinstance(channel, discord.TextChannel) and channel.permissions_for(member.guild.me).send_messages:
                try:
                    embed = discord.Embed(
                        title="Member Joined",
                        description=f"Welcome to **{member.guild.name}**, {member.mention}!",
                        color=discord.Color.green()
                    )
                    embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
                    embed.set_footer(text=f"Member #{member.guild.member_count}")
                    
                    await channel.send(embed=embed)
                except discord.HTTPException as e:
                    log.warning(f"Failed to send welcome message in {member.guild.id}: {e}")


class ServerConfig(commands.GroupCog, name="config"):
    """Server configuration commands."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.configs = GuildConfigRepository(bot.db)

    @app_commands.command(name="welcome", description="Set or disable the welcome channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The channel to send welcome messages in (leave blank to disable)")
    async def config_welcome_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel = None) -> None:
        assert interaction.guild_id is not None
        
        if channel is None:
            await self.configs.update_config(interaction.guild_id, "welcome_channel_id", None)
            await interaction.response.send_message("✅ Welcome messages have been **disabled**.", ephemeral=True)
            return

        # Proactively check bot's write permissions in the requested channel
        if not channel.permissions_for(interaction.guild.me).send_messages: # type: ignore
            await interaction.response.send_message(f"❌ I do not have permission to send messages in {channel.mention}.", ephemeral=True)
            return
            
        await self.configs.update_config(interaction.guild_id, "welcome_channel_id", channel.id)
        await interaction.response.send_message(f"✅ Welcome messages will now be sent in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="autorole", description="Set or disable the automatic role given to new members.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role to give new members (leave blank to disable)")
    async def config_autorole_cmd(self, interaction: discord.Interaction, role: discord.Role = None) -> None:
        assert interaction.guild_id is not None
        
        if role is None:
            await self.configs.update_config(interaction.guild_id, "autorole_id", None)
            await interaction.response.send_message("✅ Autorole has been **disabled**.", ephemeral=True)
            return

        # Protect against assigning dangerous or impossible roles
        if role.is_bot_managed() or role.is_integration() or role.is_default():
            await interaction.response.send_message("❌ You cannot use a managed, integration, or @everyone role as an autorole.", ephemeral=True)
            return
            
        # Hierarchy Check: The bot cannot assign a role higher than its own
        if interaction.guild.me.top_role <= role: # type: ignore
            await interaction.response.send_message("❌ I cannot assign this role because it is higher than or equal to my highest role.", ephemeral=True)
            return
            
        await self.configs.update_config(interaction.guild_id, "autorole_id", role.id)
        await interaction.response.send_message(f"✅ Autorole set to **{role.name}**.", ephemeral=True)

async def setup(bot: Fyrion) -> None:
    await bot.add_cog(WelcomeEvents(bot))
    await bot.add_cog(ServerConfig(bot))
