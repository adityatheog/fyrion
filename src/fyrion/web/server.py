"""
uvicorn wrapper for the dashboard.

``uvicorn.Server.serve()`` is awaited as a task inside Fyrion's own event loop
rather than through ``uvicorn.run()``, which would create and own a second loop.
Sharing the loop is what allows the HTTP layer to read the bot's live cache and
the same SQLite pool.

Signal handling is disabled here on purpose: :mod:`fyrion.runtime` installs the
process-wide handlers, and letting uvicorn install its own would race with them.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from discord.ext import commands

from fyrion.config import Config
from fyrion.web.app import create_app

log = logging.getLogger("fyrion.web.server")

# Give in-flight requests a moment to finish before the worker is torn down.
GRACEFUL_TIMEOUT_SECONDS = 10


class DashboardServer:
    """Owns the uvicorn server for the lifetime of the process."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.app = create_app(bot)
        self._server = uvicorn.Server(self._build_config())
        # Fyrion owns SIGINT/SIGTERM; see fyrion.runtime.install_signal_handlers.
        self._server.install_signal_handlers = lambda: None
        self._serving = False

    def _build_config(self) -> uvicorn.Config:
        return uvicorn.Config(
            app=self.app,
            host=Config.DASHBOARD_HOST,
            port=Config.DASHBOARD_PORT,
            # log_config=None keeps uvicorn from replacing the handlers that
            # fyrion.logging.logger installed on the root logger.
            log_config=None,
            access_log=Config.DASHBOARD_ACCESS_LOG,
            # Header sizes are bounded so a malformed request cannot allocate
            # unbounded memory.
            h11_max_incomplete_event_size=64 * 1024,
            timeout_graceful_shutdown=GRACEFUL_TIMEOUT_SECONDS,
            proxy_headers=Config.DASHBOARD_TRUST_PROXY,
            forwarded_allow_ips=(
                Config.DASHBOARD_FORWARDED_ALLOW_IPS
                if Config.DASHBOARD_TRUST_PROXY
                else None
            ),
            server_header=False,  # do not advertise the server version
            date_header=True,
            lifespan="on",
        )

    @property
    def is_serving(self) -> bool:
        return self._serving

    async def serve(self) -> None:
        """Serves until :meth:`stop` is called or the task is cancelled."""
        binding = f"{Config.DASHBOARD_HOST}:{Config.DASHBOARD_PORT}"
        if Config.DASHBOARD_HOST not in {"127.0.0.1", "localhost", "::1"}:
            # Worth stating plainly: the API is reachable off-host, so the
            # OAuth login and the reverse proxy in front of it are the only
            # things standing between the internet and guild configuration.
            log.warning(
                "Dashboard is binding to %s, which is reachable beyond this "
                "host. All /api routes require an authenticated Discord OAuth "
                "session with Manage Server on the target guild; serve it "
                "behind TLS and a reverse proxy.",
                binding,
            )
        else:
            log.info("Dashboard listening on %s (loopback only).", binding)

        self._serving = True
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            raise
        finally:
            self._serving = False
            log.info("Dashboard stopped serving.")

    async def stop(self) -> None:
        """Asks uvicorn to drain connections and shut down."""
        if not self._serving:
            self._server.should_exit = True
            return

        log.info(
            "Stopping the dashboard (graceful timeout %ds).", GRACEFUL_TIMEOUT_SECONDS
        )
        self._server.should_exit = True

        # Wait for the serve() task to observe should_exit; force the exit if it
        # overruns so shutdown cannot hang the process.
        deadline = GRACEFUL_TIMEOUT_SECONDS + 5
        waited = 0.0
        while self._serving and waited < deadline:
            await asyncio.sleep(0.1)
            waited += 0.1

        if self._serving:
            log.warning("Dashboard did not stop gracefully; forcing exit.")
            self._server.force_exit = True


__all__ = ["DashboardServer", "GRACEFUL_TIMEOUT_SECONDS"]
