"""
Persistent views for the ticket system.
"""
import logging
import discord
from discord.ui import View, Button
from fyrion.database.repositories.tickets import TicketRepository

log = logging.getLogger("fyrion.views.tickets")

class TicketControlView(View):
    """View attached inside the actual ticket channel for staff and users."""
    def __init__(self) -> None:
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="fyrion:ticket:close", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: Button) -> None:
        # Prevent double-clicks by immediately deferring
        await interaction.response.defer()
        
        repo = TicketRepository(interaction.client.db) # type: ignore
        await repo.close_ticket(interaction.channel_id) # type: ignore
        
        try:
            assert isinstance(interaction.channel, discord.TextChannel)
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I lack permissions to delete this channel.", ephemeral=True)
        except discord.HTTPException as e:
            log.error(f"Failed to delete ticket channel {interaction.channel_id}: {e}")

class TicketPanelView(View):
    """The persistent panel containing the 'Create Ticket' button."""
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="fyrion:ticket:create", emoji="🎫")
    async def create_ticket(self, interaction: discord.Interaction, button: Button) -> None:
        assert interaction.guild is not None
        repo = TicketRepository(interaction.client.db) # type: ignore
        
        # 1. Configuration Check
        config = await repo.get_config(interaction.guild.id)
        if not config or not config.get("category_id"):
            await interaction.response.send_message("❌ The ticket system is not configured for this server.", ephemeral=True)
            return
            
        category = interaction.guild.get_channel(config["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("❌ The configured ticket category is missing or invalid.", ephemeral=True)
            return

        # 2. Duplicate Check (One open ticket per user)
        existing = await repo.get_open_ticket_for_user(interaction.guild.id, interaction.user.id)
        if existing:
            await interaction.response.send_message("❌ You already have an open ticket.", ephemeral=True)
            return

        # 3. Create Channel securely
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        try:
            # We prefix with ticket- to identify them easily
            safe_name = "".join([c for c in interaction.user.display_name if c.isalnum()]).lower() or "user"
            channel = await category.create_text_channel(
                name=f"ticket-{safe_name}", 
                overwrites=overwrites,
                reason=f"Ticket created by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I lack permissions to create channels in the ticket category.", ephemeral=True)
            return

        # 4. Database persistence and greeting
        await repo.create_ticket(interaction.guild.id, channel.id, interaction.user.id)
        
        embed = discord.Embed(
            title="Ticket Created",
            description=f"Welcome {interaction.user.mention}. Please describe your issue below.\nStaff will be with you shortly.",
            color=discord.Color.blue()
        )
        await channel.send(embed=embed, view=TicketControlView())
        await interaction.response.send_message(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)
