"""
Giveaway cog.

Giveaways live in the core ``giveaways`` and ``giveaway_entries`` tables, so
every read and write goes through the pooled data layer, which validates each
identifier against the schema allow-list and binds every value as a SQL
parameter.

Design notes
------------
* **One persistent view, many messages.** A panel posted before a restart must
  keep working, so the entry buttons use fixed ``custom_id``s and
  ``timeout=None``. A persistent view cannot carry per-message state, so the
  pressed button resolves its giveaway from ``interaction.message.id``.
* **Live countdown.** A single scheduler loop refreshes the giveaway embed on a
  cadence that tightens as the deadline approaches (every five minutes when the
  end is more than an hour away, every ten seconds in the final minute).
  Refreshes use a partial message edit, so no history fetch is needed, and the
  cadence is deliberately coarse far from the deadline to stay well inside
  Discord's per-channel edit rate limit.
* **Exactly one ending.** Ending is claimed with a conditional ``UPDATE`` that
  only matches an ``active`` row, so a scheduler tick and a manual
  ``/giveaway-end`` racing each other cannot both announce winners.
* **One entry per member.** The unique index on ``(giveaway_id, user_id)`` is
  what enforces it, so a double click cannot create two entries.
* **Never orphan a message.** If the database row cannot be written, the panel
  message that was just posted is removed again, so no live giveaway exists that
  nothing can end.

Authorization: ``default_permissions`` only decides whether Discord *shows* a
command, so ``/giveaway-start``, ``/giveaway-end`` and ``/giveaway-reroll``
re-check ``Manage Server`` server side.

Prizes, descriptions and requirement labels are operator supplied, so every
reply disables mention parsing. The single exception is the winner
announcement, which is allowed to mention exactly the drawn winners and nothing
else — never a role and never ``@everyone``.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands, tasks

from fyrion.database.manager import iso_from_now, utc_now_iso
from fyrion.database.repositories.leveling import level_from_xp
from fyrion.utils.modlog import send_log
from fyrion.utils.permissions import missing_channel_permissions

log = logging.getLogger("fyrion.cogs.giveaways")

NO_MENTIONS = discord.AllowedMentions.none()

ENTER_BUTTON_ID = "fyrion:giveaway:enter"
COUNT_BUTTON_ID = "fyrion:giveaway:count"

# Bounds. A giveaway shorter than half a minute cannot realistically be entered,
# and one longer than two months is almost always a typo.
MIN_DURATION_SECONDS = 30
MAX_DURATION_SECONDS = 60 * 60 * 24 * 60
MAX_WINNERS = 20
MAX_PRIZE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 1000
MAX_LEVEL_REQUIREMENT = 500

# A server with dozens of simultaneous giveaways would make the scheduler edit
# messages constantly, so the number of live giveaways is capped.
MAX_ACTIVE_PER_GUILD = 25
MAX_LISTED = 15

# How often the scheduler wakes up. The per-giveaway refresh cadence below is
# what actually decides when a message is edited.
SCHEDULER_SECONDS = 15
MAX_TRACKED_GIVEAWAYS = 200

# Refresh cadence: (remaining seconds threshold, seconds between edits).
REFRESH_STEPS: tuple[tuple[float, float], ...] = (
    (3600.0, 300.0),
    (900.0, 120.0),
    (300.0, 60.0),
    (60.0, 20.0),
)
FINAL_REFRESH_SECONDS = 10.0

# Drawing validates each candidate against the live guild, which may need a REST
# fetch when the member cache is cold. The attempt cap bounds that work.
DRAW_ATTEMPT_BASE = 20
DRAW_ATTEMPT_MULTIPLIER = 5
MAX_ENTRY_SCAN = 10_000

MESSAGE_LINK_RE = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d{15,25})/(?P<channel>\d{15,25})/(?P<message>\d{15,25})"
)

_DURATION_TOKEN = re.compile(r"(?P<value>\d+)(?P<unit>[a-z]+)")
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}

StatusFilter = Literal["active", "ended", "cancelled", "all"]

# Strong references so a scheduled task cannot be garbage collected mid-flight.
_BACKGROUND_TASKS: set[Any] = set()


# ---------------------------------------------------------------------------
# Parsing and formatting helpers
# ---------------------------------------------------------------------------


def parse_duration(raw: str) -> int:
    """Parses a duration such as ``30m``, ``2h30m`` or ``3d`` into seconds.

    A bare number is read as minutes, which is what people type most often.

    Raises:
        ValueError: with a user-facing message when the value is unusable.
    """
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("No duration was supplied.")

    if text.isdigit():
        seconds = int(text) * 60
    else:
        matches = list(_DURATION_TOKEN.finditer(text))
        if not matches or "".join(match.group(0) for match in matches) != text:
            raise ValueError(
                "That is not a duration. Use values such as `45m`, `2h30m`, "
                "`3d` or a plain number of minutes."
            )

        seconds = 0
        for match in matches:
            unit = _UNIT_SECONDS.get(match.group("unit"))
            if unit is None:
                raise ValueError(
                    f"`{match.group('unit')}` is not a duration unit. Use s, m, "
                    "h, d or w."
                )
            seconds += int(match.group("value")) * unit

    if seconds < MIN_DURATION_SECONDS:
        raise ValueError(
            f"A giveaway must run for at least {MIN_DURATION_SECONDS} seconds."
        )
    if seconds > MAX_DURATION_SECONDS:
        raise ValueError("A giveaway can run for at most 60 days.")
    return seconds


def format_delta(seconds: float) -> str:
    """Renders a remaining duration as a short human readable string."""
    total = int(max(0.0, seconds))
    if total <= 0:
        return "a moment"

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if secs and not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts) or f"{total}s"


def refresh_interval(remaining: float) -> float:
    """Returns how long to wait between two countdown edits."""
    for threshold, interval in REFRESH_STEPS:
        if remaining > threshold:
            return interval
    return FINAL_REFRESH_SECONDS


def parse_iso(value: Any) -> datetime | None:
    """Parses the ISO-8601 UTC format the schema stores, tolerating junk."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def winner_ids(row: Mapping[str, Any]) -> list[int]:
    """Parses a giveaway's stored winner list.

    A corrupt value degrades to an empty list: it must never raise on the
    announcement path.
    """
    raw = row.get("winners")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        candidates: Sequence[Any] = raw
    else:
        try:
            parsed = json.loads(str(raw))
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, (list, tuple)):
            return []
        candidates = parsed

    result: list[int] = []
    for item in candidates:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in result:
            result.append(value)
    return result


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _weighted_index(rng: random.Random, weights: Sequence[int]) -> int:
    """Returns a weighted random index into ``weights``."""
    total = sum(weights)
    if total <= 0:
        return 0
    pick = rng.uniform(0.0, float(total))
    running = 0.0
    for index, weight in enumerate(weights):
        running += float(weight)
        if pick <= running:
            return index
    return len(weights) - 1


def requirement_lines(guild: discord.Guild | None, row: Mapping[str, Any]) -> list[str]:
    """Renders a giveaway's entry requirements for the embed."""
    lines: list[str] = []

    role_id = row.get("required_role_id")
    if role_id:
        role = guild.get_role(int(role_id)) if guild is not None else None
        lines.append(
            f"Must have {role.mention}"
            if role is not None
            else f"Must have a deleted role (`{int(role_id)}`)"
        )

    level = row.get("required_level")
    if level:
        lines.append(f"Must be at least level **{int(level)}**")

    return lines


def giveaway_embed(
    guild: discord.Guild | None,
    row: Mapping[str, Any],
    *,
    entry_count: int,
) -> discord.Embed:
    """Builds the giveaway panel embed for the current state of ``row``."""
    status = str(row.get("status") or "active")
    prize = str(row.get("prize") or "a prize")
    winner_count = max(1, _as_int(row.get("winner_count"), 1))
    ends_at = parse_iso(row.get("ends_at"))
    ended_at = parse_iso(row.get("ended_at"))
    winners = winner_ids(row)

    if status == "active":
        color = discord.Color.green()
    elif status == "cancelled":
        color = discord.Color.dark_grey()
    else:
        color = discord.Color.gold()

    description: list[str] = []
    body = row.get("description")
    if body:
        description.append(str(body)[:MAX_DESCRIPTION_LENGTH])

    if status == "active":
        if ends_at is not None:
            remaining = (ends_at - datetime.now(timezone.utc)).total_seconds()
            description.append(
                f"Ends {discord.utils.format_dt(ends_at, style='R')} "
                f"({discord.utils.format_dt(ends_at, style='f')})"
            )
            description.append(f"Time left: **{format_delta(remaining)}**")
        description.append("Press **Enter** below to take part.")
    elif status == "cancelled":
        description.append("This giveaway was cancelled.")
    else:
        moment = ended_at or ends_at
        if moment is not None:
            description.append(f"Ended {discord.utils.format_dt(moment, style='R')}")
        if winners:
            description.append(
                "Winner(s): " + ", ".join(f"<@{user_id}>" for user_id in winners)
            )
        else:
            description.append("No valid entries, so no winner was drawn.")

    embed = discord.Embed(
        title=f"🎉 {prize}"[:256],
        description="\n".join(description)[:4000],
        color=color,
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(name="Winners", value=str(winner_count), inline=True)
    embed.add_field(name="Entries", value=f"{max(0, entry_count):,}", inline=True)

    host_id = row.get("host_id")
    if host_id:
        embed.add_field(name="Hosted by", value=f"<@{int(host_id)}>", inline=True)

    requirements = requirement_lines(guild, row)
    if requirements:
        embed.add_field(
            name="Requirements",
            value="\n".join(f"\u2022 {line}" for line in requirements)[:1024],
            inline=False,
        )

    giveaway_id = row.get("giveaway_id")
    footer = f"Giveaway #{int(giveaway_id)}" if giveaway_id else "Giveaway"
    if status == "active":
        footer += " \u2022 one entry per member"
    embed.set_footer(text=footer)

    return embed


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class GiveawayRepository:
    """Reads and writes the ``giveaways`` and ``giveaway_entries`` tables."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def create(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        host_id: int,
        prize: str,
        winner_count: int,
        ends_at: str,
        description: str | None = None,
        required_role_id: int | None = None,
        required_level: int | None = None,
    ) -> dict[str, Any]:
        """Stores a new giveaway and returns the created row."""
        giveaway_id = await self.db.insert(
            "giveaways",
            {
                "guild_id": int(guild_id),
                "channel_id": int(channel_id),
                "message_id": int(message_id),
                "host_id": int(host_id),
                "prize": str(prize)[:MAX_PRIZE_LENGTH],
                "description": (
                    str(description)[:MAX_DESCRIPTION_LENGTH] if description else None
                ),
                "winner_count": max(1, int(winner_count)),
                "required_role_id": int(required_role_id) if required_role_id else None,
                "required_level": int(required_level) if required_level else None,
                "status": "active",
                "ends_at": ends_at,
            },
        )
        if giveaway_id is None:
            raise RuntimeError("The giveaway row could not be inserted.")

        row = await self.db.fetch_one("giveaways", {"giveaway_id": int(giveaway_id)})
        if row is None:  # pragma: no cover - the insert just succeeded
            raise RuntimeError("The giveaway disappeared after insertion.")
        return row

    async def get(self, giveaway_id: int) -> dict[str, Any] | None:
        return await self.db.fetch_one("giveaways", {"giveaway_id": int(giveaway_id)})

    async def get_by_message(self, message_id: int) -> dict[str, Any] | None:
        return await self.db.fetch_one("giveaways", {"message_id": int(message_id)})

    async def list_active(
        self, *, limit: int = MAX_TRACKED_GIVEAWAYS
    ) -> list[dict[str, Any]]:
        return await self.db.fetch_many(
            "giveaways",
            {"status": "active"},
            order_by="ends_at ASC",
            limit=max(1, int(limit)),
        )

    async def list_for_guild(
        self,
        guild_id: int,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = MAX_LISTED,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"guild_id": int(guild_id)}
        if statuses:
            where["status"] = list(statuses)
        return await self.db.fetch_many(
            "giveaways",
            where,
            order_by="giveaway_id DESC",
            limit=max(1, int(limit)),
        )

    async def count_active(self, guild_id: int) -> int:
        return await self.db.count(
            "giveaways", {"guild_id": int(guild_id), "status": "active"}
        )

    async def claim_ended(self, giveaway_id: int) -> bool:
        """Marks an active giveaway ended. Returns False when already ended.

        The status guard lives in the ``UPDATE``, so a scheduler tick and a
        manual ``/giveaway-end`` cannot both proceed.
        """
        changed = await self.db.update(
            "giveaways",
            {"status": "ended", "ended_at": utc_now_iso()},
            {"giveaway_id": int(giveaway_id), "status": "active"},
        )
        return bool(changed)

    async def cancel(self, giveaway_id: int) -> bool:
        changed = await self.db.update(
            "giveaways",
            {"status": "cancelled", "ended_at": utc_now_iso()},
            {"giveaway_id": int(giveaway_id), "status": "active"},
        )
        return bool(changed)

    async def store_winners(self, giveaway_id: int, winners: Sequence[int]) -> None:
        payload = json.dumps(
            [int(user_id) for user_id in winners], separators=(",", ":")
        )
        await self.db.update(
            "giveaways", {"winners": payload}, {"giveaway_id": int(giveaway_id)}
        )

    async def delete(self, giveaway_id: int) -> bool:
        removed = await self.db.delete("giveaways", {"giveaway_id": int(giveaway_id)})
        return bool(removed)

    async def entries(self, giveaway_id: int) -> list[dict[str, Any]]:
        return await self.db.get_giveaway_entries(int(giveaway_id))

    async def count_entries(self, giveaway_id: int) -> int:
        return await self.db.count(
            "giveaway_entries", {"giveaway_id": int(giveaway_id)}
        )

    async def has_entry(self, giveaway_id: int, user_id: int) -> bool:
        return await self.db.exists(
            "giveaway_entries",
            {"giveaway_id": int(giveaway_id), "user_id": int(user_id)},
        )

    async def add_entry(self, giveaway_id: int, user_id: int) -> bool:
        return await self.db.add_giveaway_entry(int(giveaway_id), int(user_id))

    async def remove_entry(self, giveaway_id: int, user_id: int) -> bool:
        return await self.db.remove_giveaway_entry(int(giveaway_id), int(user_id))


async def check_requirements(
    db: Any, member: discord.Member, row: Mapping[str, Any]
) -> str | None:
    """Returns a refusal reason when ``member`` may not enter, else ``None``."""
    role_id = row.get("required_role_id")
    if role_id:
        role = member.guild.get_role(int(role_id))
        if role is None:
            return (
                "The role this giveaway requires no longer exists, so entries "
                "cannot be validated. Please tell the host."
            )
        if not any(existing.id == role.id for existing in member.roles):
            return f"You need the {role.name} role to enter this giveaway."

    required_level = row.get("required_level")
    if required_level:
        needed = int(required_level)
        profile = None
        try:
            profile = await db.fetch_one(
                "leveling_profiles",
                {"guild_id": member.guild.id, "user_id": member.id},
            )
        except Exception:
            log.exception(
                "Could not read the leveling profile for member %s.", member.id
            )
            return "I could not verify your level right now. Please try again."

        stored = _as_int((profile or {}).get("level"), 0)
        derived = level_from_xp(_as_int((profile or {}).get("xp"), 0))
        level = max(stored, derived)
        if level < needed:
            return (
                f"You need to be at least level {needed} to enter this giveaway; "
                f"you are level {level}."
            )

    return None


# ---------------------------------------------------------------------------
# Persistent view
# ---------------------------------------------------------------------------


async def _reply(
    interaction: discord.Interaction, message: str, *, ephemeral: bool = True
) -> None:
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
        # The interaction token expired; there is nothing left to answer.
        log.debug("Giveaway interaction expired before it could be answered.")
    except discord.HTTPException as exc:
        log.warning("Could not answer a giveaway interaction: %s", exc)


class GiveawayView(discord.ui.View):
    """Entry controls attached to a giveaway message.

    Persistent: the ``custom_id``s are stable, so Discord routes interactions
    from messages posted before a restart back into this process.
    """

    def __init__(self, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

    @classmethod
    def ended(cls) -> "GiveawayView":
        """Returns the same view with its buttons disabled."""
        return cls(disabled=True)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="Enter",
        style=discord.ButtonStyle.success,
        custom_id=ENTER_BUTTON_ID,
        emoji="🎉",
    )
    async def enter_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._enter(interaction)

    @discord.ui.button(
        label="Participants",
        style=discord.ButtonStyle.secondary,
        custom_id=COUNT_BUTTON_ID,
        emoji="👥",
    )
    async def participants_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._participants(interaction)

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    @staticmethod
    def _repo(interaction: discord.Interaction) -> GiveawayRepository:
        return GiveawayRepository(interaction.client.db)  # type: ignore[attr-defined]

    @staticmethod
    async def _load(
        interaction: discord.Interaction, repo: GiveawayRepository
    ) -> dict[str, Any] | None:
        message = interaction.message
        if message is None:
            await _reply(interaction, "\u274c I could not identify that giveaway.")
            return None

        try:
            row = await repo.get_by_message(message.id)
        except Exception:
            log.exception("Could not read the giveaway for message %s.", message.id)
            await _reply(
                interaction, "\u274c I could not read that giveaway. Please try again."
            )
            return None

        if row is None:
            await _reply(
                interaction,
                "\u274c This message is no longer linked to a giveaway I track.",
            )
            return None
        return row

    async def _enter(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(
                interaction, "\u274c Giveaways can only be entered inside a server."
            )
            return

        await interaction.response.defer(ephemeral=True)
        repo = self._repo(interaction)

        row = await self._load(interaction, repo)
        if row is None:
            return

        giveaway_id = int(row["giveaway_id"])
        if str(row.get("status")) != "active":
            await _reply(interaction, "\u274c That giveaway is no longer running.")
            return

        ends_at = parse_iso(row.get("ends_at"))
        if ends_at is not None and ends_at <= datetime.now(timezone.utc):
            await _reply(
                interaction,
                "\u274c That giveaway is already ending, so entries are closed.",
            )
            return

        # Leaving is always allowed, so the requirement check only guards joining.
        try:
            already = await repo.has_entry(giveaway_id, member.id)
        except Exception:
            log.exception("Could not read giveaway entries for %s.", giveaway_id)
            await _reply(
                interaction, "\u274c I could not check your entry. Please try again."
            )
            return

        if already:
            try:
                await repo.remove_entry(giveaway_id, member.id)
                total = await repo.count_entries(giveaway_id)
            except Exception:
                log.exception("Could not remove a giveaway entry for %s.", member.id)
                await _reply(
                    interaction, "\u274c Your entry could not be removed right now."
                )
                return

            await _reply(
                interaction,
                "\u2139\ufe0f You have left this giveaway. Press **Enter** again to "
                f"rejoin. Participants: **{total:,}**.",
            )
            return

        refusal = await check_requirements(
            interaction.client.db, member, row  # type: ignore[attr-defined]
        )
        if refusal is not None:
            await _reply(interaction, f"\U0001f6ab {refusal}")
            return

        try:
            inserted = await repo.add_entry(giveaway_id, member.id)
            total = await repo.count_entries(giveaway_id)
        except Exception:
            log.exception("Could not record a giveaway entry for %s.", member.id)
            await _reply(
                interaction,
                "\u274c Your entry could not be recorded. Please try again.",
            )
            return

        if not inserted:
            # The unique index refused a duplicate: a double click landed twice.
            await _reply(
                interaction,
                f"\u2139\ufe0f You are already entered. Participants: **{total:,}**.",
            )
            return

        await _reply(
            interaction,
            "\u2705 You are entered. Good luck! Participants: "
            f"**{total:,}**.\nPress **Enter** again if you change your mind.",
        )

    async def _participants(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(interaction, "\u274c This only works inside a server.")
            return

        await interaction.response.defer(ephemeral=True)
        repo = self._repo(interaction)

        row = await self._load(interaction, repo)
        if row is None:
            return

        giveaway_id = int(row["giveaway_id"])
        try:
            total = await repo.count_entries(giveaway_id)
            entered = await repo.has_entry(giveaway_id, member.id)
        except Exception:
            log.exception("Could not count giveaway entries for %s.", giveaway_id)
            await _reply(
                interaction, "\u274c I could not read that giveaway right now."
            )
            return

        winners = max(1, _as_int(row.get("winner_count"), 1))
        lines = [
            f"👥 **{total:,}** member(s) have entered for **{winners}** winner(s).",
            "You are entered." if entered else "You have not entered yet.",
        ]

        ends_at = parse_iso(row.get("ends_at"))
        if str(row.get("status")) == "active" and ends_at is not None:
            remaining = (ends_at - datetime.now(timezone.utc)).total_seconds()
            lines.append(f"Time left: **{format_delta(remaining)}**.")

        await _reply(interaction, "\n".join(lines))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Giveaways(commands.Cog):
    """Giveaway hosting, entry tracking and winner selection."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = GiveawayRepository(self.db)
        self._random = random.SystemRandom()

        # giveaway_id -> monotonic timestamp of the last countdown edit.
        self._last_refresh: dict[int, float] = {}
        # Guards against the scheduler and a command ending the same giveaway.
        self._finishing: set[int] = set()

    async def cog_load(self) -> None:
        # Registering the persistent view here keeps panels posted before a
        # restart interactive.
        self.bot.add_view(GiveawayView())
        self._scheduler.start()

    async def cog_unload(self) -> None:
        self._scheduler.cancel()

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    async def _respond(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )

    async def _reject(self, interaction: discord.Interaction, reason: str) -> None:
        await self._respond(interaction, f"\u274c {reason}")

    async def _authorize(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Re-checks ``Manage Server`` server side."""
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return None

        permissions = member.guild_permissions
        if not (permissions.administrator or permissions.manage_guild):
            await self._reject(
                interaction, "You need the `Manage Server` permission for that."
            )
            return None

        if guild.me is None:
            await self._reject(
                interaction, "I could not resolve my own membership in this server."
            )
            return None

        return guild, member

    async def _audit(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        action: str,
        detail: str,
    ) -> None:
        embed = discord.Embed(
            title=f"Giveaways: {action}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Performed by", value=f"{actor} (`{actor.id}`)", inline=False
        )
        embed.add_field(name="Details", value=detail[:1024], inline=False)
        await send_log(self.db, guild, embed)

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    @tasks.loop(seconds=SCHEDULER_SECONDS)
    async def _scheduler(self) -> None:
        """Refreshes live countdowns and ends giveaways whose time has come."""
        try:
            rows = await self.repo.list_active()
        except Exception:
            log.exception("Could not read the active giveaways.")
            return

        now = datetime.now(timezone.utc)
        monotonic = time.monotonic()
        tracked: set[int] = set()

        for row in rows:
            giveaway_id = _as_int(row.get("giveaway_id"))
            if not giveaway_id:
                continue
            tracked.add(giveaway_id)

            ends_at = parse_iso(row.get("ends_at"))
            if ends_at is None:
                # An unreadable end time would keep the giveaway alive forever.
                log.warning(
                    "Giveaway %s has an unreadable end time; ending it now.",
                    giveaway_id,
                )
                await self._finish(row)
                continue

            if ends_at <= now:
                await self._finish(row)
                continue

            remaining = (ends_at - now).total_seconds()
            await self._maybe_refresh(row, remaining, monotonic)

        # Drop refresh bookkeeping for giveaways that are no longer active.
        for giveaway_id in list(self._last_refresh):
            if giveaway_id not in tracked:
                del self._last_refresh[giveaway_id]

    @_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    @_scheduler.error
    async def _scheduler_error(self, error: BaseException) -> None:
        # tasks.loop stops on an unhandled exception, so restart it explicitly.
        log.error("The giveaway scheduler failed; restarting it.", exc_info=error)
        if not self._scheduler.is_running():
            self._scheduler.start()

    async def _maybe_refresh(
        self, row: Mapping[str, Any], remaining: float, monotonic: float
    ) -> None:
        """Edits the giveaway message when its countdown is due a refresh."""
        giveaway_id = int(row["giveaway_id"])
        interval = refresh_interval(remaining)
        last = self._last_refresh.get(giveaway_id)
        if last is not None and monotonic - last < interval:
            return
        self._last_refresh[giveaway_id] = monotonic

        message = self._partial_message(row)
        if message is None:
            return

        guild = self.bot.get_guild(int(row["guild_id"]))
        embed = giveaway_embed(
            guild, row, entry_count=_as_int(row.get("entry_count"), 0)
        )

        try:
            await message.edit(embed=embed, view=GiveawayView())
        except discord.NotFound:
            # The panel is gone, so nobody could ever enter or see the result.
            log.info("Giveaway %s lost its message; marking it cancelled.", giveaway_id)
            try:
                await self.repo.cancel(giveaway_id)
            except Exception:
                log.exception("Could not cancel giveaway %s.", giveaway_id)
        except discord.Forbidden:
            log.debug(
                "Missing permission to refresh the giveaway %s countdown.",
                giveaway_id,
            )
        except discord.HTTPException as exc:
            log.debug("Could not refresh giveaway %s: %s", giveaway_id, exc)

    def _partial_message(self, row: Mapping[str, Any]) -> discord.PartialMessage | None:
        """Returns a partial message for the giveaway panel.

        A partial message can be edited without fetching it first, which keeps
        the countdown refresh down to a single API call.
        """
        guild = self.bot.get_guild(_as_int(row.get("guild_id")))
        if guild is None:
            return None

        channel = guild.get_channel_or_thread(_as_int(row.get("channel_id")))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None

        message_id = _as_int(row.get("message_id"))
        if not message_id:
            return None
        return channel.get_partial_message(message_id)

    # ------------------------------------------------------------------
    # Drawing and ending
    # ------------------------------------------------------------------

    async def _resolve_member(
        self, guild: discord.Guild, user_id: int
    ) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        except discord.HTTPException as exc:
            log.debug(
                "Could not fetch member %s in guild %s: %s", user_id, guild.id, exc
            )
            return None

    async def _draw(
        self,
        guild: discord.Guild,
        entries: Sequence[Mapping[str, Any]],
        *,
        count: int,
        exclude: set[int],
    ) -> list[int]:
        """Draws up to ``count`` winners, weighted by their entry weight.

        Candidates are validated against the live guild, because someone who
        left must not win. Validation may need a REST fetch when the member
        cache is cold, so the number of attempts is bounded; a giveaway whose
        entrants have mostly left may therefore return fewer winners than
        requested rather than issuing hundreds of requests.
        """
        pool: list[tuple[int, int]] = []
        for entry in list(entries)[:MAX_ENTRY_SCAN]:
            user_id = _as_int(entry.get("user_id"))
            if not user_id or user_id in exclude:
                continue
            weight = max(1, _as_int(entry.get("entries"), 1))
            pool.append((user_id, weight))

        winners: list[int] = []
        attempts = 0
        attempt_limit = DRAW_ATTEMPT_BASE + max(1, count) * DRAW_ATTEMPT_MULTIPLIER

        while pool and len(winners) < max(1, count) and attempts < attempt_limit:
            attempts += 1
            index = _weighted_index(self._random, [weight for _, weight in pool])
            user_id, _ = pool.pop(index)

            member = await self._resolve_member(guild, user_id)
            if member is None or member.bot:
                continue
            winners.append(user_id)

        return winners

    async def _finish(
        self,
        row: Mapping[str, Any],
        *,
        ended_by: discord.abc.User | None = None,
    ) -> list[int] | None:
        """Ends a giveaway, draws winners and announces them.

        Returns the winner ids, or ``None`` when the giveaway had already been
        ended by another code path.
        """
        giveaway_id = int(row["giveaway_id"])
        if giveaway_id in self._finishing:
            return None
        self._finishing.add(giveaway_id)

        try:
            try:
                claimed = await self.repo.claim_ended(giveaway_id)
            except Exception:
                log.exception("Could not mark giveaway %s ended.", giveaway_id)
                return None

            if not claimed:
                return None

            self._last_refresh.pop(giveaway_id, None)

            guild = self.bot.get_guild(int(row["guild_id"]))
            winners: list[int] = []
            entry_count = 0

            if guild is None:
                log.info(
                    "Giveaway %s ended while its guild was unavailable; no "
                    "winners were drawn.",
                    giveaway_id,
                )
            else:
                try:
                    entries = await self.repo.entries(giveaway_id)
                except Exception:
                    log.exception(
                        "Could not read entries for giveaway %s.", giveaway_id
                    )
                    entries = []

                entry_count = len(entries)
                winners = await self._draw(
                    guild,
                    entries,
                    count=max(1, _as_int(row.get("winner_count"), 1)),
                    exclude=set(winner_ids(row)),
                )

            try:
                await self.repo.store_winners(giveaway_id, winners)
            except Exception:
                log.exception("Could not store winners for giveaway %s.", giveaway_id)

            fresh = await self._safe_get(giveaway_id) or dict(row)
            await self._publish_result(
                fresh,
                winners,
                entry_count=entry_count,
                ended_by=ended_by,
                rerolled=False,
            )
            return winners
        finally:
            self._finishing.discard(giveaway_id)

    async def _safe_get(self, giveaway_id: int) -> dict[str, Any] | None:
        try:
            return await self.repo.get(giveaway_id)
        except Exception:
            log.exception("Could not re-read giveaway %s.", giveaway_id)
            return None

    async def _publish_result(
        self,
        row: Mapping[str, Any],
        winners: Sequence[int],
        *,
        entry_count: int,
        ended_by: discord.abc.User | None,
        rerolled: bool,
    ) -> None:
        """Edits the panel and announces the outcome in the giveaway channel."""
        guild = self.bot.get_guild(_as_int(row.get("guild_id")))
        if guild is None:
            return

        message = self._partial_message(row)
        if message is not None and not rerolled:
            try:
                await message.edit(
                    embed=giveaway_embed(guild, row, entry_count=entry_count),
                    view=GiveawayView.ended(),
                )
            except discord.NotFound:
                message = None
            except discord.HTTPException as exc:
                log.debug("Could not close the giveaway panel: %s", exc)

        channel = guild.get_channel_or_thread(_as_int(row.get("channel_id")))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        me = guild.me
        if me is None:
            return
        missing = missing_channel_permissions(
            me, channel, view_channel=True, send_messages=True, embed_links=True
        )
        if missing:
            log.info(
                "Cannot announce giveaway %s in guild %s: missing %s.",
                row.get("giveaway_id"),
                guild.id,
                ", ".join(missing),
            )
            return

        prize = str(row.get("prize") or "the prize")
        jump = message.jump_url if message is not None else None

        embed = discord.Embed(
            title="🎉 Giveaway rerolled" if rerolled else "🎉 Giveaway ended",
            description=f"**{prize}**"[:4000],
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Winner(s)",
            value=(
                ", ".join(f"<@{user_id}>" for user_id in winners)[:1024]
                if winners
                else "No valid entries"
            ),
            inline=False,
        )
        if entry_count:
            embed.add_field(name="Entries", value=f"{entry_count:,}", inline=True)
        host_id = row.get("host_id")
        if host_id:
            embed.add_field(name="Hosted by", value=f"<@{int(host_id)}>", inline=True)
        if ended_by is not None:
            embed.add_field(
                name="Ended by", value=f"{ended_by} (`{ended_by.id}`)", inline=True
            )
        if jump:
            embed.add_field(
                name="Giveaway", value=f"[Open the message]({jump})", inline=False
            )
        giveaway_id = row.get("giveaway_id")
        if giveaway_id:
            embed.set_footer(text=f"Giveaway #{int(giveaway_id)}")

        if winners:
            mentions = ", ".join(f"<@{user_id}>" for user_id in winners)
            content = f"🎉 Congratulations {mentions}! You won **{prize[:150]}**."
            allowed = discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[discord.Object(id=int(user_id)) for user_id in winners],
                replied_user=False,
            )
        else:
            content = (
                "Nobody could be drawn for this giveaway \u2014 there were no "
                "eligible entries."
            )
            allowed = NO_MENTIONS

        try:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            log.warning("Could not announce a giveaway result: %s", exc)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    async def _resolve(self, guild_id: int, raw: str) -> dict[str, Any] | None:
        """Resolves a giveaway id, message id or message link for a guild."""
        text = (raw or "").strip()
        if not text:
            return None

        link = MESSAGE_LINK_RE.search(text)
        if link is not None:
            row = await self.repo.get_by_message(int(link.group("message")))
            if row is not None and int(row["guild_id"]) == guild_id:
                return row
            return None

        if text.startswith("#"):
            text = text[1:]
        if not text.isdigit():
            return None

        value = int(text)
        row = await self.repo.get(value)
        if row is not None and int(row["guild_id"]) == guild_id:
            return row

        row = await self.repo.get_by_message(value)
        if row is not None and int(row["guild_id"]) == guild_id:
            return row
        return None

    async def _autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        statuses: Sequence[str],
    ) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild_id
        if guild_id is None:
            return []

        try:
            rows = await self.repo.list_for_guild(
                guild_id, statuses=list(statuses), limit=25
            )
        except Exception:
            log.exception("Could not autocomplete giveaways for guild %s.", guild_id)
            return []

        needle = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            giveaway_id = _as_int(row.get("giveaway_id"))
            prize = str(row.get("prize") or "giveaway")
            label = f"#{giveaway_id} \u2022 {prize}"[:100]
            if needle and needle not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=str(giveaway_id)))
            if len(choices) >= 25:
                break
        return choices

    # ------------------------------------------------------------------
    # /giveaway-start
    # ------------------------------------------------------------------

    @app_commands.command(
        name="giveaway-start",
        description="Start a giveaway with a live countdown and entry button.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        prize="What is being given away",
        duration="How long it runs, for example 45m, 2h30m, 3d",
        winners="How many winners to draw",
        channel="Where the giveaway is posted (defaults to this channel)",
        description="Extra details shown above the entry button",
        required_role="Only members with this role may enter",
        required_level="Minimum Fyrion level required to enter",
    )
    async def giveaway_start_cmd(
        self,
        interaction: discord.Interaction,
        prize: app_commands.Range[str, 1, MAX_PRIZE_LENGTH],
        duration: app_commands.Range[str, 1, 32],
        winners: app_commands.Range[int, 1, MAX_WINNERS] = 1,
        channel: Optional[discord.TextChannel] = None,
        description: Optional[
            app_commands.Range[str, 1, MAX_DESCRIPTION_LENGTH]
        ] = None,
        required_role: Optional[discord.Role] = None,
        required_level: Optional[
            app_commands.Range[int, 1, MAX_LEVEL_REQUIREMENT]
        ] = None,
    ) -> None:
        context = await self._authorize(interaction)
        if context is None:
            return
        guild, member = context

        me = guild.me
        assert me is not None  # guaranteed by _authorize

        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return

        target = channel if channel is not None else interaction.channel
        if not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
            await self._reject(
                interaction,
                "Giveaways must be posted to a text channel in this server.",
            )
            return

        missing = missing_channel_permissions(
            me, target, view_channel=True, send_messages=True, embed_links=True
        )
        if missing:
            names = ", ".join(f"`{name}`" for name in missing)
            await self._reject(
                interaction, f"I am missing {names} in {target.mention}."
            )
            return

        invoker_missing = missing_channel_permissions(
            member, target, view_channel=True, send_messages=True
        )
        if invoker_missing:
            names = ", ".join(f"`{name}`" for name in invoker_missing)
            await self._reject(
                interaction, f"You are missing {names} in {target.mention}."
            )
            return

        if required_role is not None:
            if required_role.guild.id != guild.id:
                await self._reject(
                    interaction, "That role does not belong to this server."
                )
                return
            if required_role.is_default():
                await self._reject(
                    interaction,
                    "`@everyone` is not a useful requirement \u2014 leave the "
                    "option empty to let everyone enter.",
                )
                return

        await interaction.response.defer(ephemeral=True)

        try:
            active = await self.repo.count_active(guild.id)
        except Exception:
            log.exception("Could not count active giveaways for guild %s.", guild.id)
            await self._reject(
                interaction, "I could not read this server's giveaways. Try again."
            )
            return

        if active >= MAX_ACTIVE_PER_GUILD:
            await self._reject(
                interaction,
                f"This server already has {MAX_ACTIVE_PER_GUILD} running "
                "giveaways, which is the maximum. End one first.",
            )
            return

        ends_iso = iso_from_now(seconds)
        draft: dict[str, Any] = {
            "giveaway_id": None,
            "guild_id": guild.id,
            "channel_id": target.id,
            "host_id": member.id,
            "prize": str(prize).strip(),
            "description": description.strip() if description else None,
            "winner_count": int(winners),
            "required_role_id": required_role.id if required_role is not None else None,
            "required_level": int(required_level) if required_level else None,
            "status": "active",
            "ends_at": ends_iso,
            "winners": "[]",
            "entry_count": 0,
        }

        try:
            message = await target.send(
                embed=giveaway_embed(guild, draft, entry_count=0),
                view=GiveawayView(),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.Forbidden:
            await self._reject(
                interaction,
                f"Discord refused to post the giveaway in {target.mention}.",
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not post a giveaway in guild %s: %s", guild.id, exc)
            await self._reject(
                interaction, f"Discord rejected the giveaway (HTTP {exc.status})."
            )
            return

        try:
            row = await self.repo.create(
                guild_id=guild.id,
                channel_id=target.id,
                message_id=message.id,
                host_id=member.id,
                prize=draft["prize"],
                winner_count=int(winners),
                ends_at=ends_iso,
                description=draft["description"],
                required_role_id=draft["required_role_id"],
                required_level=draft["required_level"],
            )
        except Exception:
            log.exception(
                "Could not record the giveaway for guild %s; rolling back the "
                "message.",
                guild.id,
            )
            # An untracked giveaway could never be ended, so withdraw the panel.
            try:
                await message.delete()
            except discord.HTTPException:
                log.warning(
                    "Orphaned giveaway message %s could not be removed.", message.id
                )
            await self._reject(
                interaction,
                "The giveaway could not be recorded, so it was removed again. "
                "Please try once more.",
            )
            return

        # One extra edit so the footer carries the giveaway number the
        # management commands refer to.
        try:
            await message.edit(
                embed=giveaway_embed(guild, row, entry_count=0), view=GiveawayView()
            )
        except discord.HTTPException:
            pass

        ends_at = parse_iso(ends_iso)
        lines = [
            f"\u2705 Giveaway **#{int(row['giveaway_id'])}** started in "
            f"{target.mention}: {message.jump_url}",
            f"Prize: **{draft['prize'][:150]}** \u2022 winners: **{winners}** "
            f"\u2022 runs for **{format_delta(seconds)}**",
        ]
        if ends_at is not None:
            lines.append(f"Ends {discord.utils.format_dt(ends_at, style='F')}.")
        if required_role is not None:
            lines.append(f"Only members with {required_role.mention} may enter.")
        if required_level:
            settings = await self.db.get_guild_settings(guild.id)
            if not settings.get("leveling_enabled"):
                lines.append(
                    "\u26a0\ufe0f Leveling is disabled in this server, so most "
                    "members will not meet the level requirement."
                )

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

        await self._audit(
            guild,
            member,
            "Giveaway started",
            f"Giveaway: #{int(row['giveaway_id'])}\n"
            f"Prize: {draft['prize']}\n"
            f"Channel: #{target.name} (`{target.id}`)\n"
            f"Winners: {winners}\nEnds at: {ends_iso}",
        )

    # ------------------------------------------------------------------
    # /giveaway-end
    # ------------------------------------------------------------------

    @app_commands.command(
        name="giveaway-end",
        description="End a running giveaway now and draw its winners.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        giveaway="Pick a running giveaway, or paste its message link or ID"
    )
    async def giveaway_end_cmd(
        self, interaction: discord.Interaction, giveaway: str
    ) -> None:
        context = await self._authorize(interaction)
        if context is None:
            return
        guild, member = context

        await interaction.response.defer(ephemeral=True)

        try:
            row = await self._resolve(guild.id, giveaway)
        except Exception:
            log.exception("Could not resolve a giveaway in guild %s.", guild.id)
            await self._reject(
                interaction, "I could not read that giveaway. Please try again."
            )
            return

        if row is None:
            await self._reject(
                interaction,
                "I could not find that giveaway in this server. Pick one from the "
                "list, or paste its message link.",
            )
            return

        status = str(row.get("status") or "")
        if status != "active":
            await self._reject(
                interaction,
                f"Giveaway #{int(row['giveaway_id'])} is already **{status}**. Use "
                "`/giveaway-reroll` to draw a replacement winner.",
            )
            return

        winners = await self._finish(row, ended_by=member)
        if winners is None:
            await self._reject(
                interaction, "That giveaway was already being ended elsewhere."
            )
            return

        giveaway_id = int(row["giveaway_id"])
        if winners:
            summary = ", ".join(f"<@{user_id}>" for user_id in winners)
            body = f"Winner(s): {summary}"
        else:
            body = (
                "No winner could be drawn \u2014 there were no eligible entries "
                "left in the server."
            )

        await interaction.followup.send(
            f"\u2705 Giveaway **#{giveaway_id}** ended.\n{body}",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

        await self._audit(
            guild,
            member,
            "Giveaway ended early",
            f"Giveaway: #{giveaway_id}\nPrize: {row.get('prize')}\n"
            f"Winners drawn: {len(winners)}",
        )

    @giveaway_end_cmd.autocomplete("giveaway")
    async def _end_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current, ("active",))

    # ------------------------------------------------------------------
    # /giveaway-reroll
    # ------------------------------------------------------------------

    @app_commands.command(
        name="giveaway-reroll",
        description="Draw replacement winners for a giveaway that already ended.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        giveaway="Pick a finished giveaway, or paste its message link or ID",
        winners="How many replacement winners to draw",
    )
    async def giveaway_reroll_cmd(
        self,
        interaction: discord.Interaction,
        giveaway: str,
        winners: app_commands.Range[int, 1, MAX_WINNERS] = 1,
    ) -> None:
        context = await self._authorize(interaction)
        if context is None:
            return
        guild, member = context

        await interaction.response.defer(ephemeral=True)

        try:
            row = await self._resolve(guild.id, giveaway)
        except Exception:
            log.exception("Could not resolve a giveaway in guild %s.", guild.id)
            await self._reject(
                interaction, "I could not read that giveaway. Please try again."
            )
            return

        if row is None:
            await self._reject(
                interaction,
                "I could not find that giveaway in this server. Pick one from the "
                "list, or paste its message link.",
            )
            return

        status = str(row.get("status") or "")
        if status == "active":
            await self._reject(
                interaction,
                "That giveaway is still running. End it with `/giveaway-end` " "first.",
            )
            return
        if status != "ended":
            await self._reject(interaction, "A cancelled giveaway cannot be rerolled.")
            return

        giveaway_id = int(row["giveaway_id"])
        previous = winner_ids(row)

        try:
            entries = await self.repo.entries(giveaway_id)
        except Exception:
            log.exception("Could not read entries for giveaway %s.", giveaway_id)
            await self._reject(
                interaction, "I could not read that giveaway's entries. Try again."
            )
            return

        drawn = await self._draw(
            guild, entries, count=int(winners), exclude=set(previous)
        )

        if not drawn:
            await interaction.followup.send(
                "\u2139\ufe0f No replacement winner could be drawn: every "
                "remaining entrant has already won or is no longer in the server.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        # Past winners are retained so a later reroll never picks them again.
        combined = previous + [user_id for user_id in drawn if user_id not in previous]
        try:
            await self.repo.store_winners(giveaway_id, combined)
        except Exception:
            log.exception("Could not store rerolled winners for %s.", giveaway_id)

        fresh = await self._safe_get(giveaway_id) or dict(row)
        await self._publish_result(
            fresh,
            drawn,
            entry_count=len(entries),
            ended_by=member,
            rerolled=True,
        )

        summary = ", ".join(f"<@{user_id}>" for user_id in drawn)
        await interaction.followup.send(
            f"\u2705 Rerolled giveaway **#{giveaway_id}**.\nNew winner(s): {summary}\n"
            f"{len(previous)} previous winner(s) were excluded from the draw.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

        await self._audit(
            guild,
            member,
            "Giveaway rerolled",
            f"Giveaway: #{giveaway_id}\nPrize: {row.get('prize')}\n"
            f"New winners: {len(drawn)}\nExcluded: {len(previous)}",
        )

    @giveaway_reroll_cmd.autocomplete("giveaway")
    async def _reroll_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current, ("ended",))

    # ------------------------------------------------------------------
    # /giveaway-list
    # ------------------------------------------------------------------

    @app_commands.command(
        name="giveaway-list",
        description="List this server's giveaways and how many members entered.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        status="Which giveaways to list (running ones by default)",
        limit="How many to show (1-15)",
    )
    async def giveaway_list_cmd(
        self,
        interaction: discord.Interaction,
        status: StatusFilter = "active",
        limit: app_commands.Range[int, 1, MAX_LISTED] = MAX_LISTED,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return

        await interaction.response.defer(ephemeral=True)

        statuses = None if status == "all" else (status,)
        try:
            rows = await self.repo.list_for_guild(
                guild.id, statuses=statuses, limit=int(limit)
            )
            active_total = await self.repo.count_active(guild.id)
        except Exception:
            log.exception("Could not list giveaways for guild %s.", guild.id)
            await self._reject(
                interaction, "I could not read this server's giveaways. Try again."
            )
            return

        if not rows:
            await interaction.followup.send(
                f"\u2139\ufe0f There are no **{status}** giveaways in this server.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        lines: list[str] = []
        for row in rows:
            giveaway_id = _as_int(row.get("giveaway_id"))
            prize = str(row.get("prize") or "a prize")[:60]
            entries = _as_int(row.get("entry_count"), 0)
            row_status = str(row.get("status") or "active")
            channel = guild.get_channel_or_thread(_as_int(row.get("channel_id")))
            location = (
                channel.mention
                if channel is not None
                else f"deleted channel `{_as_int(row.get('channel_id'))}`"
            )

            detail = (
                f"**#{giveaway_id}** \u2022 {prize} \u2022 {location} "
                f"\u2022 {entries:,} entr{'y' if entries == 1 else 'ies'}"
            )

            if row_status == "active":
                ends_at = parse_iso(row.get("ends_at"))
                if ends_at is not None:
                    detail += (
                        " \u2022 ends " f"{discord.utils.format_dt(ends_at, style='R')}"
                    )
            else:
                winners = winner_ids(row)
                detail += f" \u2022 **{row_status}**"
                if winners:
                    detail += " \u2022 won by " + ", ".join(
                        f"<@{user_id}>" for user_id in winners[:5]
                    )

            lines.append(detail)

        embed = discord.Embed(
            title=f"Giveaways: {guild.name}",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"{active_total} running \u2022 use /giveaway-end or "
                "/giveaway-reroll with the number shown"
            )
        )

        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))


__all__ = [
    "Giveaways",
    "GiveawayRepository",
    "GiveawayView",
    "ENTER_BUTTON_ID",
    "COUNT_BUTTON_ID",
    "MAX_ACTIVE_PER_GUILD",
    "MAX_WINNERS",
    "check_requirements",
    "format_delta",
    "giveaway_embed",
    "parse_duration",
    "parse_iso",
    "refresh_interval",
    "winner_ids",
    "setup",
]
