"""
Core gateway client.

``AutoShardedBot`` is used rather than ``Bot`` so a single process can span
several gateway shards as the bot grows; with ``shard_count=None`` discord.py
asks Discord for the recommended shard count at connect time.

Startup work happens in :meth:`Fyrion.setup_hook`, which discord.py runs after
login but before the gateway connection is used: open the database pool,
register persistent views, load every cog found under ``fyrion.cogs``, then
synchronize the application command tree.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from typing import Any, Sequence

import discord
from discord.ext import commands
from discord.utils import utcnow

from fyrion.config import Config
from fyrion.database.manager import DatabasePool
from fyrion.views.tickets import TicketControlView, TicketPanelView

log = logging.getLogger("fyrion.bot")

# Every non-private module directly inside this package is a loadable extension.
COGS_PACKAGE = "fyrion.cogs"

# Persistent views must be re-registered on each boot. Their ``custom_id``s are
# stable, so Discord routes interactions from pre-restart messages back here.
PERSISTENT_VIEWS: tuple[type[discord.ui.View], ...] = (
    TicketPanelView,
    TicketControlView,
)


def build_intents() -> discord.Intents:
    """Declares exactly the gateway intents Fyrion needs, and nothing more.

    Starting from ``Intents.none()`` keeps the declaration explicit: every flag
    below is required by a shipped feature. ``members`` and ``message_content``
    are privileged and must be enabled in the Discord Developer Portal,
    otherwise the gateway refuses the connection.
    """
    intents = discord.Intents.none()

    intents.guilds = True  # guild, channel and role cache: needed everywhere
    intents.members = True  # PRIVILEGED: autorole, welcome, invite tracking
    intents.message_content = True  # PRIVILEGED: AutoMod content scanning
    intents.guild_messages = True  # on_message delivery inside guilds
    intents.guild_reactions = True  # reaction roles
    intents.invites = True  # invite create/delete events for cache deltas
    intents.moderation = True  # ban/unban events for audit logging

    return intents


def discover_extensions(package_name: str = COGS_PACKAGE) -> list[str]:
    """Returns dotted module paths for every loadable cog in ``package_name``.

    Modules whose names begin with an underscore are treated as private helpers
    and skipped, so shared code can live beside cogs without being imported as
    an extension. Sub-packages are ignored; add them explicitly if needed.
    """
    package = importlib.import_module(package_name)
    search_paths = getattr(package, "__path__", None)
    if search_paths is None:
        raise RuntimeError(f"{package_name} is not a package; cannot discover cogs.")

    return sorted(
        f"{package_name}.{module.name}"
        for module in pkgutil.iter_modules(search_paths)
        if not module.ispkg and not module.name.startswith("_")
    )


class Fyrion(commands.AutoShardedBot):
    """Sharded Fyrion client."""

    def __init__(
        self,
        *,
        db: Any | None = None,
        extensions: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            help_command=None,  # replaced by the /help slash command
            shard_count=Config.SHARD_COUNT,  # None: Discord recommends the count
            chunk_guilds_at_startup=False,  # avoid a member fetch storm on boot
            max_messages=Config.MESSAGE_CACHE_SIZE,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
                replied_user=False,
            ),
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=Config.ACTIVITY_NAME,
            ),
            status=discord.Status.online,
        )

        # Repositories and the dashboard expect ``bot.db`` to expose the pooled
        # query helpers. Injectable so tests can pass an in-memory pool.
        self.db: Any = db if db is not None else DatabasePool()
        self.boot_time = utcnow()
        # Lets the dashboard report readiness without polling ``is_ready()``.
        self.ready_event = asyncio.Event()

        self._requested_extensions: tuple[str, ...] | None = (
            tuple(extensions) if extensions is not None else None
        )
        self.loaded_extensions: tuple[str, ...] = ()
        self.failed_extensions: tuple[str, ...] = ()
        self._synced = False

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """Runs once, after login but before the gateway connection is used."""
        log.info("Fyrion boot sequence starting.")

        await self.db.connect()
        if hasattr(self.db, "start_maintenance"):
            self.db.start_maintenance(Config.DATABASE_MAINTENANCE_INTERVAL_SECONDS)

        # The command tree's error handler is installed by the ErrorHandler cog
        # when it loads in _load_extensions below.
        for view_cls in PERSISTENT_VIEWS:
            self.add_view(view_cls())
        log.info("Registered %d persistent view(s).", len(PERSISTENT_VIEWS))

        await self._load_extensions()

        if Config.SYNC_COMMANDS_ON_STARTUP:
            await self.sync_commands()
        else:
            log.info("Command sync skipped (SYNC_COMMANDS_ON_STARTUP=false).")

        log.info("Setup complete; connecting to the gateway.")

    async def _load_extensions(self) -> None:
        """Loads every discovered cog, tolerating individual failures."""
        if self._requested_extensions is not None:
            extensions = list(self._requested_extensions)
        else:
            extensions = discover_extensions()

        if not extensions:
            log.warning("No cogs discovered in %s.", COGS_PACKAGE)
            return

        loaded: list[str] = []
        failed: list[str] = []
        for extension in extensions:
            try:
                await self.load_extension(extension)
            except commands.ExtensionError:
                failed.append(extension)
                # A single broken cog must not prevent the bot from starting.
                log.exception("Failed to load extension %s", extension)
            else:
                loaded.append(extension)
                log.info("Loaded extension %s", extension)

        self.loaded_extensions = tuple(loaded)
        self.failed_extensions = tuple(failed)
        log.info(
            "Extensions loaded: %d succeeded, %d failed.", len(loaded), len(failed)
        )

    async def sync_commands(self) -> int:
        """Pushes the application command tree to Discord (global scope).

        Returns the number of synced commands. Sync failures are recoverable:
        previously registered commands keep working, so this never raises into
        the boot sequence.
        """
        try:
            synced = await self.tree.sync()
        except discord.HTTPException:
            log.exception("Failed to sync the application command tree.")
            return 0

        self._synced = True
        log.info("Synced %d global application command(s).", len(synced))
        return len(synced)

    @property
    def synced(self) -> bool:
        return self._synced

    # ------------------------------------------------------------------
    # Gateway events
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        self.ready_event.set()
        if self.user is not None:
            log.info("Authenticated as %s (ID: %s)", self.user, self.user.id)
        log.info(
            "Ready across %d shard(s) and %d guild(s).",
            self.shard_count or 1,
            len(self.guilds),
        )

    async def on_resumed(self) -> None:
        log.info("Gateway session resumed.")

    async def on_shard_ready(self, shard_id: int) -> None:
        log.info("Shard %d is ready.", shard_id)

    async def on_shard_disconnect(self, shard_id: int) -> None:
        log.warning(
            "Shard %d disconnected; discord.py will attempt to resume.", shard_id
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Creates the guild's parent rows so child inserts never fail on FK."""
        log.info("Joined guild %s (ID: %s).", guild.name, guild.id)
        ensure_guild = getattr(self.db, "ensure_guild", None)
        if ensure_guild is None:
            return
        try:
            await ensure_guild(guild.id)
        except Exception:
            log.exception("Could not initialize storage for guild %s.", guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Data is deliberately retained: a kick is often temporary, and silently
        # destroying a server's configuration is not recoverable.
        log.info(
            "Removed from guild %s (ID: %s); stored data retained.",
            guild.name,
            guild.id,
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Closes the database pool before tearing down the gateway session."""
        if self.is_closed():
            return

        log.info("Closing Fyrion.")
        try:
            if hasattr(self.db, "stop_maintenance"):
                await self.db.stop_maintenance()
            await self.db.close()
        except Exception:
            log.exception("Error while closing the database pool.")
        finally:
            await super().close()


__all__ = [
    "Fyrion",
    "build_intents",
    "discover_extensions",
    "COGS_PACKAGE",
    "PERSISTENT_VIEWS",
]
