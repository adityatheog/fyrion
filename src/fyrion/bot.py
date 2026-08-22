"""
Core Bot module.
Handles initialization, intents, and the asynchronous startup sequence.
"""
import logging
import discord
from discord.ext import commands
from discord.utils import utcnow

from fyrion.database.connection import DatabaseManager
from fyrion.errors.handler import setup_error_handlers
from fyrion.views.tickets import TicketPanelView, TicketControlView

log = logging.getLogger("fyrion.bot")

class Fyrion(commands.Bot):
    def __init__(self) -> None:
        # Declare explicit intents required for functionality
        intents = discord.Intents.default()
        intents.message_content = True  # Required for anti-link and text processing
        intents.members = True          # Required for autorole, welcome, and caching

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None  # We use our custom slash-command help system
        )
        
        self.db = DatabaseManager()
        self.boot_time = utcnow()

    async def setup_hook(self) -> None:
        """
        Executed natively by discord.py before the bot connects to the gateway.
        Ideal for database connections, loading cogs, and registering views.
        """
        log.info("Initializing Fyrion boot sequence...")
        
        # Connect to the database
        await self.db.connect()
        
        # Attach error handlers
        setup_error_handlers(self)
        
        # Register persistent views so buttons survive restarts
        log.info("Registering persistent UI views...")
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        
        # Load cogs
        log.info("Loading extensions...")
        await self.load_extension("fyrion.cogs.moderation")
        await self.load_extension("fyrion.cogs.automod")
        await self.load_extension("fyrion.cogs.welcome")
        await self.load_extension("fyrion.cogs.tickets")
        await self.load_extension("fyrion.cogs.invites")
        await self.load_extension("fyrion.cogs.utility")
        await self.load_extension("fyrion.cogs.help")
        
        log.info("Syncing application command tree...")
        # Syncs slash commands to Discord globally.
        await self.tree.sync()
        
        log.info("Setup complete. Connecting to gateway...")

    async def on_ready(self) -> None:
        """Fired when the bot has connected and cached its guilds."""
        log.info(f"Successfully authenticated as {self.user} (ID: {self.user.id})")
        log.info(f"Connected to {len(self.guilds)} guilds.")

    async def close(self) -> None:
        """Ensures a graceful shutdown of background tasks and database connections."""
        log.info("Shutdown signal received. Closing Fyrion...")
        await self.db.close()
        await super().close()
