"""
Discord permission and hierarchy validation utilities.
"""
import discord

def can_moderate(invoker: discord.Member, target: discord.Member) -> str | None:
    """
    Validates if the invoker and the bot have the hierarchical authority to moderate the target.
    
    Returns:
        str: An error message if the action is forbidden.
        None: If the action is permitted.
    """
    guild = invoker.guild
    bot_member = guild.me

    if target.id == bot_member.id:
        return "I cannot moderate myself."
        
    if target.id == invoker.id:
        return "You cannot moderate yourself."
        
    if target.id == guild.owner_id:
        return "The server owner cannot be moderated."

    # Bypass hierarchy check if the invoker is the server owner
    if invoker.id != guild.owner_id:
        if invoker.top_role <= target.top_role:
            return "You cannot moderate a member with an equal or higher role than yourself."

    # The bot must also strictly obey the hierarchy
    if bot_member.top_role <= target.top_role:
        return "I cannot moderate a member with an equal or higher role than my highest role."

    return None
