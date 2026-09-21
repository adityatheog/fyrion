"""
Fun and entertainment commands.

Six commands live here. Three are entirely local (``/8ball``, ``/coinflip``,
``/roll``) and three call an upstream HTTP service (``/meme``, ``/quote``,
``/urban``).

Every network call goes through one shared :class:`httpx.AsyncClient`, created on
cog load and closed on unload, so connections are pooled instead of being rebuilt
per invocation. Each request is bounded: a total timeout, a response size cap, a
verified final host, and a per-user command cooldown. ``httpx`` is imported
defensively, so a deployment without it still gets the three offline commands
instead of losing the whole cog.

Safety properties worth stating explicitly:

* Upstream payloads are untrusted input. Nothing returned by an API is executed
  or interpreted: text is escaped, mentions are defused, lengths are truncated,
  and URLs are validated against the expected host before being handed to
  Discord.
* Command arguments are attacker controlled too, so every reply disables mention
  parsing entirely. A crafted question can never make Fyrion ping a role or
  ``@everyone``.
* ``/meme`` and ``/urban`` surface user-generated content from third parties.
  Results flagged NSFW are dropped unless the channel is age restricted, and
  Urban Dictionary definitions are only posted publicly in age-restricted
  channels; elsewhere they are returned ephemerally with a warning.
* Dice expressions are parsed with a bounded regex and hard caps on dice count,
  side count and term count, so no input can turn into unbounded work.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.config import Config

try:  # pragma: no cover - depends on the deployment's dependencies
    import httpx
except ImportError:  # pragma: no cover - optional at runtime
    httpx = None  # type: ignore[assignment]

log = logging.getLogger("fyrion.cogs.fun")

# Fun commands echo user text and third-party content, so nothing they post is
# ever allowed to mention anyone.
NO_MENTIONS: Final[discord.AllowedMentions] = discord.AllowedMentions.none()

HTTPX_AVAILABLE: Final[bool] = httpx is not None

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT: Final[float] = 8.0
CONNECT_TIMEOUT: Final[float] = 5.0
# Upper bound on a single upstream response. All three APIs answer with a few
# kilobytes of JSON; anything larger is a bug or an attack.
MAX_RESPONSE_BYTES: Final[int] = 256 * 1024
MAX_REDIRECTS: Final[int] = 3

MEME_API_URL: Final[str] = "https://meme-api.com/gimme"
MEME_API_HOST: Final[str] = "meme-api.com"
MEME_ATTEMPTS: Final[int] = 3

QUOTE_API_URL: Final[str] = "https://zenquotes.io/api/random"
QUOTE_API_HOST: Final[str] = "zenquotes.io"

URBAN_API_URL: Final[str] = "https://api.urbandictionary.com/v0/define"
URBAN_API_HOST: Final[str] = "urbandictionary.com"
URBAN_LINK_HOST: Final[str] = "urbandictionary.com"

REDDIT_LINK_HOSTS: Final[tuple[str, ...]] = ("reddit.com", "redd.it")
IMAGE_HOSTS: Final[tuple[str, ...]] = (
    "redd.it",
    "reddit.com",
    "redditmedia.com",
    "imgur.com",
    "giphy.com",
)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_QUESTION_LENGTH: Final[int] = 250
MAX_TERM_LENGTH: Final[int] = 80
MAX_EXPRESSION_LENGTH: Final[int] = 64
MAX_COINS: Final[int] = 20
FIELD_VALUE_LIMIT: Final[int] = 1024

MAX_DICE_GROUPS: Final[int] = 6
MAX_DICE_PER_ROLL: Final[int] = 100
MIN_DIE_SIDES: Final[int] = 2
MAX_DIE_SIDES: Final[int] = 1000
MAX_FLAT_MODIFIER: Final[int] = 100_000
MAX_SHOWN_ROLLS: Final[int] = 25

SUBREDDIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]{2,21}$")

# Bounded quantifiers only: this pattern cannot backtrack catastrophically.
DICE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<sign>[+-])?(?:(?P<count>\d{1,3})?d(?P<sides>\d{1,4})|(?P<flat>\d{1,6}))"
)

# ---------------------------------------------------------------------------
# Static content
# ---------------------------------------------------------------------------

AFFIRMATIVE: Final[tuple[str, ...]] = (
    "It is certain.",
    "It is decidedly so.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
)

NONCOMMITTAL: Final[tuple[str, ...]] = (
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "I cannot predict right now.",
    "Concentrate and ask again.",
)

NEGATIVE: Final[tuple[str, ...]] = (
    "Do not count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
)

# Used when the quote service is unreachable or rate limiting Fyrion, so the
# command still answers instead of failing.
FALLBACK_QUOTES: Final[tuple[tuple[str, str], ...]] = (
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("Simplicity is the ultimate sophistication.", "Leonardo da Vinci"),
    ("Whether you think you can or you cannot, you are right.", "Henry Ford"),
    ("It always seems impossible until it is done.", "Nelson Mandela"),
    ("Fall seven times, stand up eight.", "Japanese proverb"),
    (
        "The best time to plant a tree was twenty years ago. The second best time is now.",
        "Chinese proverb",
    ),
    ("What we do now echoes in eternity.", "Marcus Aurelius"),
    (
        "Perfection is achieved when there is nothing left to take away.",
        "Antoine de Saint-Exupery",
    ),
)

COIN_FACES: Final[tuple[str, str]] = ("Heads", "Tails")


class ServiceError(RuntimeError):
    """Raised when an upstream service cannot satisfy a request.

    The message is user facing, so it never carries a URL, a payload excerpt or
    any other internal detail.
    """


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def sanitize_inline(text: Any, limit: int = 200) -> str:
    """Collapses text to one line, defuses mentions and escapes markdown."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return "\u2014"
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "\u2026"
    return discord.utils.escape_markdown(collapsed.replace("@", "@\u200b"))


def sanitize_block(
    text: Any, limit: int = FIELD_VALUE_LIMIT, max_lines: int = 12
) -> str:
    """Sanitizes a multi-line body while keeping its paragraph structure."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    kept = [line for line in lines if line][:max_lines]
    if not kept:
        return "\u2014"

    joined = "\n".join(kept)
    if len(joined) > limit:
        joined = joined[: limit - 1] + "\u2026"
    return discord.utils.escape_markdown(joined.replace("@", "@\u200b"))


def strip_urban_brackets(text: Any) -> str:
    """Removes Urban Dictionary's cross-reference brackets, keeping the words."""
    return re.sub(r"[\[\]]", "", str(text or ""))


def safe_url(value: Any, *allowed_hosts: str) -> str | None:
    """Returns an https URL only when its host matches an allow-listed domain."""
    if not value:
        return None

    candidate = str(value).strip()
    if len(candidate) > 500:
        return None

    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        return None

    host = parsed.hostname.lower()
    for allowed in allowed_hosts:
        allowed = allowed.lower()
        if host == allowed or host.endswith(f".{allowed}"):
            return candidate
    return None


def channel_is_nsfw(interaction: discord.Interaction) -> bool:
    """Returns True when the invoking channel is age restricted.

    Threads inherit the flag from their parent channel; direct messages are
    treated as *not* age restricted, which is the conservative answer.
    """
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return bool(parent is not None and parent.is_nsfw())

    checker = getattr(channel, "is_nsfw", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # pragma: no cover - defensive
            return False
    return False


# ---------------------------------------------------------------------------
# Dice
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiceResult:
    """The outcome of one dice expression."""

    total: int
    dice_used: int
    breakdown: tuple[str, ...]

    def render(self) -> str:
        joined = " ".join(self.breakdown)
        return joined[1:].lstrip() if joined.startswith("+") else joined


def roll_expression(expression: str, rng: random.Random) -> DiceResult:
    """Parses and evaluates dice notation such as ``2d6+3``.

    Raises:
        ValueError: with a user-facing message when the expression is unusable.
    """
    text = (expression or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("No dice expression was supplied. Try `2d6+3`.")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ValueError(
            f"The expression must be at most {MAX_EXPRESSION_LENGTH} characters."
        )

    position = 0
    groups = 0
    total = 0
    dice_used = 0
    breakdown: list[str] = []

    while position < len(text):
        match = DICE_PATTERN.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(
                "I could not read that expression. Use dice notation such as "
                "`1d20`, `2d6+3` or `4d8-2`."
            )
        position = match.end()

        groups += 1
        if groups > MAX_DICE_GROUPS:
            raise ValueError(f"Use at most {MAX_DICE_GROUPS} terms in one roll.")

        negative = match.group("sign") == "-"
        sign = -1 if negative else 1
        prefix = "-" if negative else "+"

        flat = match.group("flat")
        if flat is not None:
            value = int(flat)
            if value > MAX_FLAT_MODIFIER:
                raise ValueError(f"Modifiers are limited to {MAX_FLAT_MODIFIER:,}.")
            total += sign * value
            breakdown.append(f"{prefix} {value}")
            continue

        count = int(match.group("count") or 1)
        sides = int(match.group("sides"))

        if count < 1:
            raise ValueError("You must roll at least one die.")
        if not MIN_DIE_SIDES <= sides <= MAX_DIE_SIDES:
            raise ValueError(
                f"Dice must have between {MIN_DIE_SIDES} and {MAX_DIE_SIDES} sides."
            )
        if dice_used + count > MAX_DICE_PER_ROLL:
            raise ValueError(f"You can roll at most {MAX_DICE_PER_ROLL} dice at once.")

        rolls = [rng.randint(1, sides) for _ in range(count)]
        dice_used += count
        subtotal = sum(rolls)
        total += sign * subtotal

        shown = ", ".join(str(value) for value in rolls[:MAX_SHOWN_ROLLS])
        if count > MAX_SHOWN_ROLLS:
            shown += f", \u2026 (+{count - MAX_SHOWN_ROLLS} more)"
        breakdown.append(f"{prefix} {count}d{sides} [{shown}] = {subtotal}")

    if dice_used == 0:
        raise ValueError("That expression contains no dice. Try `2d6+3`.")

    return DiceResult(total=total, dice_used=dice_used, breakdown=tuple(breakdown))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Fun(commands.Cog):
    """Games, randomness and lookups."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # SystemRandom so results are not predictable from previous outputs.
        self._rng = random.SystemRandom()
        self._client: Any | None = None

    async def cog_load(self) -> None:
        if not HTTPX_AVAILABLE:
            log.warning(
                "httpx is not installed, so /meme, /quote and /urban will report "
                "that they are unavailable. Install it with: "
                "pip install -r requirements.txt"
            )
            return

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers={
                "User-Agent": (
                    f"Fyrion/{Config.VERSION} "
                    "(+https://github.com/adityatheog/fyrion)"
                ),
                "Accept": "application/json",
            },
        )

    async def cog_unload(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - shutdown best effort
                log.debug("The fun cog's HTTP client did not close cleanly.")

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send(
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ) -> None:
        """Replies once, whether or not the interaction was deferred."""
        try:
            # discord.py's typed send overloads use MISSING (not None) for an
            # omitted content/embed; both mean "not supplied" at runtime.
            send_content = content if content is not None else discord.utils.MISSING
            send_embed = embed if embed is not None else discord.utils.MISSING
            if interaction.response.is_done():
                await interaction.followup.send(
                    content=send_content,
                    embed=send_embed,
                    ephemeral=ephemeral,
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                await interaction.response.send_message(
                    content=send_content,
                    embed=send_embed,
                    ephemeral=ephemeral,
                    allowed_mentions=NO_MENTIONS,
                )
        except discord.NotFound:
            log.debug("A fun command interaction expired before it was answered.")
        except discord.HTTPException as exc:
            log.warning("Could not answer a fun command interaction: %s", exc)

    async def _reject(self, interaction: discord.Interaction, reason: str) -> None:
        await self._send(interaction, content=f"\u274c {reason}", ephemeral=True)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get_json(
        self,
        url: str,
        *,
        host: str,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        """Performs a bounded GET and returns the decoded JSON payload.

        Raises:
            ServiceError: with a user-facing message on any failure.
        """
        client = self._client
        if client is None:
            raise ServiceError(
                "This command needs the `httpx` package, which is not installed "
                "on this instance."
            )

        try:
            response = await client.get(url, params=dict(params or {}))
        except httpx.TimeoutException:
            raise ServiceError(
                "The service did not respond in time. Please try again shortly."
            ) from None
        except httpx.HTTPError as exc:
            log.warning("Request to %s failed: %s", url, exc)
            raise ServiceError("I could not reach that service right now.") from None

        # follow_redirects is on, so confirm where the request actually landed.
        final_host = (response.url.host or "").lower()
        if not (final_host == host or final_host.endswith(f".{host}")):
            log.warning(
                "Request to %s redirected to an unexpected host (%s).", url, final_host
            )
            raise ServiceError(
                "The service redirected somewhere unexpected, so I stopped."
            )

        if response.status_code == 404:
            raise ServiceError("The service had nothing to return for that request.")
        if response.status_code == 429:
            raise ServiceError(
                "The service is rate limiting Fyrion. Please try again in a minute."
            )
        if response.status_code >= 400:
            log.info("Request to %s returned HTTP %s.", url, response.status_code)
            raise ServiceError(
                f"The service replied with an error (HTTP {response.status_code})."
            )

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ServiceError("The service returned an unexpectedly large response.")

        try:
            return response.json()
        except ValueError:
            raise ServiceError(
                "The service returned a response I could not read."
            ) from None

    # ------------------------------------------------------------------
    # /8ball
    # ------------------------------------------------------------------

    @app_commands.command(
        name="8ball", description="Ask the magic 8-ball a yes or no question."
    )
    @app_commands.describe(question="The question you want answered")
    @app_commands.checks.cooldown(4, 20.0, key=lambda interaction: interaction.user.id)
    async def eightball_cmd(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, MAX_QUESTION_LENGTH],
    ) -> None:
        text = sanitize_inline(question, MAX_QUESTION_LENGTH)

        bucket = self._rng.choices(
            (AFFIRMATIVE, NONCOMMITTAL, NEGATIVE), weights=(10, 5, 5), k=1
        )[0]
        answer = self._rng.choice(bucket)

        if bucket is AFFIRMATIVE:
            color = discord.Color.green()
        elif bucket is NEGATIVE:
            color = discord.Color.red()
        else:
            color = discord.Color.blurple()

        embed = discord.Embed(title="\U0001f3b1 Magic 8-Ball", color=color)
        embed.add_field(name="Question", value=text, inline=False)
        embed.add_field(name="Answer", value=f"**{answer}**", inline=False)
        embed.set_footer(text="For entertainment only.")

        await self._send(interaction, embed=embed)

    # ------------------------------------------------------------------
    # /coinflip
    # ------------------------------------------------------------------

    @app_commands.command(name="coinflip", description="Flip one or more coins.")
    @app_commands.describe(count=f"How many coins to flip (1-{MAX_COINS})")
    @app_commands.checks.cooldown(5, 15.0, key=lambda interaction: interaction.user.id)
    async def coinflip_cmd(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, MAX_COINS] = 1,
    ) -> None:
        flips = [self._rng.choice(COIN_FACES) for _ in range(int(count))]
        heads = flips.count(COIN_FACES[0])
        tails = len(flips) - heads

        embed = discord.Embed(title="\U0001fa99 Coin flip", color=discord.Color.gold())

        if len(flips) == 1:
            embed.description = f"It landed on **{flips[0]}**."
        else:
            embed.description = (
                "\u2192 " + ", ".join(f"**{face}**" for face in flips)[:4000]
            )
            embed.add_field(name="Heads", value=str(heads), inline=True)
            embed.add_field(name="Tails", value=str(tails), inline=True)
            if heads == tails:
                embed.set_footer(text="A perfect tie.")
            else:
                leader = COIN_FACES[0] if heads > tails else COIN_FACES[1]
                embed.set_footer(text=f"{leader} came out ahead.")

        await self._send(interaction, embed=embed)

    # ------------------------------------------------------------------
    # /roll
    # ------------------------------------------------------------------

    @app_commands.command(
        name="roll", description="Roll dice using standard notation, for example 2d6+3."
    )
    @app_commands.describe(
        dice="Dice expression such as 1d20, 2d6+3 or 4d8-2",
        private="Show the result only to you",
    )
    @app_commands.checks.cooldown(5, 15.0, key=lambda interaction: interaction.user.id)
    async def roll_cmd(
        self,
        interaction: discord.Interaction,
        dice: app_commands.Range[str, 1, MAX_EXPRESSION_LENGTH] = "1d20",
        private: bool = False,
    ) -> None:
        try:
            result = roll_expression(dice, self._rng)
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return

        embed = discord.Embed(
            title="\U0001f3b2 Dice roll", color=discord.Color.blurple()
        )
        embed.add_field(
            name="Expression",
            value=f"`{sanitize_inline(dice, MAX_EXPRESSION_LENGTH)}`",
            inline=True,
        )
        embed.add_field(name="Total", value=f"**{result.total:,}**", inline=True)
        embed.add_field(
            name="Breakdown",
            value=f"```\n{result.render()[: FIELD_VALUE_LIMIT - 10]}\n```",
            inline=False,
        )
        embed.set_footer(text=f"{result.dice_used} die/dice rolled.")

        await self._send(interaction, embed=embed, ephemeral=private)

    # ------------------------------------------------------------------
    # /quote
    # ------------------------------------------------------------------

    @app_commands.command(
        name="quote", description="Show a random inspirational quote."
    )
    @app_commands.checks.cooldown(3, 30.0, key=lambda interaction: interaction.user.id)
    async def quote_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        text: str | None = None
        author: str | None = None
        source = "zenquotes.io"

        try:
            payload = await self._get_json(QUOTE_API_URL, host=QUOTE_API_HOST)
        except ServiceError as exc:
            log.info("The quote service was unavailable: %s", exc)
        else:
            entry: Any = None
            if isinstance(payload, list) and payload:
                entry = payload[0]
            elif isinstance(payload, dict):
                entry = payload

            if isinstance(entry, dict):
                candidate_text = str(entry.get("q") or "").strip()
                candidate_author = str(entry.get("a") or "").strip()
                # The API answers rate limiting with a normal-looking quote
                # attributed to itself; treat that as a miss.
                if candidate_text and candidate_author.lower() != "zenquotes.io":
                    text = candidate_text
                    author = candidate_author or "Unknown"

        if text is None:
            text, author = self._rng.choice(FALLBACK_QUOTES)
            source = "Fyrion's offline collection"

        embed = discord.Embed(
            title="\U0001f4ac Quote",
            description=f"> {sanitize_inline(text, 900)}",
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="Attributed to", value=sanitize_inline(author, 120), inline=False
        )
        embed.set_footer(text=f"Source: {source}")

        await self._send(interaction, embed=embed)

    # ------------------------------------------------------------------
    # /meme
    # ------------------------------------------------------------------

    @app_commands.command(name="meme", description="Fetch a random meme.")
    @app_commands.describe(
        subreddit="Optional subreddit to pull from, for example memes or ProgrammerHumor"
    )
    @app_commands.checks.cooldown(3, 20.0, key=lambda interaction: interaction.user.id)
    async def meme_cmd(
        self,
        interaction: discord.Interaction,
        subreddit: Optional[app_commands.Range[str, 2, 21]] = None,
    ) -> None:
        if subreddit is not None:
            candidate = subreddit.strip().lstrip("/")
            if candidate.lower().startswith("r/"):
                candidate = candidate[2:]
            if not SUBREDDIT_PATTERN.match(candidate):
                await self._reject(
                    interaction,
                    "That is not a subreddit name. Use letters, digits and "
                    "underscores only, for example `ProgrammerHumor`.",
                )
                return
            url = f"{MEME_API_URL}/{candidate}"
        else:
            url = MEME_API_URL

        await interaction.response.defer()

        allow_nsfw = channel_is_nsfw(interaction)
        payload: dict[str, Any] | None = None
        skipped_nsfw = 0

        for _ in range(MEME_ATTEMPTS):
            try:
                data = await self._get_json(url, host=MEME_API_HOST)
            except ServiceError as exc:
                await self._send(interaction, content=f"\u274c {exc}", ephemeral=True)
                return

            if not isinstance(data, dict):
                await self._send(
                    interaction,
                    content="\u274c The meme service returned something unusable.",
                    ephemeral=True,
                )
                return

            if data.get("code") and int(data.get("code") or 0) >= 400:
                await self._send(
                    interaction,
                    content=(
                        "\u274c That subreddit could not be read. It may be "
                        "private, empty or misspelled."
                    ),
                    ephemeral=True,
                )
                return

            if bool(data.get("nsfw")) and not allow_nsfw:
                skipped_nsfw += 1
                continue

            payload = data
            break

        if payload is None:
            await self._send(
                interaction,
                content=(
                    "\u26a0\ufe0f Every meme I received was flagged NSFW, so none "
                    "were posted. Try again, or use an age-restricted channel."
                ),
                ephemeral=True,
            )
            return

        image_url = safe_url(payload.get("url"), *IMAGE_HOSTS)
        post_url = safe_url(payload.get("postLink"), *REDDIT_LINK_HOSTS)
        spoiler = bool(payload.get("spoiler"))

        embed = discord.Embed(
            title=sanitize_inline(payload.get("title"), 240),
            url=post_url,
            color=discord.Color.orange(),
        )

        if image_url is None:
            embed.description = (
                "\u26a0\ufe0f The image was hosted somewhere I do not embed, so "
                "only the link is shown."
            )
        elif spoiler:
            # A spoiler-flagged post must not be rendered inline.
            embed.description = (
                "\u26a0\ufe0f This post is marked as a spoiler, so the image is "
                f"linked instead of shown: [open it]({image_url})"
            )
        else:
            embed.set_image(url=image_url)

        author = payload.get("author")
        if author:
            embed.add_field(
                name="Posted by", value=sanitize_inline(f"u/{author}", 80), inline=True
            )

        ups = payload.get("ups")
        if isinstance(ups, int):
            embed.add_field(name="Upvotes", value=f"{ups:,}", inline=True)

        footer = (
            f"r/{sanitize_inline(payload.get('subreddit'), 40)} \u2022 via meme-api.com"
        )
        if skipped_nsfw:
            footer += f" \u2022 skipped {skipped_nsfw} NSFW result(s)"
        embed.set_footer(text=footer[:2048])

        await self._send(interaction, embed=embed)

    # ------------------------------------------------------------------
    # /urban
    # ------------------------------------------------------------------

    @app_commands.command(
        name="urban", description="Look a term up on Urban Dictionary."
    )
    @app_commands.describe(term="The word or phrase to define")
    @app_commands.checks.cooldown(3, 20.0, key=lambda interaction: interaction.user.id)
    async def urban_cmd(
        self,
        interaction: discord.Interaction,
        term: app_commands.Range[str, 1, MAX_TERM_LENGTH],
    ) -> None:
        query = " ".join(term.split())
        if not query:
            await self._reject(interaction, "No term was supplied.")
            return

        # Urban Dictionary is entirely user generated and frequently explicit,
        # so definitions are only posted publicly in age-restricted channels.
        ephemeral = not channel_is_nsfw(interaction)
        await interaction.response.defer(ephemeral=ephemeral)

        try:
            payload = await self._get_json(
                URBAN_API_URL, host=URBAN_API_HOST, params={"term": query}
            )
        except ServiceError as exc:
            await self._send(interaction, content=f"\u274c {exc}", ephemeral=True)
            return

        entries = payload.get("list") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            await self._send(
                interaction,
                content=(
                    "\u2139\ufe0f Urban Dictionary has no definition for "
                    f"**{sanitize_inline(query, MAX_TERM_LENGTH)}**."
                ),
                ephemeral=True,
            )
            return

        def score(item: Any) -> int:
            if not isinstance(item, dict):
                return -1
            try:
                return int(item.get("thumbs_up") or 0) - int(
                    item.get("thumbs_down") or 0
                )
            except (TypeError, ValueError):
                return 0

        best = max(
            (item for item in entries if isinstance(item, dict)),
            key=score,
            default=None,
        )
        if best is None:
            await self._send(
                interaction,
                content="\u274c Urban Dictionary returned something unusable.",
                ephemeral=True,
            )
            return

        definition = sanitize_block(strip_urban_brackets(best.get("definition")))
        example = sanitize_block(strip_urban_brackets(best.get("example")), 600, 8)
        permalink = safe_url(best.get("permalink"), URBAN_LINK_HOST)

        embed = discord.Embed(
            title=f"\U0001f4d6 {sanitize_inline(best.get('word') or query, 200)}",
            url=permalink,
            description=definition,
            color=discord.Color.dark_teal(),
        )

        if example and example != "\u2014":
            embed.add_field(name="Example", value=example, inline=False)

        try:
            up = int(best.get("thumbs_up") or 0)
            down = int(best.get("thumbs_down") or 0)
        except (TypeError, ValueError):
            up, down = 0, 0
        embed.add_field(
            name="Votes", value=f"\U0001f44d {up:,} / \U0001f44e {down:,}", inline=True
        )

        author = best.get("author")
        if author:
            embed.add_field(
                name="Author", value=sanitize_inline(author, 80), inline=True
            )

        footer = "Definitions are written by Urban Dictionary users, not by Fyrion."
        if ephemeral:
            footer += " Shown only to you because this channel is not age restricted."
        embed.set_footer(text=footer[:2048])

        await self._send(interaction, embed=embed, ephemeral=ephemeral)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))


__all__ = [
    "Fun",
    "DiceResult",
    "ServiceError",
    "AFFIRMATIVE",
    "NEGATIVE",
    "NONCOMMITTAL",
    "FALLBACK_QUOTES",
    "MAX_COINS",
    "MAX_DICE_PER_ROLL",
    "MAX_DIE_SIDES",
    "channel_is_nsfw",
    "roll_expression",
    "safe_url",
    "sanitize_block",
    "sanitize_inline",
    "strip_urban_brackets",
    "setup",
]
