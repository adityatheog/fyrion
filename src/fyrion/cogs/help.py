"""
Dynamic slash-command help system.
"""
import discord
from discord import app_commands
from discord.ext import commands

from fyrion.bot import Fyrion

class HelpCategorySelect(discord.ui.Select):
    def __init__(self, cogs_dict: dict[str, commands.Cog]) -> None:
        # Filter out hidden or event-only cogs without commands
        self.cogs_dict = {
            name: cog for name, cog in cogs_dict.items() 
            if cog.get_app_commands()
        }
        
        options = []
        for name in self.cogs_dict.keys():
            options.append(discord.SelectOption(label=name.capitalize(), description=f"Commands for {name}"))
            
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_cog_name = self.values[0]
        cog = self.cogs_dict[selected_cog_name]
        
        embed = discord.Embed(
            title=f"{selected_cog_name.capitalize()} Commands",
            description=cog.__doc__ or "No description provided.",
            color=discord.Color.blue()
        )
        
        for command in cog.get_app_commands():
            # Format group commands nicely (e.g. /mod kick, /config welcome)
            if isinstance(command, app_commands.Group):
                for sub in command.commands:
                    embed.add_field(name=f"/{command.name} {sub.name}", value=sub.description, inline=False)
            else:
                embed.add_field(name=f"/{command.name}", value=command.description, inline=False)
                
        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self, cogs_dict: dict[str, commands.Cog]) -> None:
        super().__init__(timeout=120)
        self.add_item(HelpCategorySelect(cogs_dict))

class Help(commands.Cog):
    """Help menu and documentation."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="Displays Fyrion's help menu and commands.")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Fyrion Help Center",
            description="Welcome to Fyrion. Select a category from the dropdown menu below to explore commands.\n\n"
                        "**Features:**\n"
                        "🛡️ Moderation & Security\n"
                        "🎫 Ticket Management\n"
                        "👋 Welcome & Autorole\n"
                        "📊 Invite Tracking",
            color=discord.Color.blurple()
        )
        
        view = HelpView(self.bot.cogs)
        # Send ephemerally to avoid cluttering chat channels
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def setup(bot: Fyrion) -> None:
    await bot.add_cog(Help(bot))
