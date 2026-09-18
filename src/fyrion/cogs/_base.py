"""
Shared cog base.

Every cog wires ``self.db = bot.db`` and re-implements the same trio of reply
helpers: a defer-aware ``_respond``, a red-cross ``_reject`` and a green-check
``_ok``, plus the odd embed sender. ``FyrionCog`` factors those out so a cog only
carries the logic that is actually specific to it.

This module is deliberately underscore-prefixed: :func:`fyrion.bot.discover_extensions`
skips ``_``-prefixed modules, so ``FyrionCog`` lives beside the real cogs without
ever being loaded as an extension (it defines no ``setup`` and holds no commands).

``FyrionCog`` is a plain mixin -- it does **not** inherit from ``commands.Cog`` --
so it composes with both ``commands.Cog`` and ``commands.GroupCog`` subclasses
without disturbing discord.py's cog metaclass or the group a ``GroupCog`` builds
from its ``name=`` keyword.
"""
from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger("fyrion.cogs")

# Replies never parse mentions: user-controlled text (names, reasons, topics)
# echoed back must not be able to ping a role or ``@everyone``.
NO_MENTIONS = discord.AllowedMentions.none()


class FyrionCog:
    """Mixin supplying ``self.db`` and the shared ephemeral reply helpers.

    Combine with ``commands.Cog`` or ``commands.GroupCog`` in that order, e.g.::

        class Foo(FyrionCog, commands.Cog): ...
        class Bar(FyrionCog, commands.GroupCog, name="bar"): ...

    Subclasses that want their plain ``_respond`` to default to a public
    (non-ephemeral) reply set ``DEFAULT_EPHEMERAL = False`` at class level;
    ``_reject`` and ``_ok`` stay ephemeral regardless.
    """

    #: Default visibility for :meth:`_respond` when no ``ephemeral`` is passed.
    DEFAULT_EPHEMERAL: bool = True

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.db: Any = bot.db

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    async def _respond(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool | None = None,
    ) -> None:
        """Replies once, whether or not the interaction was already deferred.

        An interaction that expired before it could be answered is logged and
        swallowed rather than raised into the global error handler.
        """
        if ephemeral is None:
            ephemeral = self.DEFAULT_EPHEMERAL
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
                )
            else:
                await interaction.response.send_message(
                    message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
                )
        except discord.NotFound:
            log.debug("An interaction expired before it was answered.")
        except discord.HTTPException as exc:
            log.warning("Could not answer an interaction: %s", exc)

    async def _send_embed(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        *,
        ephemeral: bool = False,
        mentions: discord.AllowedMentions = NO_MENTIONS,
    ) -> None:
        """Sends an embed once, defer-aware, tolerating an expired interaction."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed, ephemeral=ephemeral, allowed_mentions=mentions
                )
            else:
                await interaction.response.send_message(
                    embed=embed, ephemeral=ephemeral, allowed_mentions=mentions
                )
        except discord.NotFound:
            log.debug("An interaction expired before it was answered.")
        except discord.HTTPException as exc:
            log.warning("Could not answer an interaction: %s", exc)

    async def _reject(self, interaction: discord.Interaction, reason: str) -> None:
        """Ephemeral red-cross refusal."""
        await self._respond(interaction, f"❌ {reason}", ephemeral=True)

    async def _ok(self, interaction: discord.Interaction, message: str) -> None:
        """Ephemeral green-check confirmation."""
        await self._respond(interaction, f"✅ {message}", ephemeral=True)

    async def _note(self, interaction: discord.Interaction, reason: str) -> None:
        """Ephemeral information reply."""
        await self._respond(interaction, f"ℹ️ {reason}", ephemeral=True)


__all__ = ["FyrionCog", "NO_MENTIONS"]
