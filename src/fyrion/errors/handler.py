"""
Centralized error handling for Discord application commands.
"""
import logging
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("fyrion.errors")

def setup_error_handlers(bot: commands.Bot) -> None:
    """Attaches error handlers to the bot instance."""
    
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Catches and handles errors triggered by slash commands."""
        
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f}s."
            await _safe_reply(interaction, msg)
            
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "🚫 You lack the required permissions to execute this action."
            await _safe_reply(interaction, msg)
            
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "⚠️ I am missing necessary permissions to perform this action."
            log.warning(f"Missing permissions in guild {interaction.guild_id}: {error.missing_permissions}")
            await _safe_reply(interaction, msg)
            
        elif isinstance(error, app_commands.TransformerError):
            msg = "❌ Failed to parse argument. Please check your input."
            await _safe_reply(interaction, msg)
            
        elif isinstance(error, app_commands.CommandInvokeError):
            # Unwrap the original exception raised inside the command
            original = error.original
            
            # 10062: Unknown Interaction - occurs when network lag causes the interaction to expire (3s window)
            if isinstance(original, discord.NotFound) and original.code == 10062:
                log.debug(f"Interaction expired before acknowledgment for user {interaction.user.id}.")
                return
                
            # Gracefully catch forbidden API calls inside commands
            if isinstance(original, discord.Forbidden):
                await _safe_reply(interaction, "🚫 Discord rejected this action (Missing Permissions or Hierarchy).")
                return
                
            log.error(f"Command '{interaction.command.name if interaction.command else 'Unknown'}' failed: {original}", exc_info=original)
            await _safe_reply(interaction, "❌ An unexpected database or API error occurred.")
            
        else:
            log.error(f"Unhandled error type {type(error)}: {error}", exc_info=error)
            await _safe_reply(interaction, "❌ An unexpected error occurred while processing this command.")

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        """Catches text-based prefix command errors (if enabled)."""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore invalid commands silently
        log.error(f"Unhandled prefix command error: {error}")

async def _safe_reply(interaction: discord.Interaction, message: str) -> None:
    """Ensures a response is sent whether the interaction was already deferred or not."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.NotFound:
        # The interaction token has completely died, we cannot reply anymore
        log.debug("Attempted to send _safe_reply but the interaction was dead.")
    except discord.HTTPException as e:
        log.error(f"Failed to send error response to user: {e}")
