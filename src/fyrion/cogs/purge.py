"""
Bulk message deletion.

Every command here is a thin wrapper around one shared executor,
:meth:`Purge._execute`, which owns the parts that must never differ between
filters:

* **Authorization.** ``default_permissions`` only controls whether Discord shows
  a command, so it is never the gate. The executor re-checks, server side, that
  the invoker holds ``Manage Messages`` *and* ``Read Message History`` in the
  target channel, and that Fyrion holds them too.
* **Bounded work.** ``amount`` is the number of *matching* messages to delete;
  the executor stops scanning after ``amount * SCAN_MULTIPLIER`` messages (hard
  capped by :data:`MAX_SCAN`), so a filter that matches nothing cannot walk a
  channel's entire history.
* **Pinned safety.** Pinned messages are skipped unless the invoker opts in.
* **Auditability.** Every purge is mirrored to the guild's moderation log
  channel, and a targeted purge is recorded as a ``purge`` moderation case.

Discord's bulk-delete endpoint refuses messages older than 14 days. discord.py's
``purge`` handles that transparently: newer messages go out in bulk, older ones
are deleted individually. The executor therefore reports what was actually
deleted rather than what was requested.

Replies are ephemeral. A purge confirmation posted publicly is itself channel
noise, and the deleted content is never echoed back.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.utils.modlog import send_log
from fyrion.utils.patterns import (
    CONTENT_SCAN_LIMIT,
    INVITE_REGEX,
    URL_REGEX,
    excerpt,
)

log = logging.getLogger("fyrion.cogs.purge")

NO_MENTIONS = discord.AllowedMentions.none()

# Discord's bulk delete endpoint accepts at most 100 messages per call, and
# discord.py chunks larger purges; keeping the ceiling at 100 keeps a single
# invocation to a single, predictable API burst.
MAX_DELETE = 100
# A filtered purge has to look past non-matching messages to find its targets.
SCAN_MULTIPLIER = 5
MAX_SCAN = 500
# Operator-supplied regexes are length limited; see _compile_pattern.
MAX_PATTERN_LENGTH = 200
# Discord truncates audit log reasons at 512 characters.
AUDIT_REASON_LIMIT = 512

# Channel types that expose Messageable.purge.
PURGEABLE: tuple[type, ...] = (
    discord.TextChannel,
    discord.Thread,
    discord.VoiceChannel,
    discord.StageChannel,
)

# Rejects the classic nested-quantifier shape ((a+)+, (a*)*) that turns a
# regex into a denial-of-service primitive. Python's re has no match timeout,
# so the pattern is refused up front rather than executed hopefully.
NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")

Predicate = Callable[[discord.Message], bool]


def _counting_matcher(
    predicate: Predicate, target: int, *, include_pinned: bool
) -> Predicate:
    """Wraps a predicate so it stops matching after ``target`` messages.

    ``Messageable.purge`` applies its check to every message in the scanned
    window and has no notion of \"stop after N matches\". Counting here is what
    keeps ``amount`` an exact upper bound on how many messages are deleted.
    """
    remaining = max(0, target)

    def check(message: discord.Message) -> bool:
        nonlocal remaining
        if remaining <= 0:
            return False
        if message.pinned and not include_pinned:
            # Pinned messages are curated content; deleting one by accident is
            # not recoverable.
            return False
        if not predicate(message):
            return False
        remaining -= 1
        return True

    return check


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compiles an operator-supplied regex, refusing dangerous shapes.

    Raises :class:`ValueError` with a user-facing message when the expression is
    too long, syntactically invalid, or nests one quantifier inside another.
    Python's ``re`` module cannot time a match out, so an expression that could
    backtrack exponentially has to be refused before it ever runs against
    attacker-controlled message content.
    """
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(
            f"The pattern must be at most {MAX_PATTERN_LENGTH} characters."
        )
    if NESTED_QUANTIFIER.search(pattern):
        raise ValueError(
            "That pattern nests one quantifier inside another (for example "
            "`(a+)+`), which can hang the bot. Please simplify it."
        )

    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"That is not a valid regular expression: {exc}") from None


def _summarize(deleted: Sequence[discord.Message]) -> tuple[int, int]:
    """Returns ``(distinct_authors, attachments)`` for a completed purge."""
    authors = {message.author.id for message in deleted}
    attachments = sum(len(message.attachments) for message in deleted)
    return len(authors), attachments


class Purge(commands.Cog):
    """Bulk message deletion commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_reason(moderator: discord.abc.User, description: str) -> str:
        return f"Purge by {moderator} ({moderator.id}): {description}"[
            :AUDIT_REASON_LIMIT
        ]

    @staticmethod
    def _authorize(
        interaction: discord.Interaction, channel: discord.abc.GuildChannel
    ) -> str | None:
        """Re-checks permissions server side. Returns an error, or None.

        ``default_permissions`` is a client-side hint: Discord uses it to hide a
        command, not to enforce it. Channel-level overwrites can also grant or
        revoke ``Manage Messages`` independently of the guild-level permission,
        so the effective permissions for *this* channel are what matter.
        """
        invoker = interaction.user
        if not isinstance(invoker, discord.Member):
            return "This command can only be used inside a server."

        guild = invoker.guild
        me = guild.me
        if me is None:
            return "I could not resolve my own membership in this server."

        invoker_permissions = channel.permissions_for(invoker)
        if not invoker_permissions.manage_messages:
            return (
                "You need the `Manage Messages` permission in "
                f"{channel.mention} to purge it."
            )
        if not invoker_permissions.read_message_history:
            return (
                "You need the `Read Message History` permission in "
                f"{channel.mention} to purge it."
            )

        my_permissions = channel.permissions_for(me)
        missing = [
            name
            for name, granted in (
                ("Manage Messages", my_permissions.manage_messages),
                ("Read Message History", my_permissions.read_message_history),
                ("View Channel", my_permissions.view_channel),
            )
            if not granted
        ]
        if missing:
            return (
                "I am missing "
                + ", ".join(f"`{name}`" for name in missing)
                + f" in {channel.mention}."
            )

        return None

    def _resolve_channel(
        self,
        interaction: discord.Interaction,
        override: discord.TextChannel | discord.Thread | None,
    ) -> tuple[Any | None, str | None]:
        """Returns ``(channel, error)`` for the channel a purge should target."""
        channel = override if override is not None else interaction.channel

        if not isinstance(channel, PURGEABLE):
            return None, (
                "Messages can only be purged from text channels, threads and "
                "channel-bound voice chats."
            )

        guild = interaction.guild
        if guild is None or getattr(channel, "guild", None) != guild:
            # Prevents a crafted interaction from pointing one guild's purge at
            # another guild's channel.
            return None, "That channel does not belong to this server."

        return channel, None

    # ------------------------------------------------------------------
    # Executor
    # ------------------------------------------------------------------

    async def _execute(
        self,
        interaction: discord.Interaction,
        *,
        amount: int,
        predicate: Predicate,
        description: str,
        include_pinned: bool = False,
        channel_override: discord.TextChannel | discord.Thread | None = None,
        case_target: discord.abc.User | None = None,
    ) -> None:
        """Runs one purge end to end: authorize, delete, report, log."""
        channel, channel_error = self._resolve_channel(interaction, channel_override)
        if channel is None:
            await interaction.response.send_message(
                f"\u274c {channel_error}", ephemeral=True
            )
            return

        if error := self._authorize(interaction, channel):
            await interaction.response.send_message(f"\u274c {error}", ephemeral=True)
            return

        # Bulk deletion regularly outruns the three second interaction window,
        # especially once messages older than 14 days force per-message calls.
        await interaction.response.defer(ephemeral=True)

        limit = min(MAX_SCAN, max(amount, amount * SCAN_MULTIPLIER))
        matcher = _counting_matcher(predicate, amount, include_pinned=include_pinned)

        try:
            deleted = await channel.purge(
                limit=limit,
                check=matcher,
                bulk=True,
                reason=self._audit_reason(interaction.user, description),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "\u274c Discord refused the purge. I need `Manage Messages` and "
                f"`Read Message History` in {channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.NotFound:
            await interaction.followup.send(
                "\u274c That channel no longer exists.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            log.error("Purge failed in channel %s: %s", channel.id, exc)
            await interaction.followup.send(
                "\u274c Discord rejected the purge. Messages older than 14 days "
                "cannot always be removed in bulk \u2014 try a smaller amount.",
                ephemeral=True,
            )
            return

        await self._report(
            interaction,
            channel,
            deleted,
            amount=amount,
            limit=limit,
            description=description,
            include_pinned=include_pinned,
        )

        guild = interaction.guild
        if guild is not None:
            await self._write_log(
                guild, interaction.user, channel, deleted, description
            )
            if case_target is not None and deleted:
                await self._record_case(
                    guild, interaction.user, case_target, len(deleted), description
                )

    async def _report(
        self,
        interaction: discord.Interaction,
        channel: Any,
        deleted: Sequence[discord.Message],
        *,
        amount: int,
        limit: int,
        description: str,
        include_pinned: bool,
    ) -> None:
        """Sends the ephemeral confirmation."""
        count = len(deleted)
        if count == 0:
            await interaction.followup.send(
                f"\u2139\ufe0f No messages matching {description} were found in the "
                f"last {limit} message(s) of {channel.mention}.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        authors, attachments = _summarize(deleted)
        lines = [
            f"\u2705 Deleted **{count}** message(s) matching {description} in "
            f"{channel.mention}.",
            f"Authors affected: {authors} \u2022 attachments removed: {attachments}",
        ]
        if count < amount:
            lines.append(
                f"Only {count} of the requested {amount} matched within the last "
                f"{limit} message(s) scanned."
            )
        if not include_pinned:
            lines.append("Pinned messages were skipped.")

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # Auditing
    # ------------------------------------------------------------------

    async def _write_log(
        self,
        guild: discord.Guild,
        moderator: discord.abc.User,
        channel: Any,
        deleted: Sequence[discord.Message],
        description: str,
    ) -> None:
        """Mirrors the purge into the guild's moderation log channel.

        Deleted content is deliberately not reproduced: a purge is usually run
        to remove exactly that content, and re-posting it would defeat the
        point. Only counts and metadata are recorded.
        """
        if not deleted:
            return
        if channel.id == await self._log_channel_id(guild):
            # Logging a purge of the log channel into the log channel it just
            # emptied is noise.
            return

        authors, attachments = _summarize(deleted)
        embed = discord.Embed(
            title="Messages purged",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Moderator",
            value=f"{moderator} (`{moderator.id}`)",
            inline=False,
        )
        embed.add_field(name="Channel", value=channel.mention, inline=False)
        embed.add_field(name="Filter", value=description, inline=False)
        embed.add_field(name="Deleted", value=str(len(deleted)), inline=True)
        embed.add_field(name="Authors", value=str(authors), inline=True)
        embed.add_field(name="Attachments", value=str(attachments), inline=True)

        oldest = deleted[-1]
        embed.add_field(
            name="Oldest removed",
            value=discord.utils.format_dt(oldest.created_at, style="F"),
            inline=False,
        )

        await send_log(self.db, guild, embed)

    async def _log_channel_id(self, guild: discord.Guild) -> int | None:
        from fyrion.utils.modlog import resolve_log_channel_id

        return await resolve_log_channel_id(self.db, guild.id)

    async def _record_case(
        self,
        guild: discord.Guild,
        moderator: discord.abc.User,
        target: discord.abc.User,
        count: int,
        description: str,
    ) -> None:
        """Records a member-targeted purge in the moderation case history."""
        create = getattr(self.db, "create_moderation_case", None)
        if create is None:
            return

        try:
            await create(
                guild_id=guild.id,
                action="purge",
                target_id=target.id,
                target_tag=str(target),
                moderator_id=moderator.id,
                reason=f"Purged {count} message(s) matching {description}"[
                    :AUDIT_REASON_LIMIT
                ],
            )
        except Exception:
            log.exception(
                "Could not record the purge case for %s in %s.", target.id, guild.id
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @app_commands.command(
        name="purge", description="Delete the most recent messages in a channel."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many messages to delete (1-100)",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_DELETE],
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        await self._execute(
            interaction,
            amount=amount,
            predicate=lambda message: True,
            description="any message",
            include_pinned=include_pinned,
            channel_override=channel,
        )

    @app_commands.command(
        name="purge-user", description="Delete recent messages from one member."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        member="Whose messages to delete",
        amount="How many of their messages to delete (1-100)",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_user_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.User,
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        target_id = member.id
        await self._execute(
            interaction,
            amount=amount,
            predicate=lambda message: message.author.id == target_id,
            description=f"messages from **{member}**",
            include_pinned=include_pinned,
            channel_override=channel,
            case_target=member,
        )

    @app_commands.command(
        name="purge-bot", description="Delete recent messages posted by bots."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many bot messages to delete (1-100)",
        channel="Target channel (defaults to the current one)",
        include_webhooks="Also delete webhook messages",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_bot_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_webhooks: bool = True,
        include_pinned: bool = False,
    ) -> None:
        def is_bot(message: discord.Message) -> bool:
            if message.author.bot:
                return True
            # Webhook posts have no bot user attached, so they need their own
            # test rather than relying on author.bot.
            return include_webhooks and message.webhook_id is not None

        scope = "bot and webhook messages" if include_webhooks else "bot messages"
        await self._execute(
            interaction,
            amount=amount,
            predicate=is_bot,
            description=scope,
            include_pinned=include_pinned,
            channel_override=channel,
        )

    @app_commands.command(
        name="purge-links",
        description="Delete recent messages that contain links or invites.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many matching messages to delete (1-100)",
        invites_only="Only delete Discord invite links",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_links_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        invites_only: bool = False,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        matcher = INVITE_REGEX if invites_only else URL_REGEX

        def has_link(message: discord.Message) -> bool:
            content = message.content
            if not content:
                return False
            return matcher.search(content[:CONTENT_SCAN_LIMIT]) is not None

        await self._execute(
            interaction,
            amount=amount,
            predicate=has_link,
            description="invite links" if invites_only else "links",
            include_pinned=include_pinned,
            channel_override=channel,
        )

    @app_commands.command(
        name="purge-attachments",
        description="Delete recent messages that carry files or images.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many matching messages to delete (1-100)",
        images_only="Only delete messages whose attachments are images",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_attachments_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        images_only: bool = False,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        def has_attachment(message: discord.Message) -> bool:
            if not message.attachments:
                return False
            if not images_only:
                return True
            # content_type is None for uploads Discord could not classify, so
            # fall back to the reported dimensions.
            return any(
                (item.content_type or "").startswith("image/") or item.height
                for item in message.attachments
            )

        await self._execute(
            interaction,
            amount=amount,
            predicate=has_attachment,
            description="image attachments" if images_only else "attachments",
            include_pinned=include_pinned,
            channel_override=channel,
        )

    @app_commands.command(
        name="purge-contains",
        description="Delete recent messages containing a piece of text.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        text="The text to look for (case-insensitive)",
        amount="How many matching messages to delete (1-100)",
        case_sensitive="Match the text exactly as typed",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_contains_cmd(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 200],
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        case_sensitive: bool = False,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        needle = text if case_sensitive else text.lower()

        def contains(message: discord.Message) -> bool:
            content = message.content
            if not content:
                return False
            haystack = content[:CONTENT_SCAN_LIMIT]
            if not case_sensitive:
                haystack = haystack.lower()
            return needle in haystack

        await self._execute(
            interaction,
            amount=amount,
            predicate=contains,
            description=f"the text `{excerpt(text, 60)}`",
            include_pinned=include_pinned,
            channel_override=channel,
        )

    @app_commands.command(
        name="purge-embeds",
        description="Delete recent messages that contain embeds or stickers.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many matching messages to delete (1-100)",
        include_stickers="Also delete messages that only contain stickers",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_embeds_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        include_stickers: bool = False,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        def has_embed(message: discord.Message) -> bool:
            if message.embeds:
                return True
            return include_stickers and bool(message.stickers)

        await self._execute(
            interaction,
            amount=amount,
            predicate=has_embed,
            description="embeds or stickers" if include_stickers else "embeds",
            include_pinned=include_pinned,
            channel_override=channel,
        )

    @app_commands.command(
        name="purge-match",
        description="Delete recent messages matching a regular expression.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        pattern="Regular expression to match against message content",
        amount="How many matching messages to delete (1-100)",
        channel="Target channel (defaults to the current one)",
        include_pinned="Delete pinned messages too (skipped by default)",
    )
    async def purge_match_cmd(
        self,
        interaction: discord.Interaction,
        pattern: app_commands.Range[str, 1, MAX_PATTERN_LENGTH],
        amount: app_commands.Range[int, 1, MAX_DELETE] = 50,
        channel: Optional[discord.TextChannel | discord.Thread] = None,
        include_pinned: bool = False,
    ) -> None:
        try:
            expression = compile_pattern(pattern)
        except ValueError as exc:
            await interaction.response.send_message(
                f"\u274c {exc}", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return

        def matches(message: discord.Message) -> bool:
            content = message.content
            if not content:
                return False
            return expression.search(content[:CONTENT_SCAN_LIMIT]) is not None

        await self._execute(
            interaction,
            amount=amount,
            predicate=matches,
            description=f"the pattern `{excerpt(pattern, 60)}`",
            include_pinned=include_pinned,
            channel_override=channel,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Purge(bot))


__all__ = [
    "Purge",
    "MAX_DELETE",
    "MAX_SCAN",
    "MAX_PATTERN_LENGTH",
    "SCAN_MULTIPLIER",
    "compile_pattern",
    "setup",
]
