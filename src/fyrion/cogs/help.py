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
            # value is pinned to the raw cog key; the label may be prettified but
            # capitalize() lowercases the tail ("AutoModCommands" -> "Automodcommands"),
            # which would no longer match cogs_dict and raise KeyError in the callback.
            options.append(
                discord.SelectOption(
                    label=name, value=name, description=f"Commands for {name}"
                )
            )

        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_cog_name = self.values[0]
        cog = self.cogs_dict[selected_cog_name]

        embed = discord.Embed(
            title=f"{selected_cog_name} Commands",
            description=cog.__doc__ or "No description provided.",
            color=discord.Color.blurple()
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
        # Set once the menu is sent, so on_timeout can grey the dropdown out
        # rather than leaving a dead, still-clickable-looking control behind.
        self.message: discord.Message | discord.InteractionMessage | None = None
        self.add_item(HelpCategorySelect(cogs_dict))

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Select, discord.ui.Button)):
                child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            # The menu already expired for the viewer; nothing left to disable.
            pass

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
        # Keep a handle to the sent message so the view can disable itself on timeout.
        view.message = await interaction.original_response()

async def setup(bot: Fyrion) -> None:
    await bot.add_cog(Help(bot))
