"""
Plain-text transcript export.

Used when a support ticket is closed. A ``.txt`` transcript is deliberately
chosen over an HTML one: it is small, diffable, readable in any client, and it
cannot execute anything when opened. Message content is attacker controlled, so
nothing here interprets it — the text is written verbatim into a file rather
than rendered as markup, with carriage returns normalised and each message
bounded in length.

History reads are bounded by :data:`MAX_MESSAGES`. A ticket that outgrew that
bound is reported as truncated rather than silently clipped, so an operator can
tell the difference between "short conversation" and "we stopped reading".
"""
from __future__ import annotations

import io
import logging
from datetime import timezone
from typing import Any, Mapping, Sequence

import discord

log = logging.getLogger("fyrion.utils.transcripts")

# Upper bound on how many messages are read from a channel's history.
MAX_MESSAGES = 1000
# Per-message content bound. Discord itself caps messages at 4000 characters.
MAX_MESSAGE_CONTENT = 4000
# Attachments and embeds are listed, never downloaded or inlined.
MAX_ATTACHMENTS_LISTED = 10

RULE = "=" * 74


def _stamp(moment: Any) -> str:
    """Formats a timestamp as unambiguous UTC."""
    if moment is None:
        return "unknown time"
    try:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (AttributeError, ValueError, OSError):
        return str(moment)


def _clean(text: str) -> str:
    """Normalises line endings and bounds the length of a message body."""
    if not text:
        return ""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalised) > MAX_MESSAGE_CONTENT:
        normalised = normalised[:MAX_MESSAGE_CONTENT] + " [truncated]"
    return normalised


async def collect_history(
    channel: discord.abc.Messageable, *, limit: int = MAX_MESSAGES
) -> tuple[list[discord.Message], bool]:
    """Reads a channel's history oldest first.

    Returns ``(messages, truncated)``. One extra message is requested so the
    truncation flag is accurate rather than guessed.
    """
    bound = max(1, int(limit))
    collected: list[discord.Message] = []

    async for message in channel.history(limit=bound + 1, oldest_first=True):
        collected.append(message)

    truncated = len(collected) > bound
    return collected[:bound], truncated


def render_message(message: discord.Message) -> str:
    """Renders a single message as one or more transcript lines."""
    author = message.author
    header = f"[{_stamp(message.created_at)}] {author} ({author.id})"
    if getattr(author, "bot", False):
        header += " [bot]"
    if message.edited_at is not None:
        header += f" [edited {_stamp(message.edited_at)}]"

    lines = [f"{header}:"]

    body = _clean(message.content or "")
    if body:
        lines.extend(f"    {line}" for line in body.split("\n"))
    elif not message.attachments and not message.embeds and not message.stickers:
        lines.append("    [no text content]")

    for attachment in message.attachments[:MAX_ATTACHMENTS_LISTED]:
        lines.append(
            f"    [attachment] {attachment.filename} "
            f"({attachment.size} bytes) {attachment.url}"
        )
    if len(message.attachments) > MAX_ATTACHMENTS_LISTED:
        extra = len(message.attachments) - MAX_ATTACHMENTS_LISTED
        lines.append(f"    [attachment] ... and {extra} more")

    for embed in message.embeds:
        title = embed.title or embed.author.name if embed.author else embed.title
        description = _clean(embed.description or "")
        summary = title or (description.split("\n", 1)[0] if description else "embed")
        lines.append(f"    [embed] {summary}")

    for sticker in message.stickers:
        lines.append(f"    [sticker] {sticker.name}")

    return "\n".join(lines)


def build_transcript(
    *,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    messages: Sequence[discord.Message],
    truncated: bool = False,
    ticket: Mapping[str, Any] | None = None,
    opener: discord.abc.User | None = None,
    closed_by: discord.abc.User | None = None,
    reason: str | None = None,
    note: str | None = None,
) -> str:
    """Renders a complete transcript document."""
    ticket = ticket or {}

    header: list[str] = [
        "Fyrion support ticket transcript",
        RULE,
        f"Server        : {guild.name} ({guild.id})",
        f"Channel       : #{channel.name} ({channel.id})",
    ]

    number = ticket.get("ticket_number")
    if number:
        header.append(f"Ticket        : #{int(number)}")
    if ticket.get("ticket_id"):
        header.append(f"Ticket ID     : {int(ticket['ticket_id'])}")
    if ticket.get("subject"):
        header.append(f"Topic         : {_clean(str(ticket['subject']))}")

    owner_id = ticket.get("user_id")
    if opener is not None:
        header.append(f"Opened by     : {opener} ({opener.id})")
    elif owner_id:
        header.append(f"Opened by     : user {int(owner_id)}")

    if ticket.get("created_at"):
        header.append(f"Opened at     : {ticket['created_at']}")
    if ticket.get("claimed_by"):
        header.append(f"Claimed by    : user {int(ticket['claimed_by'])}")
    if closed_by is not None:
        header.append(f"Closed by     : {closed_by} ({closed_by.id})")
    if reason:
        header.append(f"Close reason  : {_clean(reason)}")

    header.append(f"Messages      : {len(messages)}" + (" (truncated)" if truncated else ""))
    if note:
        header.append(f"Note          : {note}")
    header.append(RULE)
    header.append("")

    if truncated:
        header.append(
            f"[Only the first {MAX_MESSAGES} messages of this ticket are "
            "included.]"
        )
        header.append("")

    body = [render_message(message) for message in messages]
    if not body:
        body = ["[No messages were recorded in this ticket.]"]

    footer = ["", RULE, "End of transcript."]

    return "\n".join(header + body + footer)


def transcript_filename(
    ticket: Mapping[str, Any] | None, channel: discord.abc.GuildChannel
) -> str:
    """Returns a stable, filesystem-safe transcript filename."""
    number = (ticket or {}).get("ticket_number")
    if number:
        return f"ticket-{int(number):04d}-{channel.id}.txt"
    return f"ticket-{channel.id}.txt"


def build_file(text: str, *, filename: str) -> discord.File:
    """Wraps transcript text in a fresh :class:`discord.File`.

    A ``File`` wraps a consumable stream, so a new one is built per upload
    rather than reusing a single object for several destinations.
    """
    buffer = io.BytesIO(text.encode("utf-8"))
    buffer.seek(0)
    return discord.File(buffer, filename=filename)


__all__ = [
    "MAX_MESSAGES",
    "MAX_MESSAGE_CONTENT",
    "build_file",
    "build_transcript",
    "collect_history",
    "render_message",
    "transcript_filename",
]
