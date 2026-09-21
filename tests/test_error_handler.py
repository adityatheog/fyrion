"""
Tests for the global command error handler cog.

Phase 4 collapsed error handling to a single tree: the ``ErrorHandler`` cog is
now the only thing that assigns ``bot.tree.on_error``. These tests confirm the
install/restore contract without needing a live gateway connection.
"""

import discord
import pytest
from discord.ext import commands

from fyrion.cogs.error_handler import ErrorHandler


@pytest.fixture
def bot():
    """A minimal bot with a real command tree but no gateway connection."""
    intents = discord.Intents.none()
    instance = commands.Bot(command_prefix="!", intents=intents)
    return instance


@pytest.mark.asyncio
async def test_loading_installs_handler_and_unloading_restores(bot):
    """Loading the cog claims tree.on_error; unloading puts the old one back."""
    original_handler = bot.tree.on_error

    await bot.add_cog(ErrorHandler(bot))

    cog = bot.get_cog("ErrorHandler")
    assert cog is not None
    # Bound methods are recreated on each access, so compare with ==, not is.
    assert bot.tree.on_error == cog.on_app_command_error
    assert bot.tree.on_error != original_handler

    await bot.remove_cog("ErrorHandler")

    assert bot.tree.on_error == original_handler


@pytest.mark.asyncio
async def test_handler_preserves_a_custom_prior_handler(bot):
    """A handler installed before the cog loads is restored on unload."""

    async def custom_handler(interaction, error):  # pragma: no cover - never called
        return None

    bot.tree.on_error = custom_handler  # type: ignore[method-assign]

    await bot.add_cog(ErrorHandler(bot))
    assert bot.tree.on_error is not custom_handler

    await bot.remove_cog("ErrorHandler")
    assert bot.tree.on_error is custom_handler
