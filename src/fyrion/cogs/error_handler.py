"""
Unified global error handling for Fyrion.

discord.py routes every uncaught exception raised inside an application command
(and inside autocomplete callbacks) to ``bot.tree.on_error``. This cog installs a
single handler there, so there is exactly one place that decides what a user
sees when something fails.

Design rules:

* **Always answer.** A failed command replies ephemerally with a clean embed
  rather than leaving the interaction hanging. The only exception is an
  interaction whose token has already expired, where no reply is possible.
* **Expected versus unexpected.** Cooldowns, missing permissions, failed checks
  and Discord refusals are part of normal operation: they get a short,
  actionable message and an ``INFO`` log line. Anything else is a defect: it
  gets a generic message plus a short reference id, and a full traceback in the
  log.
* **Never leak internals.** SQL text, filesystem paths, tracebacks and raw
  Discord payloads never reach Discord. The reference id is what ties a user's
  report to the log entry.
* **Reload safe.** The handler that was installed before this cog loaded is
  saved and restored on unload, so reloading the extension never leaves the
  command tree without an error handler.

The errors named in the specification are handled explicitly:
``CommandOnCooldown``, ``MissingPermissions``, ``BotMissingPermissions``,
``CheckFailure`` (last, since it is the base class of the others),
``discord.NotFound`` and ``discord.Forbidden``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.database.manager import DatabaseError, InsufficientFundsError

log = logging.getLogger("fyrion.cogs.error_handler")

# Discord invalidates an interaction token three seconds after it is created.
UNKNOWN_INTERACTION: Final[int] = 10062

COLOR_COOLDOWN: Final[discord.Color] = discord.Color.orange()
COLOR_DENIED: Final[discord.Color] = discord.Color.red()
COLOR_INPUT: Final[discord.Color] = discord.Color.gold()
COLOR_FAILURE: Final[discord.Color] = discord.Color.dark_red()

GENERIC_TITLE: Final[str] = "\u274c Something went wrong"
GENERIC_BODY: Final[str] = (
    "That command could not be completed. The failure has been logged for this "
    "server's operator."
)


@dataclass(frozen=True)
class ErrorResponse:
    """What the user is told about a failure.

    ``expected`` marks conditions that are part of normal operation. They are
    logged at ``INFO`` without a traceback and carry no reference id.
    """

    title: str
    description: str
    color: discord.Color
    expected: bool = True
    footer: str | None = None


def format_permissions(names: Sequence[Any]) -> str:
    """Renders a permission list the way Discord's own UI labels them."""
    if not names:
        return "`Unknown`"
    return ", ".join(f"`{str(name).replace('_', ' ').title()}`" for name in names)


def format_roles(names: Sequence[Any]) -> str:
    """Renders a role requirement list, tolerating ids and names alike."""
    rendered: list[str] = []
    for name in names:
        if isinstance(name, int):
            rendered.append(f"<@&{name}>")
        else:
            # Role names are operator supplied, so keep them inert.
            rendered.append(f"`{str(name)[:60]}`")
    return ", ".join(rendered) or "`unknown`"


def describe_check_failure(error: BaseException) -> ErrorResponse:
    """Classifies a library-level failure raised before or around a command."""
    if isinstance(error, app_commands.CommandOnCooldown):
        retry = max(0.0, float(error.retry_after))
        moment = discord.utils.utcnow() + timedelta(seconds=retry)
        return ErrorResponse(
            title="\u23f3 Slow down",
            description=(
                "That command is on cooldown. You can use it again "
                f"{discord.utils.format_dt(moment, style='R')}."
            ),
            color=COLOR_COOLDOWN,
            footer=f"Retry in about {retry:.1f}s.",
        )

    if isinstance(error, app_commands.MissingPermissions):
        return ErrorResponse(
            title="\U0001f6ab Missing permission",
            description=(
                "You need the following permission(s) to use that command: "
                f"{format_permissions(error.missing_permissions)}."
            ),
            color=COLOR_DENIED,
        )

    if isinstance(error, app_commands.BotMissingPermissions):
        return ErrorResponse(
            title="\u26a0\ufe0f I am missing permission",
            description=(
                "I need the following permission(s) before I can do that: "
                f"{format_permissions(error.missing_permissions)}."
            ),
            color=COLOR_DENIED,
            footer="Grant them in Server Settings, then try again.",
        )

    if isinstance(error, app_commands.NoPrivateMessage):
        return ErrorResponse(
            title="\u274c Server only",
            description="That command only works inside a server, not in direct messages.",
            color=COLOR_DENIED,
        )

    if isinstance(error, app_commands.MissingRole):
        return ErrorResponse(
            title="\U0001f6ab Missing role",
            description=(
                "That command requires the "
                f"{format_roles([error.missing_role])} role."
            ),
            color=COLOR_DENIED,
        )

    if isinstance(error, app_commands.MissingAnyRole):
        return ErrorResponse(
            title="\U0001f6ab Missing role",
            description=(
                "That command requires one of these roles: "
                f"{format_roles(list(error.missing_roles))}."
            ),
            color=COLOR_DENIED,
        )

    if isinstance(error, app_commands.TransformerError):
        return ErrorResponse(
            title="\u274c Invalid argument",
            description=(
                "I could not read one of the values you supplied. Please check "
                "the argument and try again."
            ),
            color=COLOR_INPUT,
        )

    if isinstance(error, app_commands.CommandSignatureMismatch):
        return ErrorResponse(
            title="\u267b\ufe0f Command out of date",
            description=(
                "That command has changed since Discord last refreshed it. Wait "
                "a moment, then try again."
            ),
            color=COLOR_INPUT,
        )

    if isinstance(error, app_commands.CommandNotFound):
        return ErrorResponse(
            title="\u2753 Unknown command",
            description=(
                "That command no longer exists. It will disappear from the menu "
                "once Discord refreshes its command list."
            ),
            color=COLOR_INPUT,
        )

    # Custom checks raise a plain CheckFailure, so this must stay last among the
    # check branches.
    if isinstance(error, app_commands.CheckFailure):
        return ErrorResponse(
            title="\U0001f6ab Not allowed",
            description="You are not allowed to use that command here.",
            color=COLOR_DENIED,
        )

    return ErrorResponse(
        title=GENERIC_TITLE,
        description=GENERIC_BODY,
        color=COLOR_FAILURE,
        expected=False,
    )


def describe_original(original: BaseException) -> ErrorResponse | None:
    """Classifies an exception raised inside a command body.

    Returns ``None`` when the exception is not one of the recognised cases, so
    the caller can fall back to the check-level classification.
    """
    if isinstance(original, discord.Forbidden):
        return ErrorResponse(
            title="\U0001f6ab Discord refused that action",
            description=(
                "Discord rejected the request. Check my permissions in that "
                "channel and my position in the role hierarchy."
            ),
            color=COLOR_DENIED,
        )

    if isinstance(original, discord.NotFound):
        return ErrorResponse(
            title="\u274c Not found",
            description=(
                "The target of that command no longer exists on Discord. It may "
                "have been deleted while the command was running."
            ),
            color=COLOR_INPUT,
        )

    if isinstance(original, InsufficientFundsError):
        return ErrorResponse(
            title="\u274c Not enough funds",
            description="That account does not have enough to cover the amount.",
            color=COLOR_INPUT,
        )

    if isinstance(original, sqlite3.OperationalError):
        # Usually "database is locked" under write contention.
        return ErrorResponse(
            title="\u274c Storage is busy",
            description=(
                "The database is busy right now. Please try that again in a " "moment."
            ),
            color=COLOR_FAILURE,
            expected=False,
        )

    if isinstance(original, (sqlite3.Error, DatabaseError)):
        return ErrorResponse(
            title="\u274c Storage error",
            description=(
                "A database error occurred, so nothing was changed. The failure "
                "has been logged."
            ),
            color=COLOR_FAILURE,
            expected=False,
        )

    if isinstance(original, asyncio.TimeoutError):
        return ErrorResponse(
            title="\u23f3 Timed out",
            description="That operation took too long. Please try again.",
            color=COLOR_FAILURE,
            expected=False,
        )

    if isinstance(original, discord.RateLimited):
        return ErrorResponse(
            title="\u23f3 Rate limited",
            description=(
                "Discord is rate limiting Fyrion. Please try again in a few " "seconds."
            ),
            color=COLOR_COOLDOWN,
        )

    if isinstance(original, discord.HTTPException):
        # Status only: the raw payload can carry request details.
        return ErrorResponse(
            title="\u274c Discord rejected the request",
            description=f"Discord replied with HTTP {original.status}.",
            color=COLOR_FAILURE,
            expected=False,
        )

    return None


def build_embed(response: ErrorResponse, reference: str | None) -> discord.Embed:
    """Renders an error response as a compact embed."""
    embed = discord.Embed(
        title=response.title,
        description=response.description,
        color=response.color,
        timestamp=discord.utils.utcnow(),
    )
    if reference is not None:
        embed.set_footer(text=f"Reference: {reference}")
    elif response.footer:
        embed.set_footer(text=response.footer)
    return embed


class ErrorHandler(commands.Cog):
    """Centralized, ephemeral error reporting for every command surface."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Save whatever handler the tree already had so cog_unload can put it
        # back and never leave the tree unguarded across a reload.
        self._previous_handler = bot.tree.on_error
        bot.tree.on_error = self.on_app_command_error  # type: ignore[method-assign]

    async def cog_unload(self) -> None:
        self.bot.tree.on_error = self._previous_handler  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def command_name(interaction: discord.Interaction) -> str:
        command = interaction.command
        if command is None:
            return "unknown"
        return getattr(command, "qualified_name", getattr(command, "name", "unknown"))

    @staticmethod
    def describe(error: BaseException) -> tuple[ErrorResponse, BaseException]:
        """Returns the response for an error plus the exception worth logging."""
        original = getattr(error, "original", None)
        if original is not None:
            described = describe_original(original)
            if described is not None:
                return described, original
            return (
                ErrorResponse(
                    title=GENERIC_TITLE,
                    description=GENERIC_BODY,
                    color=COLOR_FAILURE,
                    expected=False,
                ),
                original,
            )
        return describe_check_failure(error), error

    async def _reply(
        self, interaction: discord.Interaction, embed: discord.Embed
    ) -> None:
        """Answers ephemerally whether or not the interaction was deferred."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.NotFound:
            # The token expired; there is nothing left to reply to.
            log.debug("Could not report an error: the interaction was already dead.")
        except discord.HTTPException as exc:
            log.warning("Failed to deliver an error embed to the user: %s", exc)

    # ------------------------------------------------------------------
    # Application commands
    # ------------------------------------------------------------------

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        command = self.command_name(interaction)
        original = getattr(error, "original", None)

        # 10062 means the interaction expired before it was acknowledged. The
        # token is dead, so there is no way to answer the user at all.
        if (
            isinstance(original, discord.NotFound)
            and original.code == UNKNOWN_INTERACTION
        ):
            log.debug(
                "Interaction for %s expired before acknowledgement (user %s).",
                command,
                interaction.user.id,
            )
            return

        response, logged = self.describe(error)
        reference: str | None = None

        if response.expected:
            log.info(
                "Command %s refused for user %s in guild %s: %s",
                command,
                interaction.user.id,
                interaction.guild_id,
                logged,
            )
        else:
            # A short reference lets a user quote the failure without exposing
            # any internal detail.
            reference = uuid.uuid4().hex[:8]
            log.error(
                "Command %s failed for user %s in guild %s (reference %s): %s",
                command,
                interaction.user.id,
                interaction.guild_id,
                reference,
                logged,
                exc_info=logged,
            )

        await self._reply(interaction, build_embed(response, reference))

    # ------------------------------------------------------------------
    # Prefix commands
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        """Records prefix command failures.

        Fyrion exposes no prefix commands, so this only logs. Replying here
        would risk a duplicate message, because a bot-level
        ``on_command_error`` may also be installed.
        """
        if isinstance(
            error,
            (
                commands.CommandNotFound,
                commands.CheckFailure,
                commands.NoPrivateMessage,
                commands.CommandOnCooldown,
            ),
        ):
            log.debug("Prefix command refused: %s", error)
            return

        log.error("Prefix command error: %s", error, exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ErrorHandler(bot))


__all__ = [
    "ErrorHandler",
    "ErrorResponse",
    "UNKNOWN_INTERACTION",
    "GENERIC_BODY",
    "GENERIC_TITLE",
    "build_embed",
    "describe_check_failure",
    "describe_original",
    "format_permissions",
    "format_roles",
    "setup",
]
