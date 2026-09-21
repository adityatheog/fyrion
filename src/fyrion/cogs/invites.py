"""
Invite tracking and statistics cog.
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands

from fyrion.bot import Fyrion
from fyrion.database.repositories.invites import InviteRepository

log = logging.getLogger("fyrion.cogs.invites")


class Invites(commands.Cog):
    """Tracks invites and calculates user statistics."""

    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot
        self.repo = InviteRepository(bot.db)
        # Structure: {guild_id: {invite_code: uses}}
        self.cache: dict[int, dict[str, int]] = {}

    async def update_cache_for_guild(self, guild: discord.Guild) -> None:
        """Fetches and caches all invites for a guild."""
        try:
            invites = await guild.invites()
            self.cache[guild.id] = {
                invite.code: invite.uses
                for invite in invites
                if invite.uses is not None
            }
        except discord.Forbidden:
            log.debug(
                f"Missing 'Manage Guild' permissions in {guild.id}. Cannot track invites."
            )
        except discord.HTTPException as e:
            log.warning(f"Failed to fetch invites for {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Prime the cache when the bot boots."""
        log.info("Priming invite cache...")
        for guild in self.bot.guilds:
            await self.update_cache_for_guild(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Prime the cache when joining a new server."""
        await self.update_cache_for_guild(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild and isinstance(invite.guild, discord.Guild):
            if invite.guild.id not in self.cache:
                self.cache[invite.guild.id] = {}
            self.cache[invite.guild.id][invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild and invite.guild.id in self.cache:
            self.cache[invite.guild.id].pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if guild.id not in self.cache:
            return

        old_invites = self.cache.get(guild.id, {})

        try:
            new_invites = await guild.invites()
        except discord.Forbidden:
            return

        used_invite = None
        for invite in new_invites:
            old_uses = old_invites.get(invite.code, 0)
            if invite.uses and invite.uses > old_uses:
                used_invite = invite
                break

        # Update cache for next join
        self.cache[guild.id] = {
            inv.code: inv.uses for inv in new_invites if inv.uses is not None
        }

        # Credit the inviter
        if used_invite and used_invite.inviter:
            await self.repo.add_join(guild.id, member.id, used_invite.inviter.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Process leaves to calculate net invites."""
        await self.repo.add_leave(member.guild.id, member.id)

    @app_commands.command(
        name="invites", description="Check how many members someone has invited."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to check (defaults to yourself)")
    async def invites_cmd(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        target = member or interaction.user

        stats = await self.repo.get_stats(interaction.guild_id, target.id)

        embed = discord.Embed(
            title=f"Invites for {target.display_name}", color=discord.Color.teal()
        )
        embed.set_thumbnail(
            url=target.display_avatar.url if target.display_avatar else None
        )
        embed.add_field(name="Total Joins", value=f"✅ {stats['joins']}", inline=True)
        embed.add_field(name="Total Leaves", value=f"❌ {stats['leaves']}", inline=True)
        embed.add_field(
            name="Net Invites", value=f"📊 **{stats['net']}**", inline=False
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: Fyrion) -> None:
    await bot.add_cog(Invites(bot))
