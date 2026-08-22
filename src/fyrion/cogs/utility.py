"""
Utility and information commands.
"""
import discord
from discord import app_commands
from discord.ext import commands
import sys
import platform

from fyrion.bot import Fyrion

class Utility(commands.Cog):
    """General utility commands."""
    
    def __init__(self, bot: Fyrion) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's network latency.")
    async def ping_cmd(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: `{latency}ms`", ephemeral=True)

    @app_commands.command(name="uptime", description="Check how long Fyrion has been online.")
    async def uptime_cmd(self, interaction: discord.Interaction) -> None:
        # discord.utils.format_dt formats the datetime into a native Discord relative timestamp (e.g., "2 days ago")
        formatted_time = discord.utils.format_dt(self.bot.boot_time, style="R")
        await interaction.response.send_message(f"⏱️ Online since {formatted_time}.", ephemeral=True)

    @app_commands.command(name="avatar", description="View a member's avatar.")
    @app_commands.describe(member="The member to view")
    async def avatar_cmd(self, interaction: discord.Interaction, member: discord.Member = None) -> None:
        target = member or interaction.user
        
        embed = discord.Embed(title=f"{target.display_name}'s Avatar", color=discord.Color.blurple())
        embed.set_image(url=target.display_avatar.url if target.display_avatar else None)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="View information about a member.")
    async def userinfo_cmd(self, interaction: discord.Interaction, member: discord.Member = None) -> None:
        target = member or interaction.user
        
        embed = discord.Embed(title=f"User Info: {target.name}", color=target.color)
        embed.set_thumbnail(url=target.display_avatar.url if target.display_avatar else None)
        
        embed.add_field(name="ID", value=target.id, inline=True)
        embed.add_field(name="Top Role", value=target.top_role.mention, inline=True)
        embed.add_field(name="Bot?", value="Yes" if target.bot else "No", inline=True)
        
        created = discord.utils.format_dt(target.created_at, style="F")
        joined = discord.utils.format_dt(target.joined_at, style="F") if target.joined_at else "Unknown"
        
        embed.add_field(name="Account Created", value=created, inline=False)
        embed.add_field(name="Joined Server", value=joined, inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="View information about this server.")
    async def serverinfo_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        
        embed = discord.Embed(title=f"Server Info: {guild.name}", color=discord.Color.blurple())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
        embed.add_field(name="ID", value=guild.id, inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Text Channels", value=str(len(guild.text_channels)), inline=True)
        embed.add_field(name="Voice Channels", value=str(len(guild.voice_channels)), inline=True)
        
        created = discord.utils.format_dt(guild.created_at, style="F")
        embed.add_field(name="Created On", value=created, inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="botinfo", description="View Fyrion's system information.")
    async def botinfo_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Fyrion System Information", 
            description="Powerful tools for better Discord communities.",
            color=discord.Color.gold()
        )
        embed.add_field(name="Developer", value="Fyrion Open Source", inline=True)
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        
        embed.add_field(name="Python", value=platform.python_version(), inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        
        await interaction.response.send_message(embed=embed)

async def setup(bot: Fyrion) -> None:
    await bot.add_cog(Utility(bot))
