"""
Ticket configuration commands.
"""
import discord
from discord import app_commands
from discord.ext import commands

from fyrion.bot import Fyrion
from fyrion.database.repositories.tickets import TicketRepository
from fyrion.views.tickets import TicketPanelView

class Tickets(commands.GroupCog, name="ticket"):
    """Ticket system configuration commands."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.repo = TicketRepository(bot.db)

    @app_commands.command(name="setup", description="Configure the ticket system category.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(category="The category where ticket channels will be created")
    async def setup_cmd(self, interaction: discord.Interaction, category: discord.CategoryChannel) -> None:
        assert interaction.guild_id is not None
        
        # Verify bot permissions in the target category proactively
        bot_perms = category.permissions_for(interaction.guild.me) # type: ignore
        if not bot_perms.manage_channels:
            await interaction.response.send_message(f"❌ I need the `Manage Channels` permission in {category.mention} to create tickets.", ephemeral=True)
            return
            
        await self.repo.set_config(interaction.guild_id, category.id, None)
        await interaction.response.send_message(f"✅ Ticket system configured. Tickets will be created in {category.mention}.", ephemeral=True)

    @app_commands.command(name="panel", description="Send the ticket creation panel to the current channel.")
    @app_commands.default_permissions(administrator=True)
    async def panel_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        
        config = await self.repo.get_config(interaction.guild_id)
        if not config or not config.get("category_id"):
            await interaction.response.send_message("❌ Please run `/ticket setup` first.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Support Tickets",
            description="Click the button below to create a support ticket.\nPlease do not create a ticket for trivial matters.",
            color=discord.Color.blurple()
        )
        
        assert isinstance(interaction.channel, discord.TextChannel)
        await interaction.channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message("✅ Ticket panel deployed successfully.", ephemeral=True)

async def setup(bot: Fyrion) -> None:
    await bot.add_cog(Tickets(bot))
