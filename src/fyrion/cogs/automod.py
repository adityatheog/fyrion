"""
AutoMod and Security configuration cog.
Monitors messages for links and handles whitelist configurations.
"""
import logging
import re
import discord
from discord import app_commands
from discord.ext import commands

from fyrion.bot import Fyrion
from fyrion.database.repositories.guild_config import GuildConfigRepository
from fyrion.database.repositories.security import SecurityRepository

log = logging.getLogger("fyrion.cogs.automod")

# Efficient regex to catch standard http/s links, www, and common domains
URL_REGEX = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg/|discordapp\.com/invite/|[\w-]+\.\w{2,})")

class AutoMod(commands.Cog):
    """Background listener for security violations."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.configs = GuildConfigRepository(bot.db)
        self.security = SecurityRepository(bot.db)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots, webhooks, and DMs
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return

        # Fast short-circuit: avoid DB lookups if no link is present
        if not URL_REGEX.search(message.content):
            return

        # Check if anti-link is enabled for this guild
        config = await self.configs.get_config(message.guild.id)
        if not config.get("anti_link_enabled"):
            return

        # Hierarchy Bypass: Staff with Manage Messages are inherently trusted
        if message.author.guild_permissions.manage_messages:
            return

        # Fetch whitelists and check for bypasses
        whitelists = await self.security.get_whitelists(message.guild.id)
        whitelist_ids = {row['entity_id'] for row in whitelists}
        
        # Check channel and user whitelists
        if message.channel.id in whitelist_ids or message.author.id in whitelist_ids:
            return
            
        # Check role whitelists
        if any(role.id in whitelist_ids for role in message.author.roles):
            return

        # Violation confirmed - take action
        try:
            await message.delete()
            warning_msg = await message.channel.send(
                f"⚠️ {message.author.mention}, you are not allowed to send links in this channel.", 
                delete_after=5.0
            )
        except discord.Forbidden:
            log.warning(f"Missing permissions to delete links in {message.guild.id}")
        except discord.HTTPException:
            pass


class SecurityCommands(commands.GroupCog, name="security"):
    """Security configuration commands."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.configs = GuildConfigRepository(bot.db)
        self.security = SecurityRepository(bot.db)

    @app_commands.command(name="antilink", description="Enable or disable the anti-link system.")
    @app_commands.default_permissions(administrator=True)
    async def antilink_cmd(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild_id is not None
        await self.configs.update_config(interaction.guild_id, "anti_link_enabled", int(enabled))
        
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Anti-link system has been **{state}**.", ephemeral=True)

    @app_commands.command(name="whitelist", description="Whitelist entities from the anti-link system.")
    @app_commands.default_permissions(administrator=True)
    async def whitelist_cmd(self, interaction: discord.Interaction, member: discord.Member = None, role: discord.Role = None, channel: discord.TextChannel = None) -> None:
        assert interaction.guild_id is not None
        
        entities = [(member, "user"), (role, "role"), (channel, "channel")]
        added = []
        
        for entity, e_type in entities:
            if entity:
                await self.security.add_whitelist(interaction.guild_id, entity.id, e_type)
                added.append(entity.mention)
                
        if not added:
            await interaction.response.send_message("❌ You must provide at least one target (member, role, or channel).", ephemeral=True)
            return
            
        await interaction.response.send_message(f"✅ Whitelisted: {', '.join(added)}", ephemeral=True)

    @app_commands.command(name="unwhitelist", description="Remove entities from the anti-link whitelist.")
    @app_commands.default_permissions(administrator=True)
    async def unwhitelist_cmd(self, interaction: discord.Interaction, member: discord.Member = None, role: discord.Role = None, channel: discord.TextChannel = None) -> None:
        assert interaction.guild_id is not None
        
        entities = [(member, "user"), (role, "role"), (channel, "channel")]
        removed = []
        
        for entity, e_type in entities:
            if entity:
                await self.security.remove_whitelist(interaction.guild_id, entity.id, e_type)
                removed.append(entity.mention)
                
        if not removed:
            await interaction.response.send_message("❌ You must provide at least one target (member, role, or channel).", ephemeral=True)
            return
            
        await interaction.response.send_message(f"✅ Removed from whitelist: {', '.join(removed)}", ephemeral=True)

async def setup(bot: Fyrion) -> None:
    # Add both the listener cog and the command cog
    await bot.add_cog(AutoMod(bot))
    await bot.add_cog(SecurityCommands(bot))
