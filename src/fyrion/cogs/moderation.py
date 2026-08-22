"""
Core moderation cog.
Includes Kick, Ban, Timeout, Warn, and Purge.
"""
import logging
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta

from fyrion.bot import Fyrion
from fyrion.utils.permissions import can_moderate
from fyrion.database.repositories.warnings import WarningsRepository

log = logging.getLogger("fyrion.cogs.moderation")

class Moderation(commands.GroupCog, name="mod"):
    """Server moderation commands."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.warnings = WarningsRepository(bot.db)

    @app_commands.command(name="kick", description="Kicks a member from the server.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.describe(member="The member to kick", reason="Reason for the kick")
    async def kick_cmd(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        if error := can_moderate(interaction.user, member): # type: ignore
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return

        try:
            await member.kick(reason=f"By {interaction.user}: {reason}")
            await interaction.response.send_message(f"✅ Successfully kicked **{member.display_name}**. Reason: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I do not have permission to kick this member.", ephemeral=True)

    @app_commands.command(name="ban", description="Bans a member from the server.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(member="The member to ban", reason="Reason for the ban", delete_days="Days of messages to delete (0-7)")
    async def ban_cmd(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        if error := can_moderate(interaction.user, member): # type: ignore
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return

        try:
            await member.ban(reason=f"By {interaction.user}: {reason}", delete_message_days=delete_days)
            await interaction.response.send_message(f"✅ Successfully banned **{member.display_name}**. Reason: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I do not have permission to ban this member.", ephemeral=True)

    @app_commands.command(name="timeout", description="Temporarily times out a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(member="The member to timeout", minutes="Timeout duration in minutes", reason="Reason for timeout")
    async def timeout_cmd(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided") -> None:
        if error := can_moderate(interaction.user, member): # type: ignore
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return

        duration = timedelta(minutes=minutes)
        try:
            await member.timeout(duration, reason=f"By {interaction.user}: {reason}")
            await interaction.response.send_message(f"✅ **{member.display_name}** has been timed out for {minutes} minutes.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I do not have permission to time out this member.", ephemeral=True)

    @app_commands.command(name="warn", description="Issues a formal warning to a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def warn_cmd(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        if error := can_moderate(interaction.user, member): # type: ignore
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
            
        assert interaction.guild_id is not None
        await self.warnings.add_warning(interaction.guild_id, member.id, interaction.user.id, reason)
        
        # DM the user (fail silently if DMs are closed)
        try:
            await member.send(f"⚠️ You have been warned in **{interaction.guild.name}**. Reason: {reason}")
        except discord.HTTPException:
            pass

        await interaction.response.send_message(f"✅ Successfully warned **{member.display_name}**.")

    @app_commands.command(name="warnings", description="View a member's warnings.")
    @app_commands.default_permissions(moderate_members=True)
    async def warnings_cmd(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        records = await self.warnings.get_warnings(interaction.guild_id, member.id)
        
        if not records:
            await interaction.response.send_message(f"✅ **{member.display_name}** has no warnings.", ephemeral=True)
            return
            
        embed = discord.Embed(title=f"Warnings for {member.display_name}", color=discord.Color.orange())
        for idx, row in enumerate(records[:10], 1):  # Limit display to 10 for embed limits
            embed.add_field(name=f"Warning {idx} | {row['created_at'][:19]}", value=f"Reason: {row['reason']}", inline=False)
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="purge", description="Deletes a specified number of messages.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(amount="Number of messages to delete (1-100)")
    async def purge_cmd(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
        # Acknowledge the interaction immediately to prevent timeout while purging
        await interaction.response.defer(ephemeral=True)
        
        try:
            assert isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel))
            deleted = await interaction.channel.purge(limit=amount)
            await interaction.followup.send(f"✅ Purged {len(deleted)} messages.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ I am missing `Manage Messages` or `Read Message History` permissions.", ephemeral=True)
        except Exception as e:
            log.error(f"Purge error in {interaction.channel_id}: {e}")
            await interaction.followup.send("❌ An error occurred while attempting to purge messages.", ephemeral=True)

async def setup(bot: Fyrion) -> None:
    await bot.add_cog(Moderation(bot))
