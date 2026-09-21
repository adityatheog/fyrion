"""
Process supervisor for Fyrion.

Responsibilities, in order:

1. validate configuration before anything touches the network or the disk,
2. configure structured, rotating logging,
3. construct the ``AutoShardedBot`` and (optionally) the FastAPI dashboard,
4. run both concurrently under one ``asyncio.gather``,
5. shut both down in the right order on a signal, a gateway exit, or a crash.

Shutdown design
---------------
Every long-lived task sets a shared ``stop_event`` when it finishes, and a small
watchdog task waits on that event to close the bot and stop the HTTP server.
This gives one drain path for all three shutdown triggers (SIGTERM, gateway
exit, unhandled exception) instead of three independent ones.

Exit codes:
    0  clean shutdown
    1  a component failed
    2  the configuration is unusable
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import discord

from fyrion.bot import Fyrion, discover_extensions
from fyrion.config import Config, ConfigurationError
from fyrion.logging.logger import setup_logging

log = logging.getLogger("fyrion.runtime")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


# ---------------------------------------------------------------------------
# Component construction
# ---------------------------------------------------------------------------


def build_bot() -> Fyrion:
    """Creates the gateway client. Intents are declared in ``fyrion.bot``."""
    return Fyrion()


def build_dashboard(bot: Fyrion) -> Any | None:
    """Creates the dashboard server, or None when it is disabled.

    FastAPI and uvicorn are imported lazily so a deployment that never enables
    the dashboard does not need them installed at all.
    """
    if not Config.DASHBOARD_ENABLED:
        log.info("Web dashboard disabled (set DASHBOARD_ENABLED=true to serve it).")
        return None

    try:
        from fyrion.web.server import DashboardServer
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConfigurationError(
            "DASHBOARD_ENABLED is true but the web dependencies are missing. "
            "Install them with: pip install -r requirements.txt"
        ) from exc

    return DashboardServer(bot)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> None:
    """Routes SIGINT/SIGTERM into the shared stop event.

    Container runtimes send SIGTERM, so handling it is what turns
    ``docker stop`` into a graceful gateway logout and a clean SQLite close
    rather than a hard kill.
    """
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows event loops do not implement add_signal_handler; the
            # KeyboardInterrupt path in main() covers Ctrl+C there.
            log.debug("%s cannot be handled on this platform.", name)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


async def _run_bot(bot: Fyrion, token: str, stop_event: asyncio.Event) -> None:
    try:
        await bot.start(token)
    finally:
        # The gateway session ended (cleanly or not): drain everything else.
        stop_event.set()


async def _run_dashboard(dashboard: Any, stop_event: asyncio.Event) -> None:
    try:
        await dashboard.serve()
    finally:
        stop_event.set()


async def _await_shutdown(
    bot: Fyrion, dashboard: Any | None, stop_event: asyncio.Event
) -> None:
    """Stops the HTTP server first, then the gateway client.

    Order matters: the dashboard reads through ``bot.db``, so it must stop
    serving requests before the database pool is closed by ``bot.close()``.
    """
    await stop_event.wait()
    log.info("Shutdown requested; stopping Fyrion components.")

    if dashboard is not None:
        try:
            await dashboard.stop()
        except Exception:
            log.exception("Error while stopping the web dashboard.")

    if not bot.is_closed():
        try:
            await bot.close()
        except Exception:
            log.exception("Error while closing the gateway client.")


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def _report_failure(component: str, error: BaseException) -> None:
    """Logs a component failure with an actionable message where possible."""
    if isinstance(error, discord.LoginFailure):
        log.critical("Login failed: the configured DISCORD_TOKEN was rejected.")
        return

    if isinstance(error, discord.PrivilegedIntentsRequired):
        log.critical(
            "The gateway rejected the connection. Enable the Server Members "
            "and Message Content intents for this application in the Discord "
            "Developer Portal."
        )
        return

    if isinstance(error, OSError):
        log.critical(
            "%s could not bind its socket: %s. Check DASHBOARD_HOST/DASHBOARD_PORT.",
            component,
            error,
        )
        return

    log.critical("%s failed: %s", component, error, exc_info=error)


async def supervise(bot: Fyrion, dashboard: Any | None) -> int:
    """Runs the bot and the dashboard concurrently until one of them stops."""
    token = Config.DISCORD_TOKEN
    if not token:  # defensive: Config.validate() already enforces this
        raise ConfigurationError("DISCORD_TOKEN is not configured.")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    install_signal_handlers(loop, stop_event)

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_run_bot(bot, token, stop_event), name="gateway client")
    ]
    if dashboard is not None:
        tasks.append(
            asyncio.create_task(
                _run_dashboard(dashboard, stop_event), name="web dashboard"
            )
        )
    tasks.append(
        asyncio.create_task(
            _await_shutdown(bot, dashboard, stop_event), name="shutdown watchdog"
        )
    )

    # return_exceptions keeps one crashing component from cancelling the others
    # mid-drain; failures are inspected once every task has finished.
    results = await asyncio.gather(*tasks, return_exceptions=True)

    exit_code = EXIT_OK
    for task, result in zip(tasks, results):
        if not isinstance(result, BaseException):
            continue
        if isinstance(result, asyncio.CancelledError):
            continue
        exit_code = EXIT_FAILURE
        _report_failure(task.get_name(), result)

    # Belt and braces: the watchdog normally does this, but a crash during
    # startup can bypass it.
    if not bot.is_closed():
        try:
            await bot.close()
        except Exception:
            log.exception("Error during final gateway teardown.")

    log.info("Fyrion stopped with exit code %d.", exit_code)
    return exit_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Validates configuration, configures logging and runs the supervisor."""
    try:
        Config.validate()
    except ConfigurationError as exc:
        # The logger is not configured yet, so report on stderr directly.
        sys.stderr.write(f"CONFIGURATION ERROR:\n{exc}\n")
        return EXIT_CONFIG

    setup_logging()

    log.info(
        "Fyrion %s starting (environment=%s, python=%s).",
        Config.VERSION,
        Config.ENVIRONMENT,
        ".".join(str(part) for part in sys.version_info[:3]),
    )
    for key, value in Config.summary().items():
        log.debug("config %s=%s", key, value)

    try:
        extensions = discover_extensions()
    except Exception:
        log.exception("Could not enumerate cog modules.")
        extensions = []
    log.info(
        "Discovered %d cog module(s): %s",
        len(extensions),
        ", ".join(name.rsplit(".", 1)[-1] for name in extensions) or "none",
    )

    bot = build_bot()

    try:
        dashboard = build_dashboard(bot)
    except ConfigurationError as exc:
        log.critical("%s", exc)
        return EXIT_CONFIG

    try:
        return asyncio.run(supervise(bot, dashboard))
    except KeyboardInterrupt:
        log.info("Interrupt received; Fyrion stopped.")
        return EXIT_OK
    except ConfigurationError as exc:
        log.critical("%s", exc)
        return EXIT_CONFIG
    except Exception:
        log.critical("Fatal error outside the supervised tasks.", exc_info=True)
        return EXIT_FAILURE


__all__ = [
    "build_bot",
    "build_dashboard",
    "install_signal_handlers",
    "supervise",
    "main",
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_CONFIG",
]
