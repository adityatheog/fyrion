"""
Utility, information and quality-of-life commands.

Everything here is read-only with respect to Discord state: nothing in this cog
changes roles, channels or members, so no hierarchy checks are needed. Two
features do keep state of their own:

* **AFK.** One row per ``(guild_id, user_id)`` in an ``afk_status`` table this
  module owns. The DDL is idempotent and applied on cog load, and the table
  cascades from ``guild_settings`` so removing a guild leaves no orphaned rows.
  The ``on_message`` listener runs for every guild message, so it never touches
  the database on the hot path: the whole table is small and is mirrored in an
  in-memory dictionary that every write keeps in sync.
* **Polls.** Reaction based on purpose. Votes therefore live in Discord itself
  rather than in Fyrion's memory, so a restart cannot lose a poll. Only the
  optional automatic close is scheduled in-process, and that is documented as
  best effort.

Safety properties worth stating explicitly:

* ``/calculator`` parses the expression with :mod:`ast` and walks the tree with
  an explicit allow-list of node types, operators and functions. There is no
  ``eval``, no attribute access, no name binding and no imports. Expression
  length, node count, exponent size and result magnitude are all bounded,
  because Python's arithmetic has no timeout.
* Display names, poll questions, AFK reasons and role names are attacker
  controlled, so every reply disables mention parsing. The only exception is the
  AFK welcome-back notice, which may mention exactly the one member it
  addresses.
* ``/banner`` needs a REST fetch, because banners are not part of the gateway
  user payload; the command defers first so the interaction cannot expire.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import math
import operator
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Final, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import utcnow

from fyrion.config import Config
from fyrion.database.manager import utc_now_iso

log = logging.getLogger("fyrion.cogs.utility")

NO_MENTIONS = discord.AllowedMentions.none()
# The AFK notices address exactly one member, so user mentions are allowed and
# nothing else is: a crafted nickname can never make Fyrion ping a role.
NOTICE_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)

REPOSITORY_URL: Final[str] = "https://github.com/adityatheog/fyrion"
ISSUES_URL: Final[str] = f"{REPOSITORY_URL}/issues"
LICENSE_URL: Final[str] = f"{REPOSITORY_URL}/blob/main/LICENSE"

# Embeds allow 1024 characters per field value.
FIELD_VALUE_LIMIT: Final[int] = 1024
# Listing every role in a large server would overflow the field.
ROLE_DISPLAY_LIMIT: Final[int] = 20
# Guild feature flags are numerous and mostly uninteresting.
FEATURE_DISPLAY_LIMIT: Final[int] = 12

# ---------------------------------------------------------------------------
# AFK
# ---------------------------------------------------------------------------

AFK_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS afk_status (
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        reason   TEXT,
        since    TEXT    NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        mentions INTEGER NOT NULL DEFAULT 0 CHECK (mentions >= 0),
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_afk_status_guild
        ON afk_status (guild_id)
    """,
)

MAX_AFK_REASON: Final[int] = 200
DEFAULT_AFK_REASON: Final[str] = "AFK"
# A member who speaks immediately after running /afk almost always meant to
# stay away, so the status is not cleared during this grace window.
AFK_GRACE_SECONDS: Final[float] = 4.0
AFK_NOTICE_DELETE_AFTER: Final[float] = 12.0
# One mention notice per channel per window, so a spam of pings cannot turn
# into a spam of replies.
AFK_NOTICE_COOLDOWN: Final[float] = 8.0
MAX_AFK_NOTICES: Final[int] = 3

# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

MAX_EXPRESSION_LENGTH: Final[int] = 200
MAX_EXPRESSION_NODES: Final[int] = 120
MAX_EXPONENT: Final[int] = 128
MAX_MAGNITUDE: Final[float] = 1e100
MAX_FACTORIAL: Final[int] = 100
MAX_CALL_ARGUMENTS: Final[int] = 4

# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------

POLL_EMOJI: Final[tuple[str, ...]] = (
    "1\ufe0f\u20e3",
    "2\ufe0f\u20e3",
    "3\ufe0f\u20e3",
    "4\ufe0f\u20e3",
    "5\ufe0f\u20e3",
    "6\ufe0f\u20e3",
    "7\ufe0f\u20e3",
    "8\ufe0f\u20e3",
    "9\ufe0f\u20e3",
    "\U0001f51f",
)
YES_NO_EMOJI: Final[tuple[str, str]] = ("\U0001f44d", "\U0001f44e")
MAX_POLL_OPTIONS: Final[int] = 10
MIN_POLL_OPTIONS: Final[int] = 2
MAX_POLL_QUESTION: Final[int] = 256
MAX_POLL_OPTION: Final[int] = 80
MAX_POLL_MINUTES: Final[int] = 10080
POLL_BAR_SEGMENTS: Final[int] = 12

# Strong references so a scheduled poll close cannot be garbage collected.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = FIELD_VALUE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _fence(text: str) -> str:
    """Wraps user supplied text so markdown and mentions stay inert."""
    safe = _truncate(text, 900).replace("```", "`\u200b``").replace("@", "@\u200b")
    return f"```\n{safe}\n```"


def format_duration(seconds: float) -> str:
    """Renders a duration as ``3d 4h 12m 5s``, omitting empty units."""
    total = int(max(0.0, seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def human_bytes(value: Any) -> str:
    """Renders a byte count using binary units."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "unknown"

    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GiB"


def format_latency(value: float | None) -> str:
    """Renders a latency in milliseconds, tolerating an unmeasured value."""
    if value is None:
        return "unknown"
    # discord.py reports nan until the first heartbeat ack arrives.
    if value != value:
        return "unknown"
    return f"{value:.0f}ms"


def progress_bar(fraction: float, segments: int = POLL_BAR_SEGMENTS) -> str:
    filled = int(round(min(1.0, max(0.0, fraction)) * segments))
    return "\u2588" * filled + "\u2591" * (segments - filled)


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


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


class CalculatorError(ValueError):
    """Raised when an expression is unusable. The message is user facing."""


def _guarded_factorial(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CalculatorError("factorial() needs a whole number.") from None
    if number != value:
        raise CalculatorError("factorial() needs a whole number.")
    if number < 0:
        raise CalculatorError("factorial() needs a number that is not negative.")
    if number > MAX_FACTORIAL:
        raise CalculatorError(f"factorial() is limited to {MAX_FACTORIAL}.")
    return math.factorial(number)


def _guarded_log(value: float, base: float | None = None) -> float:
    if value <= 0:
        raise CalculatorError("log() needs a positive number.")
    if base is None:
        return math.log(value)
    if base <= 0 or base == 1:
        raise CalculatorError("log() needs a positive base other than 1.")
    return math.log(value, base)


def _guarded_sqrt(value: float) -> float:
    if value < 0:
        raise CalculatorError("sqrt() needs a number that is not negative.")
    return math.sqrt(value)


_BINARY_OPS: Final[dict[type[ast.operator], Callable[[Any, Any], Any]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: Final[dict[type[ast.unaryop], Callable[[Any], Any]]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS: Final[dict[str, Callable[..., Any]]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": _guarded_sqrt,
    "log": _guarded_log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "degrees": math.degrees,
    "radians": math.radians,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "hypot": math.hypot,
    "gcd": math.gcd,
    "factorial": _guarded_factorial,
}

_CONSTANTS: Final[dict[str, float]] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _check_magnitude(value: Any) -> Any:
    """Refuses results that are infinite or absurdly large."""
    if isinstance(value, bool):
        raise CalculatorError("Only numbers are supported.")
    if isinstance(value, int):
        if abs(value) > 10**60:
            raise CalculatorError("That result is too large to display.")
        return value
    if isinstance(value, float):
        if math.isnan(value):
            raise CalculatorError("That expression is not a number.")
        if math.isinf(value) or abs(value) > MAX_MAGNITUDE:
            raise CalculatorError("That result is too large to display.")
        return value
    raise CalculatorError("Only numbers are supported.")


def _eval_node(node: ast.AST) -> Any:
    """Evaluates one allow-listed AST node."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculatorError("Only numbers are supported.")
        return node.value

    if isinstance(node, ast.BinOp):
        handler = _BINARY_OPS.get(type(node.op))
        if handler is None:
            raise CalculatorError("Only + - * / // % and ** are supported operators.")

        left = _eval_node(node.left)
        right = _eval_node(node.right)

        if isinstance(node.op, ast.Pow):
            # Python has no arithmetic timeout, so an oversized exponent is
            # refused before it is ever computed.
            if abs(right) > MAX_EXPONENT:
                raise CalculatorError(f"Exponents are limited to {MAX_EXPONENT}.")
            if abs(left) > 1e6 and abs(right) > 16:
                raise CalculatorError("That power would be too large to compute.")

        try:
            result = handler(left, right)
        except ZeroDivisionError:
            raise CalculatorError("Division by zero.") from None
        except OverflowError:
            raise CalculatorError("That result is too large to compute.") from None
        except (TypeError, ValueError) as exc:
            raise CalculatorError(
                f"That expression could not be evaluated ({exc})."
            ) from None

        return _check_magnitude(result)

    if isinstance(node, ast.UnaryOp):
        unary_handler = _UNARY_OPS.get(type(node.op))
        if unary_handler is None:
            raise CalculatorError("Only unary + and - are supported.")
        return _check_magnitude(unary_handler(_eval_node(node.operand)))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalculatorError("Only plain function calls are supported.")
        if node.keywords:
            raise CalculatorError("Keyword arguments are not supported.")

        function = _FUNCTIONS.get(node.func.id)
        if function is None:
            raise CalculatorError(
                f"`{node.func.id}` is not a supported function. Available: "
                + ", ".join(sorted(_FUNCTIONS))
            )
        if len(node.args) > MAX_CALL_ARGUMENTS:
            raise CalculatorError(
                f"Functions accept at most {MAX_CALL_ARGUMENTS} arguments."
            )

        arguments = [_eval_node(argument) for argument in node.args]
        try:
            result = function(*arguments)
        except CalculatorError:
            raise
        except ZeroDivisionError:
            raise CalculatorError("Division by zero.") from None
        except OverflowError:
            raise CalculatorError("That result is too large to compute.") from None
        except (TypeError, ValueError) as exc:
            raise CalculatorError(
                f"`{node.func.id}()` could not be evaluated ({exc})."
            ) from None

        return _check_magnitude(result)

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculatorError(
            f"`{node.id}` is not a known value. Available constants: "
            + ", ".join(sorted(_CONSTANTS))
        )

    raise CalculatorError(
        "Only numbers, the operators + - * / // % **, parentheses and a small "
        "set of maths functions are supported."
    )


def evaluate(expression: str) -> Any:
    """Safely evaluates an arithmetic expression.

    The expression is parsed with :mod:`ast` and walked with an explicit
    allow-list, so there is no code execution path: no attribute access, no
    subscripting, no assignments, no comprehensions and no imports.

    Raises:
        CalculatorError: with a user-facing message when the input is unusable.
    """
    text = (expression or "").strip()
    if not text:
        raise CalculatorError("No expression was supplied.")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise CalculatorError(
            f"The expression must be at most {MAX_EXPRESSION_LENGTH} characters."
        )

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"That is not a valid expression ({exc.msg}).") from None
    except (ValueError, MemoryError, RecursionError):
        raise CalculatorError("That expression could not be parsed.") from None

    if len(list(ast.walk(tree))) > MAX_EXPRESSION_NODES:
        raise CalculatorError("That expression is too complex.")

    return _eval_node(tree.body)


def format_number(value: Any) -> str:
    """Renders a calculator result for display."""
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:.10g}"
    return str(value)


# ---------------------------------------------------------------------------
# AFK state
# ---------------------------------------------------------------------------


@dataclass
class AfkEntry:
    """One member's AFK status, mirrored in memory for the message hot path."""

    reason: str
    since: str
    mentions: int = 0
    set_at: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Utility(commands.Cog):
    """Information, diagnostics and quality-of-life commands."""

    # Applying the AFK DDL once per process is enough: it is idempotent, and
    # every pooled connection sees the same database file.
    _schema_ready: ClassVar[bool] = False

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = getattr(bot, "db", None)
        # Fallback for a bot that does not expose ``boot_time`` (for example in
        # tests).
        self._loaded_at = utcnow()

        self._afk: dict[tuple[int, int], AfkEntry] = {}
        self._notice_cooldown: dict[int, float] = {}

    @property
    def boot_time(self) -> Any:
        return getattr(self.bot, "boot_time", self._loaded_at)

    async def cog_load(self) -> None:
        await self._ensure_afk_schema()
        await self._load_afk_cache()

    # ------------------------------------------------------------------
    # AFK storage
    # ------------------------------------------------------------------

    async def _ensure_afk_schema(self) -> None:
        if Utility._schema_ready or self.db is None:
            return
        try:
            for statement in AFK_STATEMENTS:
                await self.db.execute(statement)
        except Exception:
            # AFK is optional; the rest of the cog must still work.
            log.exception("Could not prepare the AFK table.")
            return
        Utility._schema_ready = True

    async def _load_afk_cache(self) -> None:
        """Loads every AFK row into memory.

        The table holds at most one small row per away member, so keeping it in
        memory means ``on_message`` never issues a query.
        """
        if self.db is None or not Utility._schema_ready:
            return

        try:
            rows = await self.db.fetchall(
                "SELECT guild_id, user_id, reason, since, mentions FROM afk_status"
            )
        except Exception:
            log.exception("Could not load the AFK cache.")
            return

        cache: dict[tuple[int, int], AfkEntry] = {}
        for row in rows:
            try:
                key = (int(row["guild_id"]), int(row["user_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            cache[key] = AfkEntry(
                reason=str(row["reason"] or DEFAULT_AFK_REASON),
                since=str(row["since"] or utc_now_iso()),
                mentions=int(row["mentions"] or 0),
                # Rows restored from disk are not covered by the grace window.
                set_at=0.0,
            )

        self._afk = cache
        if cache:
            log.info("Restored %d AFK status entr(y/ies).", len(cache))

    async def _store_afk(self, guild_id: int, user_id: int, reason: str) -> None:
        entry = AfkEntry(reason=reason, since=utc_now_iso())
        self._afk[(guild_id, user_id)] = entry

        if self.db is None or not Utility._schema_ready:
            return
        try:
            await self.db.ensure_guild(guild_id)
            await self.db.execute(
                "INSERT INTO afk_status (guild_id, user_id, reason, since, mentions) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "    reason = excluded.reason, "
                "    since = excluded.since, "
                "    mentions = 0",
                (int(guild_id), int(user_id), reason, entry.since),
            )
        except Exception:
            log.exception(
                "Could not store the AFK status for member %s in guild %s.",
                user_id,
                guild_id,
            )

    async def _clear_afk(self, guild_id: int, user_id: int) -> AfkEntry | None:
        entry = self._afk.pop((guild_id, user_id), None)
        if entry is None:
            return None

        if self.db is not None and Utility._schema_ready:
            try:
                await self.db.execute(
                    "DELETE FROM afk_status WHERE guild_id = ? AND user_id = ?",
                    (int(guild_id), int(user_id)),
                )
            except Exception:
                log.exception(
                    "Could not clear the AFK status for member %s in guild %s.",
                    user_id,
                    guild_id,
                )
        return entry

    async def _bump_mentions(self, guild_id: int, user_id: int) -> None:
        entry = self._afk.get((guild_id, user_id))
        if entry is None:
            return
        entry.mentions += 1

        if self.db is None or not Utility._schema_ready:
            return
        try:
            await self.db.execute(
                "UPDATE afk_status SET mentions = mentions + 1 "
                "WHERE guild_id = ? AND user_id = ?",
                (int(guild_id), int(user_id)),
            )
        except Exception:
            log.exception("Could not record an AFK mention for member %s.", user_id)

    # ------------------------------------------------------------------
    # AFK listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        author = message.author

        if guild is None or message.webhook_id is not None:
            return
        if not isinstance(author, discord.Member) or author.bot:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return
        if not self._afk:
            # Fast path: nobody in the process is AFK.
            return

        await self._handle_return(message, guild, author)
        await self._handle_mentions(message, guild, author)

    async def _handle_return(
        self,
        message: discord.Message,
        guild: discord.Guild,
        author: discord.Member,
    ) -> None:
        entry = self._afk.get((guild.id, author.id))
        if entry is None:
            return
        if time.monotonic() - entry.set_at < AFK_GRACE_SECONDS:
            # They almost certainly just ran /afk; do not undo it immediately.
            return

        cleared = await self._clear_afk(guild.id, author.id)
        if cleared is None:
            return

        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        me = guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return

        since = parse_iso(cleared.since)
        away_for = (
            format_duration((datetime.now(timezone.utc) - since).total_seconds())
            if since is not None
            else None
        )

        text = f"\U0001f44b Welcome back {author.mention}, I removed your AFK status."
        if away_for:
            text += f" You were away for **{away_for}**."
        if cleared.mentions:
            text += (
                f" You were mentioned **{cleared.mentions}** "
                f"time{'s' if cleared.mentions != 1 else ''} while away."
            )

        try:
            await channel.send(
                text,
                delete_after=AFK_NOTICE_DELETE_AFTER,
                allowed_mentions=NOTICE_MENTIONS,
            )
        except discord.HTTPException:
            # The notice is cosmetic; the status is already cleared.
            pass

    async def _handle_mentions(
        self,
        message: discord.Message,
        guild: discord.Guild,
        author: discord.Member,
    ) -> None:
        if not message.mentions:
            return

        notices: list[str] = []
        for mentioned in message.mentions:
            if mentioned.id == author.id or getattr(mentioned, "bot", False):
                continue
            entry = self._afk.get((guild.id, mentioned.id))
            if entry is None:
                continue

            await self._bump_mentions(guild.id, mentioned.id)

            if len(notices) >= MAX_AFK_NOTICES:
                continue

            since = parse_iso(entry.since)
            when = (
                discord.utils.format_dt(since, style="R") if since is not None else ""
            )
            # The reason is user supplied, so it is fenced and mentions are
            # disabled on the reply below.
            reason = entry.reason.replace("`", "\u02cb").replace("@", "@\u200b")
            display = getattr(mentioned, "display_name", str(mentioned))
            notices.append(
                f"\U0001f4a4 **{display}** is AFK{f' (since {when})' if when else ''}: "
                f"{_truncate(reason, 150)}"
            )

        if not notices:
            return

        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        me = guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return

        now = time.monotonic()
        last = self._notice_cooldown.get(channel.id)
        if last is not None and now - last < AFK_NOTICE_COOLDOWN:
            return
        self._notice_cooldown[channel.id] = now

        try:
            await channel.send(
                "\n".join(notices),
                delete_after=AFK_NOTICE_DELETE_AFTER,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # /afk
    # ------------------------------------------------------------------

    @app_commands.command(
        name="afk",
        description="Mark yourself as AFK so mentions get an automatic reply.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        reason="Why you are away (shown to anyone who mentions you)",
        clear="Set to True to remove your AFK status right away",
    )
    async def afk_cmd(
        self,
        interaction: discord.Interaction,
        reason: Optional[app_commands.Range[str, 1, MAX_AFK_REASON]] = None,
        clear: bool = False,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "\u274c This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        if self.db is None or not Utility._schema_ready:
            await interaction.response.send_message(
                "\u274c The AFK system is unavailable because its storage could "
                "not be prepared.",
                ephemeral=True,
            )
            return

        if clear:
            cleared = await self._clear_afk(guild.id, member.id)
            if cleared is None:
                await interaction.response.send_message(
                    "\u2139\ufe0f You were not marked as AFK.", ephemeral=True
                )
                return

            note = (
                f" You were mentioned {cleared.mentions} "
                f"time{'s' if cleared.mentions != 1 else ''} while away."
                if cleared.mentions
                else ""
            )
            await interaction.response.send_message(
                f"\u2705 Your AFK status has been removed.{note}", ephemeral=True
            )
            return

        text = (reason or DEFAULT_AFK_REASON).strip()[:MAX_AFK_REASON]
        existing = self._afk.get((guild.id, member.id))
        await self._store_afk(guild.id, member.id, text)

        verb = "updated" if existing is not None else "set"
        await interaction.response.send_message(
            f"\U0001f4a4 AFK {verb}: {_fence(text)}"
            "Anyone who mentions you will be told. Your status clears itself as "
            "soon as you send a message here.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    # ------------------------------------------------------------------
    # /ping
    # ------------------------------------------------------------------

    async def _database_latency(self) -> float | None:
        """Returns the SQLite round-trip time in milliseconds, or None."""
        if self.db is None:
            return None

        start = time.perf_counter()
        try:
            value = await self.db.fetchval("SELECT 1")
        except Exception:
            log.exception("The database latency probe failed.")
            return None

        if value != 1:
            return None
        return (time.perf_counter() - start) * 1000.0

    @app_commands.command(
        name="ping",
        description="Check gateway, shard, database and interaction latency.",
    )
    async def ping_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        gateway = self.bot.latency * 1000.0
        database = await self._database_latency()
        round_trip = (utcnow() - interaction.created_at).total_seconds() * 1000.0

        embed = discord.Embed(
            title="\U0001f3d3 Pong",
            color=(
                discord.Color.green()
                if database is not None
                else discord.Color.orange()
            ),
        )
        embed.add_field(name="Gateway", value=format_latency(gateway), inline=True)
        embed.add_field(
            name="Interaction", value=format_latency(round_trip), inline=True
        )
        embed.add_field(
            name="Database",
            value=format_latency(database) if database is not None else "unreachable",
            inline=True,
        )

        shard_id = interaction.guild.shard_id if interaction.guild is not None else 0
        # get_shard exists only on AutoShardedBot; a non-sharded bot has no
        # per-shard latency to report, so the field is simply omitted there.
        shard = (
            self.bot.get_shard(shard_id)
            if isinstance(self.bot, commands.AutoShardedBot)
            else None
        )
        if shard is not None:
            embed.add_field(
                name=f"Shard {shard_id}",
                value=format_latency(shard.latency * 1000.0),
                inline=True,
            )

        latencies = list(getattr(self.bot, "latencies", ()))
        if len(latencies) > 1:
            rows = [
                f"`{index}`: {format_latency(value * 1000.0)}"
                for index, value in latencies[:10]
            ]
            if len(latencies) > 10:
                rows.append(f"\u2026 and {len(latencies) - 10} more")
            embed.add_field(
                name=f"All shards ({len(latencies)})",
                value=_truncate(" \u2022 ".join(rows)),
                inline=False,
            )

        if database is None:
            embed.set_footer(
                text="The database did not answer; storage-backed commands may fail."
            )

        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # /uptime and /botinfo
    # ------------------------------------------------------------------

    @app_commands.command(
        name="uptime", description="Check how long Fyrion has been online."
    )
    async def uptime_cmd(self, interaction: discord.Interaction) -> None:
        boot = self.boot_time
        elapsed = (utcnow() - boot).total_seconds()

        embed = discord.Embed(
            title="\u23f1\ufe0f Uptime",
            description=f"Online for **{format_duration(elapsed)}**.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Started",
            value=(
                f"{discord.utils.format_dt(boot, style='F')} "
                f"({discord.utils.format_dt(boot, style='R')})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Gateway",
            value=format_latency(self.bot.latency * 1000.0),
            inline=True,
        )
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(
        name="botinfo", description="View Fyrion's version, host and source code."
    )
    async def botinfo_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="Fyrion",
            url=REPOSITORY_URL,
            description=(
                "Powerful tools for better Discord communities.\n"
                f"Open source and self-hostable: {REPOSITORY_URL}"
            ),
            color=discord.Color.gold(),
        )
        if self.bot.user is not None:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        embed.add_field(name="Version", value=Config.VERSION, inline=True)
        embed.add_field(name="Environment", value=Config.ENVIRONMENT, inline=True)
        embed.add_field(
            name="Latency",
            value=format_latency(self.bot.latency * 1000.0),
            inline=True,
        )

        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(
            name="Shards", value=str(self.bot.shard_count or 1), inline=True
        )
        embed.add_field(name="Modules", value=str(len(self.bot.cogs)), inline=True)

        embed.add_field(name="Python", value=platform.python_version(), inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Platform", value=sys.platform, inline=True)

        embed.add_field(
            name="Online since",
            value=discord.utils.format_dt(self.boot_time, style="R"),
            inline=False,
        )

        commands_total = len(self.bot.tree.get_commands())
        embed.add_field(name="Slash commands", value=str(commands_total), inline=True)

        if self.db is not None:
            try:
                stats = await self.db.stats()
            except Exception:
                log.exception("Could not read the database statistics.")
                stats = {}
            if stats:
                # Filesystem paths are deliberately not exposed.
                embed.add_field(
                    name="Database",
                    value=(
                        f"schemav{stats.get('schema_version', '?')} \u2022 "
                        f"pool {stats.get('pool_size', '?')} "
                        f"({stats.get('available_connections', '?')} idle)\n"
                        f"journal {stats.get('journal_mode', 'unknown')} \u2022 "
                        f"{human_bytes(stats.get('size_bytes'))}"
                    ),
                    inline=False,
                )

        embed.add_field(
            name="Links",
            value=(
                f"[Source code]({REPOSITORY_URL}) \u2022 "
                f"[Report an issue]({ISSUES_URL}) \u2022 "
                f"[License]({LICENSE_URL})"
            ),
            inline=False,
        )
        embed.set_footer(text="MIT licensed \u2022 built with discord.py")

        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # /userinfo
    # ------------------------------------------------------------------

    @app_commands.command(
        name="userinfo", description="View information about a member."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to inspect (defaults to you)")
    async def userinfo_cmd(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        guild = interaction.guild
        target = member or interaction.user
        if guild is None or not isinstance(target, discord.Member):
            await interaction.response.send_message(
                "\u274c This command only works inside a server.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"User info: {target}",
            color=target.color if target.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
        embed.add_field(name="Bot", value="yes" if target.bot else "no", inline=True)
        embed.add_field(name="Top role", value=target.top_role.mention, inline=True)

        embed.add_field(
            name="Account created",
            value=(
                f"{discord.utils.format_dt(target.created_at, style='F')}\n"
                f"({discord.utils.format_dt(target.created_at, style='R')})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Joined server",
            value=(
                f"{discord.utils.format_dt(target.joined_at, style='F')}\n"
                f"({discord.utils.format_dt(target.joined_at, style='R')})"
                if target.joined_at is not None
                else "unknown"
            ),
            inline=False,
        )

        roles = [
            role.mention for role in reversed(target.roles) if not role.is_default()
        ]
        if roles:
            shown = roles[:ROLE_DISPLAY_LIMIT]
            extra = len(roles) - len(shown)
            value = ", ".join(shown)
            if extra > 0:
                value += f" (+{extra} more)"
            embed.add_field(
                name=f"Roles ({len(roles)})", value=_truncate(value), inline=False
            )

        if target.premium_since is not None:
            embed.add_field(
                name="Boosting since",
                value=discord.utils.format_dt(target.premium_since, style="R"),
                inline=True,
            )

        if target.timed_out_until is not None:
            embed.add_field(
                name="Timed out until",
                value=discord.utils.format_dt(target.timed_out_until, style="F"),
                inline=True,
            )

        if target.id == guild.owner_id:
            embed.add_field(name="Server owner", value="yes", inline=True)

        # Key permissions only: the full list would fill the embed.
        permissions = target.guild_permissions
        notable = [
            name
            for name in (
                "administrator",
                "manage_guild",
                "manage_roles",
                "manage_channels",
                "manage_messages",
                "ban_members",
                "kick_members",
                "moderate_members",
                "mention_everyone",
            )
            if getattr(permissions, name)
        ]
        embed.add_field(
            name="Key permissions",
            value=", ".join(f"`{name}`" for name in notable) if notable else "none",
            inline=False,
        )

        entry = self._afk.get((guild.id, target.id))
        if entry is not None:
            since = parse_iso(entry.since)
            when = (
                f" (since {discord.utils.format_dt(since, style='R')})"
                if since is not None
                else ""
            )
            embed.add_field(
                name="AFK",
                value=_truncate(
                    entry.reason.replace("@", "@\u200b") + when, FIELD_VALUE_LIMIT
                ),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # /serverinfo
    # ------------------------------------------------------------------

    @app_commands.command(
        name="serverinfo", description="View information about this server."
    )
    @app_commands.guild_only()
    async def serverinfo_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "\u274c This command only works inside a server.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Server info: {guild.name}",
            description=guild.description or None,
            color=discord.Color.blurple(),
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner is not None:
            embed.set_image(url=guild.banner.url)

        embed.add_field(
            name="Owner",
            value=(
                guild.owner.mention
                if guild.owner is not None
                else f"`{guild.owner_id}`"
            ),
            inline=True,
        )
        embed.add_field(name="ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(
            name="Members",
            value=(
                f"{guild.member_count:,}"
                if guild.member_count is not None
                else "unknown"
            ),
            inline=True,
        )

        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(
            name="Text channels", value=str(len(guild.text_channels)), inline=True
        )
        embed.add_field(
            name="Voice channels", value=str(len(guild.voice_channels)), inline=True
        )

        embed.add_field(
            name="Categories", value=str(len(guild.categories)), inline=True
        )
        embed.add_field(name="Threads", value=str(len(guild.threads)), inline=True)
        embed.add_field(
            name="Emoji / stickers",
            value=f"{len(guild.emojis)} / {len(guild.stickers)}",
            inline=True,
        )

        embed.add_field(
            name="Boosts",
            value=f"{guild.premium_subscription_count or 0} (tier {guild.premium_tier})",
            inline=True,
        )
        embed.add_field(
            name="Verification", value=str(guild.verification_level), inline=True
        )
        embed.add_field(name="Shard", value=str(guild.shard_id), inline=True)

        embed.add_field(
            name="Created",
            value=(
                f"{discord.utils.format_dt(guild.created_at, style='F')}\n"
                f"({discord.utils.format_dt(guild.created_at, style='R')})"
            ),
            inline=False,
        )

        if guild.features:
            shown = sorted(guild.features)[:FEATURE_DISPLAY_LIMIT]
            value = ", ".join(f"`{feature.lower()}`" for feature in shown)
            extra = len(guild.features) - len(shown)
            if extra > 0:
                value += f" (+{extra} more)"
            embed.add_field(
                name=f"Features ({len(guild.features)})",
                value=_truncate(value),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # /avatar and /banner
    # ------------------------------------------------------------------

    @app_commands.command(name="avatar", description="View a user's avatar.")
    @app_commands.describe(user="The user to view (defaults to you)")
    async def avatar_cmd(
        self, interaction: discord.Interaction, user: Optional[discord.User] = None
    ) -> None:
        target: discord.abc.User = user or interaction.user

        embed = discord.Embed(title=f"Avatar: {target}", color=discord.Color.blurple())
        embed.set_image(url=target.display_avatar.url)

        links = [f"[Global avatar]({target.display_avatar.url})"]
        # Members can carry a separate per-server avatar.
        member = (
            interaction.guild.get_member(target.id)
            if interaction.guild is not None
            else None
        )
        if member is not None and member.guild_avatar is not None:
            links.append(f"[Server avatar]({member.guild_avatar.url})")

        embed.description = " \u2022 ".join(links)
        embed.set_footer(text=f"ID: {target.id}")

        await interaction.response.send_message(
            embed=embed, allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(name="banner", description="View a user's profile banner.")
    @app_commands.describe(user="The user to view (defaults to you)")
    async def banner_cmd(
        self, interaction: discord.Interaction, user: Optional[discord.User] = None
    ) -> None:
        target: discord.abc.User = user or interaction.user

        # Banners are not part of the gateway user payload, so a REST fetch is
        # required; defer first so the interaction cannot expire.
        await interaction.response.defer()

        try:
            fetched = await self.bot.fetch_user(target.id)
        except discord.NotFound:
            await interaction.followup.send(
                "\u274c That user no longer exists on Discord.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not fetch the banner for user %s: %s", target.id, exc)
            await interaction.followup.send(
                f"\u274c Discord rejected the request (HTTP {exc.status}).",
                ephemeral=True,
            )
            return

        member = (
            interaction.guild.get_member(target.id)
            if interaction.guild is not None
            else None
        )
        guild_banner = getattr(member, "guild_banner", None) if member else None

        if fetched.banner is None and guild_banner is None:
            note = (
                ""
                if fetched.accent_color is None
                else f"\nTheir profile accent color is `{fetched.accent_color}`."
            )
            await interaction.followup.send(
                f"\u2139\ufe0f **{fetched}** does not have a profile banner.{note}",
                allowed_mentions=NO_MENTIONS,
            )
            return

        primary = guild_banner or fetched.banner
        assert primary is not None  # one of them exists

        embed = discord.Embed(
            title=f"Banner: {fetched}",
            color=fetched.accent_color or discord.Color.blurple(),
        )
        embed.set_image(url=primary.url)

        links: list[str] = []
        if fetched.banner is not None:
            links.append(f"[Global banner]({fetched.banner.url})")
        if guild_banner is not None:
            links.append(f"[Server banner]({guild_banner.url})")
        embed.description = " \u2022 ".join(links)
        embed.set_footer(text=f"ID: {fetched.id}")

        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------------
    # /roleinfo
    # ------------------------------------------------------------------

    @app_commands.command(name="roleinfo", description="View information about a role.")
    @app_commands.guild_only()
    @app_commands.describe(role="The role to inspect")
    async def roleinfo_cmd(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        guild = interaction.guild
        if guild is None or role.guild.id != guild.id:
            await interaction.response.send_message(
                "\u274c That role does not belong to this server.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Role info: {role.name}",
            color=role.color if role.color.value else discord.Color.blurple(),
        )
        if role.display_icon is not None and isinstance(
            role.display_icon, discord.Asset
        ):
            embed.set_thumbnail(url=role.display_icon.url)

        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Members", value=str(len(role.members)), inline=True)
        embed.add_field(
            name="Position",
            value=f"{role.position} of {len(guild.roles) - 1}",
            inline=True,
        )

        embed.add_field(
            name="Mentionable", value="yes" if role.mentionable else "no", inline=True
        )
        embed.add_field(
            name="Hoisted", value="yes" if role.hoist else "no", inline=True
        )
        embed.add_field(
            name="Managed", value="yes" if role.managed else "no", inline=True
        )

        embed.add_field(
            name="Color",
            value=str(role.color) if role.color.value else "default",
            inline=True,
        )
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(role.created_at, style="F"),
            inline=False,
        )

        granted = [name for name, value in role.permissions if value]
        if role.permissions.administrator:
            embed.add_field(
                name="Permissions",
                value="`administrator` \u2014 this bypasses every channel permission.",
                inline=False,
            )
        elif granted:
            embed.add_field(
                name=f"Permissions ({len(granted)})",
                value=_truncate(", ".join(f"`{name}`" for name in sorted(granted))),
                inline=False,
            )
        else:
            embed.add_field(name="Permissions", value="none", inline=False)

        me = guild.me
        if me is not None:
            assignable = (
                me.guild_permissions.manage_roles
                and not role.managed
                and not role.is_default()
                and me.top_role > role
            )
            embed.set_footer(
                text=(
                    "I can assign this role."
                    if assignable
                    else "I cannot assign this role (hierarchy, managed role, or "
                    "missing Manage Roles)."
                )
            )

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # /calculator
    # ------------------------------------------------------------------

    @app_commands.command(
        name="calculator", description="Evaluate a maths expression safely."
    )
    @app_commands.describe(
        expression="For example 2 + 3 * 4, sqrt(144), or log(8, 2)",
        private="Show the result only to you (default True)",
    )
    async def calculator_cmd(
        self,
        interaction: discord.Interaction,
        expression: app_commands.Range[str, 1, MAX_EXPRESSION_LENGTH],
        private: bool = True,
    ) -> None:
        try:
            result = evaluate(expression)
        except CalculatorError as exc:
            await interaction.response.send_message(
                f"\u274c {exc}", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return

        embed = discord.Embed(title="Calculator", color=discord.Color.blurple())
        embed.add_field(
            name="Expression", value=_fence(expression.strip()), inline=False
        )
        embed.add_field(
            name="Result", value=f"```\n{format_number(result)}\n```", inline=False
        )
        embed.set_footer(
            text=(
                "Supports + - * / // % ** with pi, e, tau and functions such as "
                "sqrt, log, sin and factorial."
            )
        )

        await interaction.response.send_message(
            embed=embed, ephemeral=private, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # /poll
    # ------------------------------------------------------------------

    @app_commands.command(
        name="poll", description="Create a reaction poll with up to 10 options."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        question="What you are asking",
        options=("Up to 10 options, comma separated. Leave empty for a yes/no poll."),
        channel="Where to post the poll (defaults to this channel)",
        minutes="Automatically post the results after this many minutes",
        mention_everyone="Ping @everyone (needs Mention Everyone in that channel)",
    )
    async def poll_cmd(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, MAX_POLL_QUESTION],
        options: Optional[app_commands.Range[str, 1, 900]] = None,
        channel: Optional[discord.TextChannel] = None,
        minutes: Optional[app_commands.Range[int, 1, MAX_POLL_MINUTES]] = None,
        mention_everyone: bool = False,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "\u274c This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        target = channel if channel is not None else interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "\u274c Polls can only be posted to text channels and threads.",
                ephemeral=True,
            )
            return
        if getattr(target, "guild", None) is None or target.guild.id != guild.id:
            await interaction.response.send_message(
                "\u274c That channel does not belong to this server.", ephemeral=True
            )
            return

        # The invoker must be able to post there themselves; Fyrion's own
        # permission is not a substitute for theirs.
        invoker_permissions = target.permissions_for(member)
        if not (invoker_permissions.view_channel and invoker_permissions.send_messages):
            await interaction.response.send_message(
                f"\u274c You cannot post in {target.mention}.", ephemeral=True
            )
            return

        me = guild.me
        if me is None:
            await interaction.response.send_message(
                "\u274c I could not resolve my own membership in this server.",
                ephemeral=True,
            )
            return

        permissions = target.permissions_for(me)
        missing = [
            name
            for name, granted in (
                ("View Channel", permissions.view_channel),
                ("Send Messages", permissions.send_messages),
                ("Embed Links", permissions.embed_links),
                ("Add Reactions", permissions.add_reactions),
                ("Read Message History", permissions.read_message_history),
            )
            if not granted
        ]
        if missing:
            names = ", ".join(f"`{name}`" for name in missing)
            await interaction.response.send_message(
                f"\u274c I am missing {names} in {target.mention}.", ephemeral=True
            )
            return

        if options is None or not options.strip():
            choices: list[str] = ["Yes", "No"]
            emoji: Sequence[str] = YES_NO_EMOJI
        else:
            parsed = [part.strip() for part in options.split(",") if part.strip()]
            if len(parsed) < MIN_POLL_OPTIONS:
                await interaction.response.send_message(
                    f"\u274c A poll needs at least {MIN_POLL_OPTIONS} options. "
                    "Separate them with commas, or leave the field empty for a "
                    "yes/no poll.",
                    ephemeral=True,
                )
                return
            if len(parsed) > MAX_POLL_OPTIONS:
                await interaction.response.send_message(
                    f"\u274c A poll supports at most {MAX_POLL_OPTIONS} options; "
                    f"you supplied {len(parsed)}.",
                    ephemeral=True,
                )
                return

            seen: set[str] = set()
            choices = []
            for entry in parsed:
                trimmed = entry[:MAX_POLL_OPTION]
                if trimmed.lower() in seen:
                    await interaction.response.send_message(
                        f"\u274c The option `{trimmed[:40]}` is listed more than once.",
                        ephemeral=True,
                        allowed_mentions=NO_MENTIONS,
                    )
                    return
                seen.add(trimmed.lower())
                choices.append(trimmed)
            emoji = POLL_EMOJI[: len(choices)]

        ping = (
            mention_everyone
            and invoker_permissions.mention_everyone
            and (permissions.mention_everyone)
        )
        if mention_everyone and not ping:
            await interaction.response.send_message(
                "\u274c Pinging `@everyone` needs the `Mention Everyone` "
                f"permission for both you and me in {target.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        closes_at: datetime | None = None
        description_lines = [f"{icon} {choice}" for icon, choice in zip(emoji, choices)]
        if minutes is not None:
            closes_at = datetime.fromtimestamp(
                utcnow().timestamp() + minutes * 60, tz=timezone.utc
            )
            description_lines.append(
                f"\nCloses {discord.utils.format_dt(closes_at, style='R')}."
            )

        embed = discord.Embed(
            title=f"\U0001f4ca {question.strip()}"[:256],
            description="\n".join(description_lines)[:4000],
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        embed.set_author(
            name=f"Poll by {member.display_name}",
            icon_url=member.display_avatar.url,
        )
        embed.set_footer(text="React below to vote \u2022 one reaction per option")

        content = "@everyone" if ping else None
        allowed = (
            discord.AllowedMentions(
                everyone=True, roles=False, users=False, replied_user=False
            )
            if ping
            else NO_MENTIONS
        )

        try:
            message = await target.send(
                content=content, embed=embed, allowed_mentions=allowed
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"\u274c Discord refused to post the poll in {target.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not post a poll in guild %s: %s", guild.id, exc)
            await interaction.followup.send(
                f"\u274c Discord rejected the poll (HTTP {exc.status}).",
                ephemeral=True,
            )
            return

        added = 0
        for icon in emoji:
            try:
                await message.add_reaction(icon)
            except discord.HTTPException as exc:
                log.warning("Could not add a poll reaction: %s", exc)
                break
            added += 1

        lines = [f"\u2705 Poll posted in {target.mention}: {message.jump_url}"]
        if added < len(emoji):
            lines.append(
                f"\u26a0\ufe0f Only {added} of {len(emoji)} vote reactions could be "
                "added; members can still add the rest themselves."
            )
        if closes_at is not None and minutes is not None:
            lines.append(
                "I will post the results "
                f"{discord.utils.format_dt(closes_at, style='R')}. This is best "
                "effort: a restart before then cancels it, and the votes stay on "
                "the message either way."
            )
            self._schedule_poll_close(
                channel_id=target.id,
                message_id=message.id,
                guild_id=guild.id,
                question=question.strip(),
                choices=choices,
                emoji=list(emoji),
                delay=float(minutes) * 60.0,
            )

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    def _schedule_poll_close(
        self,
        *,
        channel_id: int,
        message_id: int,
        guild_id: int,
        question: str,
        choices: Sequence[str],
        emoji: Sequence[str],
        delay: float,
    ) -> None:
        """Posts the poll results after ``delay`` seconds, best effort.

        Votes live on the Discord message, so losing this task to a restart
        never loses data: only the automatic summary is skipped.
        """

        async def _runner() -> None:
            try:
                await asyncio.sleep(max(0.0, delay))
                await self._close_poll(
                    channel_id=channel_id,
                    message_id=message_id,
                    guild_id=guild_id,
                    question=question,
                    choices=list(choices),
                    emoji=list(emoji),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not close the poll %s.", message_id)

        task = asyncio.create_task(_runner(), name=f"fyrion-poll-close-{message_id}")
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _close_poll(
        self,
        *,
        channel_id: int,
        message_id: int,
        guild_id: int,
        question: str,
        choices: Sequence[str],
        emoji: Sequence[str],
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel_or_thread(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        me = guild.me
        if me is None:
            return
        permissions = channel.permissions_for(me)
        if not (
            permissions.send_messages
            and permissions.embed_links
            and permissions.read_message_history
        ):
            return

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        except discord.HTTPException as exc:
            log.debug("Could not fetch the poll message %s: %s", message_id, exc)
            return

        counts: list[int] = []
        for icon in emoji:
            reaction = discord.utils.find(
                lambda item: str(item.emoji) == icon, message.reactions
            )
            # The bot's own seeding reaction is not a vote.
            counts.append(max(0, (reaction.count - 1) if reaction is not None else 0))

        total = sum(counts)
        best = max(counts) if counts else 0

        lines: list[str] = []
        for icon, choice, count in zip(emoji, choices, counts):
            fraction = (count / total) if total else 0.0
            marker = " \U0001f3c6" if count and count == best else ""
            lines.append(
                f"{icon} **{choice}**{marker}\n"
                f"`{progress_bar(fraction)}` {count} vote"
                f"{'s' if count != 1 else ''} ({fraction * 100:.0f}%)"
            )

        embed = discord.Embed(
            title=f"\U0001f4ca Poll closed: {question}"[:256],
            description="\n".join(lines)[:4000],
            color=discord.Color.gold(),
            timestamp=utcnow(),
        )
        embed.add_field(name="Total votes", value=f"{total:,}", inline=True)
        embed.add_field(
            name="Poll", value=f"[Open the message]({message.jump_url})", inline=True
        )
        if total == 0:
            embed.set_footer(text="Nobody voted.")
        elif counts.count(best) > 1:
            embed.set_footer(text="The leading options are tied.")

        try:
            await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("Could not post the poll results: %s", exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))


__all__ = [
    "Utility",
    "CalculatorError",
    "AfkEntry",
    "REPOSITORY_URL",
    "POLL_EMOJI",
    "YES_NO_EMOJI",
    "MAX_POLL_OPTIONS",
    "MAX_EXPRESSION_LENGTH",
    "evaluate",
    "format_duration",
    "format_latency",
    "format_number",
    "human_bytes",
    "parse_iso",
    "progress_bar",
    "setup",
]
