"""
Ticket service layer.

The ticket panel buttons (:mod:`fyrion.views.tickets`) and the ticket slash
commands (:mod:`fyrion.cogs.tickets`) must behave identically, so the logic that
actually opens and closes a ticket lives here and both call into it. Keeping it
out of the view module also avoids an import cycle: this module never imports
views, and callers pass the persistent control view in as an argument.

Guarantees enforced here rather than at the call site:

* **One open ticket per member.** Checked against the database, so a double
  click cannot create two channels.
* **Private by default.** The channel denies ``@everyone`` and grants only the
  opener, the configured support role, and Fyrion itself.
* **Never orphan a channel.** If the database row cannot be written, the channel
  that was just created is deleted again, so there is no untracked ticket
  channel that nothing can close.
* **Never lose the conversation silently.** Closing exports a plain-text
  transcript before the channel is deleted, and the deletion is delayed so the
  participants can see why it closed.
* **Bounded, explicit mentions.** The opener and the support role are pinged
  because that is the point of a ticket; ``@everyone`` and every other role are
  disabled, so a crafted topic label cannot turn into a mass ping.

Channel names are deliberately never edited after creation: Discord rate limits
channel name and topic edits to two per ten minutes, which a busy ticket queue
would exhaust. The ticket number is carried in the topic set at creation time
and in the greeting embed instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import discord

from fyrion.utils.modlog import resolve_log_channel
from fyrion.utils.permissions import missing_channel_permissions
from fyrion.utils.transcripts import (
    build_file,
    build_transcript,
    collect_history,
    transcript_filename,
)

log = logging.getLogger("fyrion.utils.tickets")

# Panels expose at most this many topic buttons. The persistent view registers
# exactly this many custom ids, so raising it requires a restart to take effect
# for messages sent before the change.
MAX_TOPICS = 5
MAX_TOPIC_LABEL = 80
MAX_PANEL_TITLE = 256
MAX_PANEL_DESCRIPTION = 2000
MAX_UNICODE_EMOJI_LENGTH = 16

# Discord truncates audit log reasons at 512 characters and channel topics at
# 1024.
AUDIT_REASON_LIMIT = 512
TOPIC_LIMIT = 1024

# How long the closing notice stays visible before the channel is removed.
CLOSE_DELAY_SECONDS = 6.0
# When no log channel is available the transcript is stored inline instead, so
# it is capped to keep the database small.
TRANSCRIPT_DB_LIMIT = 20_000

# Discord allows 50 channels per category.
CATEGORY_CHANNEL_LIMIT = 50

DEFAULT_TOPIC: Mapping[str, Any] = {
    "label": "Create Ticket",
    "emoji": "\U0001f3ab",
    "style": "primary",
}

BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

_SLUG_STRIP = re.compile(r"[^a-z0-9\-]+")
_SLUG_COLLAPSE = re.compile(r"-{2,}")

# Deletion runs in a background task; a strong reference keeps it from being
# garbage collected mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Topics and panels
# ---------------------------------------------------------------------------


def button_style(name: Any) -> discord.ButtonStyle:
    return BUTTON_STYLES.get(str(name or "").lower(), discord.ButtonStyle.primary)


def parse_emoji(bot: discord.Client, raw: str) -> str | None:
    """Validates an operator-supplied emoji.

    Raises:
        ValueError: when the value is not a usable emoji.
    """
    text = (raw or "").strip()
    if not text:
        return None

    partial = discord.PartialEmoji.from_str(text)

    if partial.id is None:
        candidate = partial.name or text
        # A standard emoji is never plain ASCII and never long. Refusing both
        # keeps arbitrary text out of the button and reaction APIs.
        if candidate.isascii() or len(candidate) > MAX_UNICODE_EMOJI_LENGTH:
            raise ValueError(
                f"`{text[:32]}` is not an emoji. Use a single standard emoji, or "
                "a custom emoji from a server I am also in."
            )
        return candidate

    if bot.get_emoji(partial.id) is None:
        raise ValueError(
            "I cannot use that custom emoji. I must be a member of the server "
            "it belongs to."
        )
    prefix = "a" if partial.animated else ""
    return f"<{prefix}:{partial.name}:{partial.id}>"


def sanitize_topics(raw: Any) -> list[dict[str, Any]]:
    """Normalises stored topic data into a usable list.

    Anything unparseable degrades to the single default topic: a corrupt panel
    configuration must still produce a working button, never an exception on the
    interaction path.
    """
    entries: Any = raw
    if isinstance(raw, str):
        try:
            entries = json.loads(raw) if raw.strip() else []
        except (ValueError, TypeError):
            entries = []

    if not isinstance(entries, (list, tuple)):
        entries = []

    topics: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"label": entry}
        if not isinstance(entry, Mapping):
            continue

        label = str(entry.get("label") or "").strip()[:MAX_TOPIC_LABEL]
        if not label:
            continue

        emoji = entry.get("emoji")
        topics.append(
            {
                "label": label,
                "emoji": str(emoji) if emoji else None,
                "style": str(entry.get("style") or "primary").lower(),
            }
        )
        if len(topics) >= MAX_TOPICS:
            break

    return topics or [dict(DEFAULT_TOPIC)]


def parse_topics(
    bot: discord.Client, raw: str | None, *, style: str = "primary"
) -> list[dict[str, Any]]:
    """Parses the ``topics`` argument of ``/ticket-setup``.

    Format: comma-separated entries, each optionally ``Label | emoji``.

    Raises:
        ValueError: with a user-facing message when the input is unusable.
    """
    if raw is None or not raw.strip():
        return [dict(DEFAULT_TOPIC, style=style)]

    entries = [part.strip() for part in raw.split(",") if part.strip()]
    if not entries:
        raise ValueError("No usable topics were supplied.")
    if len(entries) > MAX_TOPICS:
        raise ValueError(
            f"A panel can expose at most {MAX_TOPICS} topics; you supplied "
            f"{len(entries)}."
        )

    topics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        label_part, _, emoji_part = entry.partition("|")
        label = label_part.strip()[:MAX_TOPIC_LABEL]
        if not label:
            raise ValueError("Every topic needs a label before the `|` separator.")
        if label.lower() in seen:
            raise ValueError(f"The topic `{label}` is listed more than once.")
        seen.add(label.lower())

        topics.append(
            {
                "label": label,
                "emoji": parse_emoji(bot, emoji_part),
                "style": style,
            }
        )

    return topics


def panel_embed(config: Mapping[str, Any]) -> discord.Embed:
    """Builds the embed shown above the ticket panel buttons."""
    title = str(config.get("title") or "Support Tickets")[:MAX_PANEL_TITLE]
    description = str(
        config.get("description")
        or "Press a button below to open a private ticket with the staff team."
    )[:MAX_PANEL_DESCRIPTION]

    embed = discord.Embed(
        title=title, description=description, color=discord.Color.blurple()
    )

    topics = sanitize_topics(config.get("topics"))
    if len(topics) > 1:
        embed.add_field(
            name="Topics",
            value="\n".join(f"\u2022 {topic['label']}" for topic in topics)[:1024],
            inline=False,
        )

    embed.set_footer(text="You may have one open ticket at a time.")
    return embed


def slugify(name: str, *, fallback: str = "member") -> str:
    """Turns a display name into a Discord-safe channel name fragment."""
    lowered = (name or "").lower().replace(" ", "-")
    cleaned = _SLUG_STRIP.sub("", lowered)
    cleaned = _SLUG_COLLAPSE.sub("-", cleaned).strip("-")
    return cleaned[:24] or fallback


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def is_support(member: discord.Member, config: Mapping[str, Any]) -> bool:
    """Returns True when ``member`` is ticket staff.

    Staff is either someone who can already manage the server's channels, or a
    holder of the configured support role. This is evaluated server side; the
    command's ``default_permissions`` only affects visibility.
    """
    permissions = member.guild_permissions
    if (
        permissions.administrator
        or permissions.manage_guild
        or permissions.manage_channels
    ):
        return True

    role_id = config.get("support_role_id")
    if not role_id:
        return False
    return any(role.id == int(role_id) for role in member.roles)


def is_owner(ticket: Mapping[str, Any], member: discord.abc.User) -> bool:
    try:
        return int(ticket.get("user_id") or 0) == member.id
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class TicketOpenResult:
    channel: discord.TextChannel | None = None
    ticket: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.channel is not None and self.error is None


@dataclass
class TicketCloseResult:
    closed: bool = False
    error: str | None = None
    log_url: str | None = None
    owner_notified: bool = False
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


def _audit_reason(actor: discord.abc.User, description: str) -> str:
    return f"{actor} ({actor.id}): {description}"[:AUDIT_REASON_LIMIT]


def greeting_embed(
    member: discord.Member,
    ticket: Mapping[str, Any],
    topic: Mapping[str, Any] | None,
) -> discord.Embed:
    number = ticket.get("ticket_number")
    title = f"Ticket #{int(number)}" if number else "Ticket opened"

    embed = discord.Embed(
        title=title,
        description=(
            f"Thanks {member.mention}. Please describe your issue in as much "
            "detail as you can \u2014 include screenshots or links if they help. "
            "A staff member will reply here."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    if topic and topic.get("label"):
        embed.add_field(name="Topic", value=str(topic["label"])[:1024], inline=True)
    embed.add_field(name="Opened by", value=f"{member} (`{member.id}`)", inline=True)
    embed.set_footer(
        text=(
            "Staff can claim this ticket. Closing it exports a transcript and "
            "deletes the channel."
        )
    )
    return embed


async def open_ticket(
    *,
    guild: discord.Guild,
    member: discord.Member,
    repo: Any,
    config: Mapping[str, Any],
    topic: Mapping[str, Any] | None = None,
    panel_message_id: int | None = None,
    control_view: discord.ui.View | None = None,
) -> TicketOpenResult:
    """Creates a private ticket channel and records it."""
    me = guild.me
    if me is None:
        return TicketOpenResult(
            error="I could not resolve my own membership in this server."
        )

    category_id = config.get("category_id")
    if not category_id:
        return TicketOpenResult(
            error=(
                "The ticket system is not configured in this server yet. An "
                "administrator needs to run `/ticket-setup` first."
            )
        )

    category = guild.get_channel(int(category_id))
    if not isinstance(category, discord.CategoryChannel):
        return TicketOpenResult(
            error=(
                "The configured ticket category no longer exists. An "
                "administrator needs to run `/ticket-setup` again."
            )
        )

    missing = missing_channel_permissions(
        me, category, view_channel=True, manage_channels=True
    )
    if missing:
        names = ", ".join(f"`{name}`" for name in missing)
        return TicketOpenResult(
            error=f"I am missing {names} in the ticket category."
        )
    if not me.guild_permissions.manage_roles:
        # Overwrites cannot be written without it, so the channel would be
        # created world-readable.
        return TicketOpenResult(
            error=(
                "I need the `Manage Roles` permission to create a private ticket "
                "channel."
            )
        )
    if len(category.channels) >= CATEGORY_CHANNEL_LIMIT:
        return TicketOpenResult(
            error=(
                "The ticket category is full (Discord allows 50 channels per "
                "category). Ask staff to close some tickets first."
            )
        )

    try:
        existing = await repo.get_open_ticket_for_user(guild.id, member.id)
    except Exception:
        log.exception("Could not check for an existing ticket in guild %s.", guild.id)
        return TicketOpenResult(
            error="I could not check your existing tickets. Please try again."
        )

    if existing:
        channel_id = int(existing.get("channel_id") or 0)
        open_channel = guild.get_channel(channel_id)
        if open_channel is not None:
            return TicketOpenResult(
                error=(
                    f"You already have an open ticket: {open_channel.mention}. "
                    "Please continue there."
                )
            )
        # The channel is gone but the row survived; close it so the member is
        # not locked out of the system.
        try:
            await repo.close_ticket(
                channel_id, closed_by=None, reason="Channel no longer exists"
            )
        except Exception:
            log.exception("Could not reconcile the stale ticket %s.", channel_id)

    support_role: discord.Role | None = None
    role_id = config.get("support_role_id")
    if role_id:
        candidate = guild.get_role(int(role_id))
        if candidate is not None and not candidate.is_default():
            support_role = candidate

    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            add_reactions=True,
        ),
        me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_messages=True,
            manage_channels=True,
        ),
    }
    if support_role is not None:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_messages=True,
        )

    label = str((topic or {}).get("label") or "Support")[:MAX_TOPIC_LABEL]
    channel_topic = (
        f"Ticket opened by {member} ({member.id}) \u2022 topic: {label}"
    )[:TOPIC_LIMIT]

    try:
        channel = await category.create_text_channel(
            name=f"ticket-{slugify(member.display_name or member.name)}",
            overwrites=overwrites,
            topic=channel_topic,
            reason=_audit_reason(member, "Ticket opened"),
        )
    except discord.Forbidden:
        return TicketOpenResult(
            error="Discord refused to create the ticket channel. Check my "
            "permissions in the ticket category."
        )
    except discord.HTTPException as exc:
        log.warning("Ticket channel creation failed in guild %s: %s", guild.id, exc)
        return TicketOpenResult(
            error=f"Discord rejected the ticket channel (HTTP {exc.status})."
        )

    try:
        ticket = await repo.create_ticket(
            guild_id=guild.id,
            channel_id=channel.id,
            user_id=member.id,
            subject=label,
            panel_message_id=panel_message_id,
        )
    except Exception:
        log.exception(
            "Could not record the ticket for member %s in guild %s; rolling back "
            "the channel.",
            member.id,
            guild.id,
        )
        # An untracked ticket channel could never be closed by Fyrion, so it is
        # removed rather than left behind.
        try:
            await channel.delete(reason="Ticket could not be recorded")
        except discord.HTTPException:
            log.warning(
                "Orphaned ticket channel %s could not be removed.", channel.id
            )
        return TicketOpenResult(
            error="Your ticket could not be recorded. Please try again shortly."
        )

    mentions = discord.AllowedMentions(
        everyone=False,
        users=[member],
        roles=[support_role] if support_role is not None else False,
        replied_user=False,
    )
    content = member.mention
    if support_role is not None:
        content = f"{member.mention} {support_role.mention}"

    try:
        await channel.send(
            content=content,
            embed=greeting_embed(member, ticket, topic),
            view=control_view,
            allowed_mentions=mentions,
        )
    except discord.HTTPException as exc:
        # The ticket exists and is usable; only the greeting failed.
        log.warning("Could not post the ticket greeting in %s: %s", channel.id, exc)

    return TicketOpenResult(channel=channel, ticket=dict(ticket))


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------


def close_embed(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    ticket: Mapping[str, Any],
    closed_by: discord.abc.User | None,
    reason: str | None,
    message_count: int,
) -> discord.Embed:
    number = ticket.get("ticket_number")
    embed = discord.Embed(
        title=f"Ticket #{int(number)} closed" if number else "Ticket closed",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Channel", value=f"#{channel.name} (`{channel.id}`)", inline=False)

    owner_id = ticket.get("user_id")
    if owner_id:
        embed.add_field(name="Opened by", value=f"<@{int(owner_id)}>", inline=True)
    if ticket.get("claimed_by"):
        embed.add_field(
            name="Claimed by", value=f"<@{int(ticket['claimed_by'])}>", inline=True
        )
    if closed_by is not None:
        embed.add_field(
            name="Closed by", value=f"{closed_by} (`{closed_by.id}`)", inline=True
        )

    embed.add_field(
        name="Reason", value=(reason or "No reason provided")[:1024], inline=False
    )
    embed.add_field(name="Messages", value=str(message_count), inline=True)
    if ticket.get("subject"):
        embed.add_field(name="Topic", value=str(ticket["subject"])[:1024], inline=True)

    return embed


def _schedule_delete(
    channel: discord.abc.GuildChannel, delay: float, reason: str
) -> None:
    """Deletes a channel after ``delay`` seconds, in the background.

    Deleting inline would make the caller's interaction response wait for the
    grace period, so the wait happens in a task while the command replies
    immediately.
    """

    async def _runner() -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await channel.delete(reason=reason[:AUDIT_REASON_LIMIT])
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            pass  # Somebody deleted it first.
        except discord.Forbidden:
            log.warning(
                "Missing permission to delete the ticket channel %s.", channel.id
            )
        except discord.HTTPException as exc:
            log.warning("Could not delete the ticket channel %s: %s", channel.id, exc)

    task = asyncio.create_task(_runner(), name=f"fyrion-ticket-delete-{channel.id}")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def close_ticket(
    *,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    repo: Any,
    closed_by: discord.abc.User | None,
    reason: str | None = None,
    config: Mapping[str, Any] | None = None,
    delay: float = CLOSE_DELAY_SECONDS,
    notify_owner: bool = True,
) -> TicketCloseResult:
    """Closes a ticket: export the transcript, archive it, delete the channel."""
    result = TicketCloseResult()

    if not isinstance(channel, discord.TextChannel):
        result.error = "Tickets can only be closed from their own text channel."
        return result

    try:
        ticket = await repo.get_ticket_by_channel(channel.id)
    except Exception:
        log.exception("Could not read the ticket for channel %s.", channel.id)
        result.error = "I could not read that ticket. Please try again."
        return result

    if ticket is None:
        result.error = "This channel is not a Fyrion ticket."
        return result
    if str(ticket.get("status")) == "closed":
        result.error = "That ticket is already closed."
        return result

    # Claim the close first: the conditional UPDATE is what stops two
    # simultaneous closes from both exporting and both deleting.
    try:
        claimed = await repo.close_ticket(
            channel.id,
            closed_by=closed_by.id if closed_by is not None else None,
            reason=(reason or None),
        )
    except Exception:
        log.exception("Could not close the ticket for channel %s.", channel.id)
        result.error = "I could not close that ticket. Please try again."
        return result

    if not claimed:
        result.error = "That ticket is already being closed."
        return result

    result.closed = True
    if config is None:
        try:
            config = await repo.get_config(guild.id)
        except Exception:
            log.exception("Could not read the ticket config for guild %s.", guild.id)
            config = {}

    me = guild.me
    transcript_text: str | None = None
    messages: Sequence[discord.Message] = ()

    can_read = me is not None and channel.permissions_for(me).read_message_history
    if can_read:
        try:
            collected, truncated = await collect_history(channel)
        except discord.Forbidden:
            result.warnings.append(
                "I could not read the message history, so no transcript was made."
            )
        except discord.HTTPException as exc:
            log.warning("Could not read the ticket history in %s: %s", channel.id, exc)
            result.warnings.append(
                "Discord refused the history read, so no transcript was made."
            )
        else:
            messages = collected
            result.truncated = truncated
            owner = None
            owner_id = ticket.get("user_id")
            if owner_id:
                owner = guild.get_member(int(owner_id))
            transcript_text = build_transcript(
                guild=guild,
                channel=channel,
                messages=collected,
                truncated=truncated,
                ticket=ticket,
                opener=owner,
                closed_by=closed_by,
                reason=reason,
            )
    else:
        result.warnings.append(
            "I am missing `Read Message History` here, so no transcript was made."
        )

    summary = close_embed(guild, channel, ticket, closed_by, reason, len(messages))
    filename = transcript_filename(ticket, channel)

    archive = await _resolve_archive_channel(guild, repo, config)
    if archive is not None:
        try:
            payload = (
                build_file(transcript_text, filename=filename)
                if transcript_text is not None
                else None
            )
            posted = await archive.send(
                embed=summary,
                file=payload,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            result.log_url = posted.jump_url
        except discord.HTTPException as exc:
            log.warning("Could not archive the ticket transcript: %s", exc)
            result.warnings.append(
                "The transcript could not be posted to the ticket log channel."
            )

    if notify_owner and transcript_text is not None:
        result.owner_notified = await _notify_owner(
            guild, ticket, summary, transcript_text, filename
        )

    # Store a pointer rather than the whole conversation when the transcript was
    # archived, so the database does not grow with message history.
    pointer: str | None = None
    if result.log_url:
        pointer = result.log_url
    elif transcript_text is not None:
        pointer = transcript_text[:TRANSCRIPT_DB_LIMIT]

    if pointer is not None:
        try:
            await repo.set_transcript(channel.id, pointer)
        except Exception:
            log.exception("Could not store the transcript for ticket %s.", channel.id)

    notice = discord.Embed(
        title="Ticket closed",
        description=(
            f"This channel will be deleted in {int(max(1, delay))} seconds."
        ),
        color=discord.Color.dark_grey(),
    )
    notice.add_field(
        name="Reason", value=(reason or "No reason provided")[:1024], inline=False
    )
    if result.log_url:
        notice.add_field(
            name="Transcript", value=f"[Archived]({result.log_url})", inline=False
        )

    if me is not None and channel.permissions_for(me).send_messages:
        try:
            await channel.send(
                embed=notice, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            pass

    _schedule_delete(
        channel,
        delay,
        f"Ticket closed by {closed_by} ({closed_by.id})"
        if closed_by is not None
        else "Ticket closed",
    )

    return result


async def _resolve_archive_channel(
    guild: discord.Guild, repo: Any, config: Mapping[str, Any]
) -> discord.TextChannel | None:
    """Returns the writable channel transcripts should be posted to."""
    me = guild.me
    if me is None:
        return None

    channel_id = config.get("log_channel_id")
    if channel_id:
        candidate = guild.get_channel(int(channel_id))
        if isinstance(candidate, discord.TextChannel):
            permissions = candidate.permissions_for(me)
            if (
                permissions.send_messages
                and permissions.embed_links
                and permissions.attach_files
            ):
                return candidate

    # Fall back to the server's moderation log so a transcript is not lost just
    # because no ticket-specific channel was configured.
    try:
        return await resolve_log_channel(getattr(repo, "db", None), guild)
    except Exception:
        log.exception("Could not resolve a fallback archive channel.")
        return None


async def _notify_owner(
    guild: discord.Guild,
    ticket: Mapping[str, Any],
    summary: discord.Embed,
    transcript_text: str,
    filename: str,
) -> bool:
    """Sends the transcript to the member who opened the ticket, best effort."""
    owner_id = ticket.get("user_id")
    if not owner_id:
        return False

    member = guild.get_member(int(owner_id))
    if member is None:
        return False

    try:
        await member.send(
            content=f"Your ticket in **{guild.name}** has been closed.",
            embed=summary,
            file=build_file(transcript_text, filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        # Closed DMs are not an error.
        return False
    return True


__all__ = [
    "BUTTON_STYLES",
    "CLOSE_DELAY_SECONDS",
    "DEFAULT_TOPIC",
    "MAX_PANEL_DESCRIPTION",
    "MAX_PANEL_TITLE",
    "MAX_TOPICS",
    "MAX_TOPIC_LABEL",
    "TicketCloseResult",
    "TicketOpenResult",
    "button_style",
    "close_embed",
    "close_ticket",
    "greeting_embed",
    "is_owner",
    "is_support",
    "open_ticket",
    "panel_embed",
    "parse_emoji",
    "parse_topics",
    "sanitize_topics",
    "slugify",
]
