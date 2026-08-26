"""
Discord permission and hierarchy validation utilities.

Discord's ``default_permissions`` only decides whether a client *shows* a
command, so it is never treated as an authorization decision here. Every helper
in this module answers the same question server-side: given the invoker, the
bot's own member object and the target, is this action allowed by Discord's role
hierarchy and by Fyrion's own safety rules?

Each function returns a user-facing error string when the action must be
refused, or ``None`` when it is permitted. Returning the reason (rather than a
bare boolean) keeps the refusal messages consistent across every cog.
"""
from __future__ import annotations

import discord


def can_moderate(invoker: discord.Member, target: discord.Member) -> str | None:
    """
    Validates if the invoker and the bot have the hierarchical authority to
    moderate the target.

    Returns:
        str: An error message if the action is forbidden.
        None: If the action is permitted.
    """
    guild = invoker.guild
    bot_member = guild.me

    if bot_member is None:
        return "I could not resolve my own membership in this server."

    if target.id == bot_member.id:
        return "I cannot moderate myself."

    if target.id == invoker.id:
        return "You cannot moderate yourself."

    if target.id == guild.owner_id:
        return "The server owner cannot be moderated."

    # Bypass hierarchy check if the invoker is the server owner
    if invoker.id != guild.owner_id:
        if invoker.top_role <= target.top_role:
            return (
                "You cannot moderate a member with an equal or higher role than "
                "yourself."
            )

    # The bot must also strictly obey the hierarchy
    if bot_member.top_role <= target.top_role:
        return (
            "I cannot moderate a member with an equal or higher role than my "
            "highest role."
        )

    return None


def can_manage_role(invoker: discord.Member, role: discord.Role) -> str | None:
    """Validates that a role may be created, edited, assigned or deleted.

    Rules, in the order they are checked:

    * ``@everyone`` and Discord-managed roles (bot roles, integration roles,
      the booster role) can never be touched through Fyrion.
    * The bot needs ``Manage Roles`` and must rank strictly above the role,
      otherwise Discord refuses the edit anyway.
    * The invoker needs ``Manage Roles`` and must rank strictly above the role,
      so staff cannot hand out roles they could not grant themselves.
    * A role carrying ``Administrator`` may only be handled by an administrator
      or the owner: otherwise the bot becomes a privilege-escalation path.
    """
    guild = invoker.guild
    me = guild.me

    if role.is_default():
        return "The `@everyone` role cannot be managed."
    if role.managed:
        return (
            f"**{role.name}** is managed by Discord (a bot, integration or "
            "booster role) and cannot be changed."
        )

    if me is None:
        return "I could not resolve my own membership in this server."
    if not me.guild_permissions.manage_roles:
        return "I need the `Manage Roles` permission for that."
    if me.top_role <= role:
        return (
            f"**{role.name}** is equal to or above my highest role, so Discord "
            "will not let me manage it. Move my role higher in Server "
            "Settings > Roles."
        )

    if invoker.id == guild.owner_id:
        return None

    if not invoker.guild_permissions.manage_roles:
        return "You need the `Manage Roles` permission for that."
    if invoker.top_role <= role:
        return (
            f"**{role.name}** is equal to or above your highest role, so you "
            "cannot manage it."
        )
    if role.permissions.administrator and not invoker.guild_permissions.administrator:
        return (
            f"**{role.name}** grants `Administrator`. Only an administrator or "
            "the server owner may manage it."
        )

    return None


def can_manage_member_roles(
    invoker: discord.Member, target: discord.Member
) -> str | None:
    """Validates that the invoker may change ``target``'s roles.

    Editing a member's roles is a privileged action, so it obeys the same
    hierarchy rules as moderation: the invoker must outrank the target, and so
    must the bot. Members are allowed to change their *own* roles because the
    role itself is still validated by :func:`can_manage_role`.
    """
    guild = invoker.guild
    me = guild.me

    if me is None:
        return "I could not resolve my own membership in this server."

    if target.id == me.id:
        return "I cannot change my own roles."

    if me.top_role <= target.top_role:
        return (
            f"**{target}** has a role equal to or above mine, so Discord will "
            "not let me change their roles."
        )

    if invoker.id == guild.owner_id or target.id == invoker.id:
        return None

    if target.id == guild.owner_id:
        return "The server owner's roles cannot be changed through Fyrion."

    if invoker.top_role <= target.top_role:
        return (
            f"**{target}** has a role equal to or above yours, so you cannot "
            "change their roles."
        )

    return None


def missing_channel_permissions(
    member: discord.Member, channel: discord.abc.GuildChannel, **required: bool
) -> list[str]:
    """Returns the human-readable names of the permissions ``member`` lacks.

    Channel overwrites can grant or revoke a permission independently of the
    guild-level value, so effective channel permissions are what callers must
    check before acting on a specific channel.
    """
    effective = channel.permissions_for(member)
    missing: list[str] = []
    for name, needed in required.items():
        if needed and not getattr(effective, name, False):
            missing.append(name.replace("_", " ").title())
    return missing


__all__ = [
    "can_moderate",
    "can_manage_role",
    "can_manage_member_roles",
    "missing_channel_permissions",
]
