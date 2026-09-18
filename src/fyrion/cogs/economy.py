"""
Economy cog: currency, earning, gambling, a shop and inventories.

Storage
-------
Wallets live in the core ``economy_accounts`` table declared by
:mod:`fyrion.database.schema`, so every balance read and write goes through the
pooled data layer, which validates each identifier against the schema
allow-list and binds every value as a SQL parameter. The guild-level
configuration (``economy_enabled``, ``economy_currency_symbol``,
``economy_daily_amount``, ``economy_work_amount``) lives on ``guild_settings``.

Two things the core schema does not model are owned by this module, exactly as
:mod:`fyrion.database.repositories.leveling` owns the reputation table:

* ``economy_shop_items`` - one row per purchasable item per guild;
* ``economy_cooldowns``  - durable per-action cooldowns for ``/crime`` and
  ``/rob``, which have no dedicated column on the account row.

Both statements are idempotent, are applied on cog load, and cascade from
``guild_settings`` so removing a guild leaves no orphaned rows. Inventories are
stored as JSON in ``economy_accounts.inventory`` because they are only ever read
as a whole; nothing queries or counts an individual entry.

Correctness properties worth stating explicitly
-----------------------------------------------
* **No path can create a negative balance.** Credits and debits go through
  :meth:`~fyrion.database.manager.DatabasePool.adjust_balance`, whose guard is
  part of the ``UPDATE`` statement, and ``/pay`` uses ``transfer_balance`` so
  both sides move inside one transaction. Deposits and withdrawals are single
  conditional ``UPDATE`` statements that also enforce ``bank_limit``.
* **A wager is debited before the outcome is drawn**, and the payout is credited
  afterwards, so a crash mid-game cannot mint currency.
* **Cooldowns are claimed with conditional ``UPDATE`` statements**, not with a
  read followed by a write, so two simultaneous invocations cannot both pass.
* **Shop stock cannot be oversold**: the decrement is guarded in SQL, and the
  claim is released again if the purchase later fails.
* **Blackjack resolves exactly once.** The in-flight guard is set synchronously
  before any ``await``, and a timed-out hand is settled as a stand rather than
  silently swallowing the stake.

Authorization
-------------
``default_permissions`` only decides whether Discord *shows* a command, so the
administrative commands (``/economy-toggle``, ``/shop-add``, ``/shop-remove``)
re-check ``Manage Server`` server side. Item names, descriptions and display
names are operator or member supplied, so every reply disables mention parsing;
the only exception is a notice that addresses exactly one member.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.cogs._base import FyrionCog
from fyrion.database.manager import (
    InsufficientFundsError,
    iso_from_now,
    utc_now_iso,
)
from fyrion.utils.modlog import send_log
from fyrion.utils.permissions import can_manage_role

log = logging.getLogger("fyrion.cogs.economy")

NO_MENTIONS: Final[discord.AllowedMentions] = discord.AllowedMentions.none()
# Notices address exactly one member, so user mentions are allowed and nothing
# else is: a crafted nickname can never make Fyrion ping a role or @everyone.
NOTICE_MENTIONS: Final[discord.AllowedMentions] = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)

# ---------------------------------------------------------------------------
# Limits and tuning
# ---------------------------------------------------------------------------

# Upper bound on any single transfer or shop price. Balances are 64-bit
# integers, but a value this large is always a typo rather than an intent.
MAX_TRANSFER: Final[int] = 1_000_000_000

MIN_BET: Final[int] = 10
MAX_BET: Final[int] = 250_000

DAILY_COOLDOWN_SECONDS: Final[int] = 20 * 3600
# A streak survives a missed day by a margin, so a slightly late claim does not
# wipe weeks of progress.
DAILY_STREAK_WINDOW_SECONDS: Final[int] = 48 * 3600
DAILY_STREAK_BONUS: Final[int] = 25
MAX_STREAK_BONUS_DAYS: Final[int] = 30

WORK_COOLDOWN_SECONDS: Final[int] = 3600
CRIME_COOLDOWN_SECONDS: Final[int] = 4 * 3600
ROB_COOLDOWN_SECONDS: Final[int] = 6 * 3600

CRIME_SUCCESS_CHANCE: Final[float] = 0.55
ROB_SUCCESS_CHANCE: Final[float] = 0.40
ROB_MIN_TARGET_BALANCE: Final[int] = 250
# The robber must be able to cover the fine they risk; otherwise a member with
# an empty wallet could farm attempts for free.
ROB_MIN_COLLATERAL: Final[int] = 100
ROB_MIN_SHARE: Final[float] = 0.10
ROB_MAX_SHARE: Final[float] = 0.25

GAMBLE_WIN_CHANCE: Final[float] = 0.47

# How long a settings snapshot is trusted. Writes invalidate it immediately; the
# TTL only covers changes made elsewhere (for example the dashboard).
SETTINGS_TTL_SECONDS: Final[float] = 60.0
MAX_CACHED_GUILDS: Final[int] = 500

LEADERBOARD_MIN: Final[int] = 3
LEADERBOARD_MAX: Final[int] = 25
MEDALS: Final[tuple[str, str, str]] = (
    "\U0001f947",
    "\U0001f948",
    "\U0001f949",
)

MAX_INVENTORY_ENTRIES: Final[int] = 100
MAX_ITEM_QUANTITY: Final[int] = 999
MAX_ITEM_NAME: Final[int] = 64
MAX_ITEM_DESCRIPTION: Final[int] = 200
MAX_ITEM_KEY: Final[int] = 32
MAX_SHOP_ITEMS: Final[int] = 50
SHOP_PAGE_LIMIT: Final[int] = 25
MAX_PURCHASE_QUANTITY: Final[int] = 10

BLACKJACK_TIMEOUT: Final[float] = 120.0
BLACKJACK_DECKS: Final[int] = 4
DEALER_STANDS_ON: Final[int] = 17
BLACKJACK_TARGET: Final[int] = 21

FIELD_VALUE_LIMIT: Final[int] = 1024

_KEY_STRIP: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_-]+")
_KEY_COLLAPSE: Final[re.Pattern[str]] = re.compile(r"-{2,}")
_KEY_VALID: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# ---------------------------------------------------------------------------
# Flavour text
# ---------------------------------------------------------------------------

WORK_JOBS: Final[tuple[str, ...]] = (
    "moderated a very busy support channel",
    "wrote documentation nobody wanted to write",
    "reviewed a pull request end to end",
    "fixed a flaky test suite",
    "designed a new server banner",
    "hosted a community game night",
    "tidied up the channel categories",
    "answered every question in the help channel",
    "migrated a database without downtime",
    "triaged a week of bug reports",
)

CRIME_SUCCESS_TEXT: Final[tuple[str, ...]] = (
    "You sold counterfeit server boosts and nobody noticed",
    "You skimmed the vending machine in the staff lounge",
    "You resold expired giveaway codes at a premium",
    "You quietly rerouted a sponsorship payment",
    "You ran an unlicensed emoji factory for an afternoon",
)

CRIME_FAILURE_TEXT: Final[tuple[str, ...]] = (
    "A moderator noticed the paper trail immediately",
    "You tripped the audit log on your way out",
    "Your accomplice confessed within the hour",
    "You left your username in the file metadata",
    "The scheme collapsed before the first payout",
)

ROB_SUCCESS_TEXT: Final[tuple[str, ...]] = (
    "You lifted their wallet while they were typing",
    "You swapped their wallet for an identical empty one",
    "They left their wallet open in a voice channel",
)

ROB_FAILURE_TEXT: Final[tuple[str, ...]] = (
    "They caught you mid-reach and called security",
    "Their wallet was chained to the desk",
    "You picked the wrong pocket entirely",
)

# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

# (symbol, weight, three-of-a-kind multiplier). Weights are deliberately
# lopsided so the rare symbols stay rare; the two-of-a-kind consolation keeps
# the return-to-player around 0.8, which is a house edge, not a money printer.
SLOT_REELS: Final[tuple[tuple[str, int, int], ...]] = (
    ("\U0001f352", 24, 3),
    ("\U0001f34b", 20, 4),
    ("\U0001f34a", 16, 6),
    ("\U0001f514", 12, 10),
    ("\u2b50", 8, 20),
    ("\U0001f48e", 4, 50),
    ("7\ufe0f\u20e3", 2, 100),
)
SLOT_PAIR_MULTIPLIER: Final[float] = 1.5

# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

CARD_RANKS: Final[tuple[str, ...]] = (
    "A",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "J",
    "Q",
    "K",
)
CARD_SUITS: Final[tuple[str, ...]] = (
    "\u2660",
    "\u2665",
    "\u2666",
    "\u2663",
)
CARD_VALUES: Final[dict[str, int]] = {
    "A": 11,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "J": 10,
    "Q": 10,
    "K": 10,
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def as_int(value: Any, default: int = 0) -> int:
    """Coerces a stored value to int, falling back on anything unusable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_money(amount: Any, symbol: str) -> str:
    """Renders a currency amount with the guild's symbol."""
    return f"{symbol}{as_int(amount):,}"


def format_delay(seconds: float) -> str:
    """Renders a retry delay as a short human readable string."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def truncate(text: str, limit: int = FIELD_VALUE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def sanitize(text: Any, limit: int = 120) -> str:
    """Escapes operator or member supplied text so it renders inertly."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return "\u2014"
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "\u2026"
    return discord.utils.escape_markdown(collapsed.replace("@", "@\u200b"))


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


def retry_after(moment: Any, cooldown_seconds: int) -> float:
    """Returns how long remains of a cooldown that started at ``moment``."""
    started = parse_iso(moment)
    if started is None or cooldown_seconds <= 0:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return max(0.0, float(cooldown_seconds) - elapsed)


def seconds_until(moment: Any) -> float:
    """Returns how long remains until ``moment``, never negative."""
    target = parse_iso(moment)
    if target is None:
        return 0.0
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def slugify_key(raw: str) -> str:
    """Turns an item name into a stable lookup key."""
    lowered = (raw or "").strip().lower().replace(" ", "-")
    cleaned = _KEY_STRIP.sub("", lowered)
    cleaned = _KEY_COLLAPSE.sub("-", cleaned).strip("-_")
    return cleaned[:MAX_ITEM_KEY]


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------


def load_inventory(raw: Any) -> list[dict[str, Any]]:
    """Parses a stored inventory blob.

    Anything unparseable degrades to an empty inventory: a corrupt blob must not
    raise on the purchase path.
    """
    entries: Any = raw
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            entries = json.loads(raw)
        except (ValueError, TypeError):
            return []

    if not isinstance(entries, (list, tuple)):
        return []

    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or "").strip().lower()[:MAX_ITEM_KEY]
        if not key:
            continue
        quantity = max(1, min(MAX_ITEM_QUANTITY, as_int(entry.get("quantity"), 1)))
        items.append(
            {
                "key": key,
                "name": str(entry.get("name") or key)[:MAX_ITEM_NAME],
                "quantity": quantity,
                "acquired_at": str(entry.get("acquired_at") or "") or None,
            }
        )
        if len(items) >= MAX_INVENTORY_ENTRIES:
            break
    return items


def dump_inventory(items: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "key": str(item.get("key")),
            "name": str(item.get("name") or item.get("key"))[:MAX_ITEM_NAME],
            "quantity": max(
                1, min(MAX_ITEM_QUANTITY, as_int(item.get("quantity"), 1))
            ),
            "acquired_at": item.get("acquired_at"),
        }
        for item in list(items)[:MAX_INVENTORY_ENTRIES]
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def owned_quantity(items: Sequence[Mapping[str, Any]], key: str) -> int:
    for item in items:
        if str(item.get("key")) == key:
            return as_int(item.get("quantity"), 0)
    return 0


# ---------------------------------------------------------------------------
# Cards and hands
# ---------------------------------------------------------------------------


@dataclass
class Hand:
    """A blackjack hand, with soft-ace scoring."""

    cards: list[tuple[str, str]] = field(default_factory=list)

    def add(self, card: tuple[str, str]) -> None:
        self.cards.append(card)

    @property
    def total(self) -> int:
        total = 0
        aces = 0
        for rank, _ in self.cards:
            total += CARD_VALUES[rank]
            if rank == "A":
                aces += 1
        # Every ace may count as one instead of eleven, applied only as far as
        # is needed to stay under the target.
        while total > BLACKJACK_TARGET and aces:
            total -= 10
            aces -= 1
        return total

    @property
    def is_soft(self) -> bool:
        # Soft in the standard sense: an ace is still counted as 11 in the
        # final total (so the hand cannot bust on the next hit). Mirror the
        # ace-reduction in ``total`` and report whether any ace survives as 11.
        total = 0
        aces = 0
        for rank, _ in self.cards:
            total += CARD_VALUES[rank]
            if rank == "A":
                aces += 1
        while total > BLACKJACK_TARGET and aces:
            total -= 10
            aces -= 1
        return aces > 0

    @property
    def is_bust(self) -> bool:
        return self.total > BLACKJACK_TARGET

    @property
    def is_blackjack(self) -> bool:
        return len(self.cards) == 2 and self.total == BLACKJACK_TARGET

    def render(self, *, hide_after: int | None = None) -> str:
        parts: list[str] = []
        for index, (rank, suit) in enumerate(self.cards):
            if hide_after is not None and index >= hide_after:
                parts.append("`??`")
            else:
                parts.append(f"`{rank}{suit}`")
        return " ".join(parts)


def build_deck(rng: random.Random, decks: int = BLACKJACK_DECKS) -> list[tuple[str, str]]:
    deck = [
        (rank, suit)
        for _ in range(max(1, decks))
        for suit in CARD_SUITS
        for rank in CARD_RANKS
    ]
    rng.shuffle(deck)
    return deck


def blackjack_payout(stake: int, player: Hand, dealer: Hand) -> tuple[str, int]:
    """Returns ``(outcome, total_returned)`` for a finished hand.

    ``total_returned`` includes the stake, which was already debited when the
    hand was dealt: 0 is a loss, ``stake`` is a push, ``stake * 2`` a win and
    ``stake * 5 // 2`` a natural blackjack paid at 3:2.
    """
    if player.is_bust:
        return ("You went bust.", 0)
    if player.is_blackjack and dealer.is_blackjack:
        return ("Push \u2014 you both had blackjack.", stake)
    if player.is_blackjack:
        return ("Blackjack! Paid at 3:2.", stake * 5 // 2)
    if dealer.is_blackjack:
        return ("The dealer had blackjack.", 0)
    if dealer.is_bust:
        return ("The dealer went bust. You win.", stake * 2)

    if player.total > dealer.total:
        return ("You win.", stake * 2)
    if player.total < dealer.total:
        return ("The dealer wins.", 0)
    return ("Push \u2014 your stake was returned.", stake)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EconomySettings:
    """A guild's economy configuration, pre-coerced for the command path."""

    enabled: bool = False
    symbol: str = "$"
    daily_amount: int = 250
    work_amount: int = 100


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a cooldown claim."""

    granted: bool
    retry_after: float = 0.0
    streak: int = 0
    continued: bool = False


@dataclass
class BlackjackGame:
    """State for one blackjack hand."""

    guild_id: int
    user_id: int
    stake: int
    deck: list[tuple[str, str]]
    player: Hand
    dealer: Hand
    doubled: bool = False
    finished: bool = False
    resolving: bool = False

    def draw(self) -> tuple[str, str]:
        if not self.deck:
            # Four decks cannot be exhausted by one hand, but refusing to fail
            # on an empty deck is cheaper than reasoning about it.
            self.deck = build_deck(random.SystemRandom())
        return self.deck.pop()

    @property
    def can_double(self) -> bool:
        return (
            not self.finished
            and not self.doubled
            and len(self.player.cards) == 2
            and not self.player.is_bust
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class EconomyRepository:
    """Reads and writes wallets, cooldowns, shop items and inventories.

    The shop and cooldown tables now live in the core schema and are applied by
    the connection pool at boot, so this repository no longer manages any DDL.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_settings(self, guild_id: int) -> EconomySettings:
        settings = await self.db.get_guild_settings(int(guild_id))
        symbol = str(settings.get("economy_currency_symbol") or "$")[:8] or "$"
        return EconomySettings(
            enabled=bool(settings.get("economy_enabled")),
            symbol=symbol,
            daily_amount=max(0, as_int(settings.get("economy_daily_amount"), 250)),
            work_amount=max(0, as_int(settings.get("economy_work_amount"), 100)),
        )

    async def update_settings(self, guild_id: int, **values: Any) -> None:
        if not values:
            return
        await self.db.update_guild_settings(int(guild_id), **values)

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    async def get_account(self, guild_id: int, user_id: int) -> dict[str, Any]:
        return await self.db.get_economy_account(int(guild_id), int(user_id))

    async def credit(self, guild_id: int, user_id: int, amount: int) -> int:
        """Adds to a wallet and returns the new balance."""
        amount = max(0, int(amount))
        if amount == 0:
            account = await self.get_account(guild_id, user_id)
            return as_int(account.get("balance"))
        return await self.db.adjust_balance(int(guild_id), int(user_id), amount)

    async def debit(self, guild_id: int, user_id: int, amount: int) -> int:
        """Removes from a wallet, raising when the balance is insufficient."""
        return await self.db.adjust_balance(int(guild_id), int(user_id), -abs(int(amount)))

    async def take_up_to(self, guild_id: int, user_id: int, amount: int) -> int:
        """Debits at most ``amount``, returning what was actually taken.

        Used for fines, which must never push an account negative and must not
        fail outright when the member cannot cover the full penalty.
        """
        wanted = max(0, int(amount))
        if wanted == 0:
            return 0

        # A concurrent spend can invalidate the cap between the read and the
        # write, so one retry with a fresh balance is allowed.
        for _ in range(2):
            account = await self.get_account(guild_id, user_id)
            capped = min(wanted, as_int(account.get("balance")))
            if capped <= 0:
                return 0
            try:
                await self.debit(guild_id, user_id, capped)
            except InsufficientFundsError:
                continue
            return capped
        return 0

    async def deposit(
        self, guild_id: int, user_id: int, amount: int | None
    ) -> tuple[int, str | None]:
        """Moves wallet funds into the bank. Returns ``(moved, error)``.

        Both the wallet floor and the bank ceiling are enforced inside a single
        conditional ``UPDATE``, so two concurrent deposits cannot exceed either.
        """
        account = await self.get_account(guild_id, user_id)
        balance = as_int(account.get("balance"))
        bank = as_int(account.get("bank"))
        limit = as_int(account.get("bank_limit"))

        if balance <= 0:
            return 0, "Your wallet is empty."

        room = max(0, limit - bank)
        if room <= 0:
            return 0, "Your bank is already full."

        requested = balance if amount is None else min(int(amount), balance)
        moved = min(requested, room)
        if moved <= 0:
            return 0, "There is nothing to deposit."

        changed = await self.db.execute(
            "UPDATE economy_accounts "
            "   SET balance = balance - ?, bank = bank + ? "
            " WHERE guild_id = ? AND user_id = ? "
            "   AND balance >= ? AND bank + ? <= bank_limit",
            (moved, moved, int(guild_id), int(user_id), moved, moved),
        )
        if not changed:
            return 0, (
                "Your balance changed while that was processing. Please try again."
            )
        return moved, None

    async def withdraw(
        self, guild_id: int, user_id: int, amount: int | None
    ) -> tuple[int, str | None]:
        """Moves bank funds into the wallet. Returns ``(moved, error)``."""
        account = await self.get_account(guild_id, user_id)
        bank = as_int(account.get("bank"))

        if bank <= 0:
            return 0, "Your bank account is empty."

        moved = bank if amount is None else min(int(amount), bank)
        if moved <= 0:
            return 0, "There is nothing to withdraw."

        changed = await self.db.execute(
            "UPDATE economy_accounts "
            "   SET bank = bank - ?, balance = balance + ? "
            " WHERE guild_id = ? AND user_id = ? AND bank >= ?",
            (moved, moved, int(guild_id), int(user_id), moved),
        )
        if not changed:
            return 0, (
                "Your balance changed while that was processing. Please try again."
            )
        return moved, None

    async def transfer(
        self, guild_id: int, sender_id: int, recipient_id: int, amount: int
    ) -> None:
        """Moves currency between two wallets in one transaction."""
        await self.db.transfer_balance(
            int(guild_id), int(sender_id), int(recipient_id), int(amount)
        )

    async def leaderboard(self, guild_id: int, limit: int) -> list[dict[str, Any]]:
        return await self.db.economy_leaderboard(int(guild_id), max(1, int(limit)))

    async def tracked_members(self, guild_id: int) -> int:
        return await self.db.count("economy_accounts", {"guild_id": int(guild_id)})

    async def rank_of(self, guild_id: int, user_id: int, net_worth: int) -> int:
        """Returns a member's 1-based rank by net worth.

        The tie-break matches the leaderboard ordering, so ``/balance`` and
        ``/leaderboard-economy`` always agree.
        """
        value = await self.db.fetchval(
            "SELECT COUNT(*) + 1 FROM economy_accounts "
            " WHERE guild_id = ? "
            "   AND ((balance + bank) > ? "
            "        OR ((balance + bank) = ? AND user_id < ?))",
            (int(guild_id), int(net_worth), int(net_worth), int(user_id)),
            default=1,
        )
        return max(1, as_int(value, 1))

    # ------------------------------------------------------------------
    # Cooldowns
    # ------------------------------------------------------------------

    async def claim_daily(self, guild_id: int, user_id: int) -> ClaimResult:
        """Claims the daily reward, maintaining the streak.

        The cooldown lives in the ``WHERE`` clause, so two simultaneous claims
        cannot both succeed: after the first write ``last_daily_at`` is newer
        than the cutoff and the second update matches no rows.
        """
        account = await self.get_account(guild_id, user_id)
        last = account.get("last_daily_at")

        remaining = retry_after(last, DAILY_COOLDOWN_SECONDS)
        if remaining > 0:
            return ClaimResult(granted=False, retry_after=remaining)

        previous = max(0, as_int(account.get("daily_streak")))
        elapsed = None
        moment = parse_iso(last)
        if moment is not None:
            elapsed = (datetime.now(timezone.utc) - moment).total_seconds()

        continued = elapsed is not None and elapsed <= DAILY_STREAK_WINDOW_SECONDS
        streak = previous + 1 if continued else 1

        now = utc_now_iso()
        cutoff = iso_from_now(-DAILY_COOLDOWN_SECONDS)

        changed = await self.db.execute(
            "UPDATE economy_accounts "
            "   SET daily_streak = ?, last_daily_at = ? "
            " WHERE guild_id = ? AND user_id = ? "
            "   AND (last_daily_at IS NULL OR last_daily_at <= ?)",
            (streak, now, int(guild_id), int(user_id), cutoff),
        )
        if not changed:
            refreshed = await self.get_account(guild_id, user_id)
            return ClaimResult(
                granted=False,
                retry_after=retry_after(
                    refreshed.get("last_daily_at"), DAILY_COOLDOWN_SECONDS
                ),
            )

        return ClaimResult(granted=True, streak=streak, continued=continued)

    async def claim_work(self, guild_id: int, user_id: int) -> ClaimResult:
        account = await self.get_account(guild_id, user_id)
        remaining = retry_after(account.get("last_work_at"), WORK_COOLDOWN_SECONDS)
        if remaining > 0:
            return ClaimResult(granted=False, retry_after=remaining)

        now = utc_now_iso()
        cutoff = iso_from_now(-WORK_COOLDOWN_SECONDS)
        changed = await self.db.execute(
            "UPDATE economy_accounts SET last_work_at = ? "
            " WHERE guild_id = ? AND user_id = ? "
            "   AND (last_work_at IS NULL OR last_work_at <= ?)",
            (now, int(guild_id), int(user_id), cutoff),
        )
        if not changed:
            refreshed = await self.get_account(guild_id, user_id)
            return ClaimResult(
                granted=False,
                retry_after=retry_after(
                    refreshed.get("last_work_at"), WORK_COOLDOWN_SECONDS
                ),
            )
        return ClaimResult(granted=True)

    async def claim_action(
        self, guild_id: int, user_id: int, action: str, seconds: int
    ) -> ClaimResult:
        """Claims a durable per-action cooldown."""
        await self.db.ensure_guild(int(guild_id))

        now = utc_now_iso()
        next_at = iso_from_now(max(0, int(seconds)))

        # The seeded row is immediately claimable, so a first-time caller is not
        # penalised by the insert.
        await self.db.execute(
            "INSERT OR IGNORE INTO economy_cooldowns "
            "    (guild_id, user_id, action, available_at) VALUES (?, ?, ?, ?)",
            (int(guild_id), int(user_id), action, now),
        )
        changed = await self.db.execute(
            "UPDATE economy_cooldowns SET available_at = ? "
            " WHERE guild_id = ? AND user_id = ? AND action = ? "
            "   AND available_at <= ?",
            (next_at, int(guild_id), int(user_id), action, now),
        )
        if changed:
            return ClaimResult(granted=True)

        row = await self.db.fetchrow(
            "SELECT available_at FROM economy_cooldowns "
            " WHERE guild_id = ? AND user_id = ? AND action = ?",
            (int(guild_id), int(user_id), action),
        )
        available_at = row["available_at"] if row is not None else None
        return ClaimResult(granted=False, retry_after=seconds_until(available_at))

    async def release_action(self, guild_id: int, user_id: int, action: str) -> None:
        """Clears a cooldown that was claimed for an action that never ran."""
        await self.db.execute(
            "UPDATE economy_cooldowns SET available_at = ? "
            " WHERE guild_id = ? AND user_id = ? AND action = ?",
            (utc_now_iso(), int(guild_id), int(user_id), action),
        )

    # ------------------------------------------------------------------
    # Shop
    # ------------------------------------------------------------------

    async def list_items(
        self, guild_id: int, *, enabled_only: bool = True, limit: int = SHOP_PAGE_LIMIT
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT item_id, guild_id, item_key, name, description, price, "
            "       role_id, stock, max_per_user, enabled "
            "  FROM economy_shop_items WHERE guild_id = ?"
        )
        params: list[Any] = [int(guild_id)]
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY price ASC, item_key ASC LIMIT ?"
        params.append(max(1, int(limit)))

        rows = await self.db.fetchall(query, tuple(params))
        return [dict(row) for row in rows]

    async def get_item(self, guild_id: int, key: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "SELECT * FROM economy_shop_items WHERE guild_id = ? AND item_key = ?",
            (int(guild_id), str(key).lower()),
        )
        return dict(row) if row is not None else None

    async def count_items(self, guild_id: int) -> int:
        value = await self.db.fetchval(
            "SELECT COUNT(*) FROM economy_shop_items WHERE guild_id = ?",
            (int(guild_id),),
            default=0,
        )
        return as_int(value)

    async def save_item(
        self,
        guild_id: int,
        *,
        key: str,
        name: str,
        price: int,
        description: str | None = None,
        role_id: int | None = None,
        stock: int | None = None,
        max_per_user: int | None = None,
        created_by: int | None = None,
    ) -> dict[str, Any]:
        await self.db.ensure_guild(int(guild_id))

        await self.db.execute(
            "INSERT INTO economy_shop_items "
            "    (guild_id, item_key, name, description, price, role_id, stock, "
            "     max_per_user, enabled, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT (guild_id, item_key) DO UPDATE SET "
            "    name = excluded.name, "
            "    description = excluded.description, "
            "    price = excluded.price, "
            "    role_id = excluded.role_id, "
            "    stock = excluded.stock, "
            "    max_per_user = excluded.max_per_user, "
            "    enabled = 1",
            (
                int(guild_id),
                str(key).lower(),
                str(name)[:MAX_ITEM_NAME],
                str(description)[:MAX_ITEM_DESCRIPTION] if description else None,
                max(0, int(price)),
                int(role_id) if role_id else None,
                int(stock) if stock is not None else None,
                int(max_per_user) if max_per_user else None,
                int(created_by) if created_by else None,
            ),
        )

        saved = await self.get_item(guild_id, key)
        if saved is None:  # pragma: no cover - the upsert just succeeded
            raise RuntimeError(f"Shop item {key!r} disappeared after being saved.")
        return saved

    async def delete_item(self, guild_id: int, key: str) -> bool:
        removed = await self.db.execute(
            "DELETE FROM economy_shop_items WHERE guild_id = ? AND item_key = ?",
            (int(guild_id), str(key).lower()),
        )
        return bool(removed)

    async def claim_stock(self, item_id: int, quantity: int) -> bool:
        """Reserves stock for a purchase. Returns False when sold out.

        The availability guard is part of the ``UPDATE``, so two buyers cannot
        both claim the last unit. ``stock`` is ``NULL`` for unlimited items, and
        ``NULL - n`` stays ``NULL``.
        """
        changed = await self.db.execute(
            "UPDATE economy_shop_items SET stock = stock - ? "
            " WHERE item_id = ? AND enabled = 1 "
            "   AND (stock IS NULL OR stock >= ?)",
            (max(1, int(quantity)), int(item_id), max(1, int(quantity))),
        )
        return bool(changed)

    async def release_stock(self, item_id: int, quantity: int) -> None:
        """Returns reserved stock after a purchase failed."""
        await self.db.execute(
            "UPDATE economy_shop_items SET stock = stock + ? "
            " WHERE item_id = ? AND stock IS NOT NULL",
            (max(1, int(quantity)), int(item_id)),
        )

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    async def get_inventory(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        account = await self.get_account(guild_id, user_id)
        return load_inventory(account.get("inventory"))

    async def add_to_inventory(
        self, guild_id: int, user_id: int, *, key: str, name: str, quantity: int
    ) -> tuple[bool, str | None]:
        """Adds an item to a member's inventory. Returns ``(ok, error)``."""
        items = await self.get_inventory(guild_id, user_id)
        wanted = max(1, int(quantity))

        for item in items:
            if str(item.get("key")) == key:
                total = as_int(item.get("quantity"), 0) + wanted
                if total > MAX_ITEM_QUANTITY:
                    return False, (
                        f"You cannot hold more than {MAX_ITEM_QUANTITY} of one item."
                    )
                item["quantity"] = total
                item["name"] = name[:MAX_ITEM_NAME]
                break
        else:
            if len(items) >= MAX_INVENTORY_ENTRIES:
                return False, (
                    "Your inventory is full "
                    f"({MAX_INVENTORY_ENTRIES} distinct items)."
                )
            if wanted > MAX_ITEM_QUANTITY:
                return False, (
                    f"You cannot hold more than {MAX_ITEM_QUANTITY} of one item."
                )
            items.append(
                {
                    "key": key,
                    "name": name[:MAX_ITEM_NAME],
                    "quantity": wanted,
                    "acquired_at": utc_now_iso(),
                }
            )

        await self.db.update(
            "economy_accounts",
            {"inventory": dump_inventory(items)},
            {"guild_id": int(guild_id), "user_id": int(user_id)},
        )
        return True, None


# ---------------------------------------------------------------------------
# Blackjack view
# ---------------------------------------------------------------------------


class BlackjackView(discord.ui.View):
    """Hit, stand and double controls for one blackjack hand.

    Deliberately *not* persistent: an abandoned hand must settle on its own
    rather than survive a restart with a stake already debited. The timeout
    stands the hand, which is the outcome most favourable to the player that
    does not simply hand the stake back.
    """

    def __init__(
        self,
        cog: "Economy",
        game: BlackjackGame,
        player: discord.Member,
        settings: EconomySettings,
    ) -> None:
        super().__init__(timeout=BLACKJACK_TIMEOUT)
        self.cog = cog
        self.game = game
        self.player = player
        self.settings = settings
        self.message: discord.Message | discord.InteractionMessage | None = None
        self.refresh_buttons()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def refresh_buttons(self) -> None:
        self.double_button.disabled = not self.game.can_double

    def disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message(
                "\u274c That hand belongs to someone else.", ephemeral=True
            )
            return False
        if self.game.finished or self.game.resolving:
            await interaction.response.send_message(
                "\u2139\ufe0f That hand has already been settled.", ephemeral=True
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="\u2795")
    async def hit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.game.player.add(self.game.draw())

        if self.game.player.is_bust or self.game.player.total == BLACKJACK_TARGET:
            await self._settle(interaction)
            return

        self.refresh_buttons()
        await interaction.response.edit_message(
            embed=self.cog.blackjack_embed(self.game, self.player, self.settings),
            view=self,
        )

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="\u270b")
    async def stand_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._settle(interaction)

    @discord.ui.button(
        label="Double down", style=discord.ButtonStyle.success, emoji="\u2696\ufe0f"
    )
    async def double_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.game.can_double:
            await interaction.response.send_message(
                "\u274c You can only double down on your first two cards.",
                ephemeral=True,
            )
            return

        extra = self.game.stake
        try:
            await self.cog.repo.debit(self.game.guild_id, self.game.user_id, extra)
        except InsufficientFundsError:
            await interaction.response.send_message(
                "\u274c You cannot afford to double down. "
                f"That needs another {format_money(extra, self.settings.symbol)}.",
                ephemeral=True,
            )
            return
        except Exception:
            log.exception(
                "Could not take the double-down stake for member %s.",
                self.game.user_id,
            )
            await interaction.response.send_message(
                "\u274c That could not be processed. Your hand is unchanged.",
                ephemeral=True,
            )
            return

        self.game.doubled = True
        self.game.stake += extra
        self.game.player.add(self.game.draw())
        await self._settle(interaction)

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def _settle(self, interaction: discord.Interaction) -> None:
        # Set synchronously, before any await, so a rapid second click cannot
        # enter the payout path twice.
        if self.game.resolving or self.game.finished:
            return
        self.game.resolving = True

        embed = await self.cog.settle_blackjack(self.game, self.player, self.settings)
        self.disable_all()
        self.stop()

        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException as exc:
            log.warning("Could not show the blackjack result: %s", exc)

    async def on_timeout(self) -> None:
        if self.game.resolving or self.game.finished:
            return
        self.game.resolving = True

        try:
            embed = await self.cog.settle_blackjack(
                self.game, self.player, self.settings, timed_out=True
            )
        except Exception:
            log.exception("Could not settle a timed-out blackjack hand.")
            return

        self.disable_all()
        if self.message is None:
            return
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            # The result is already stored; only the message could not be shown.
            pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Economy(FyrionCog, commands.Cog):
    """Currency, earning commands, gambling, a shop and inventories."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self.repo = EconomyRepository(self.db)
        # SystemRandom so outcomes are not predictable from previous results.
        self._rng = random.SystemRandom()

        # guild_id -> (expires_at, settings)
        self._settings_cache: dict[int, tuple[float, EconomySettings]] = {}
        # (guild_id, user_id) for members with a hand in progress.
        self._in_play: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    # Settings cache
    # ------------------------------------------------------------------

    def invalidate(self, guild_id: int) -> None:
        self._settings_cache.pop(int(guild_id), None)

    async def settings_for(self, guild_id: int) -> EconomySettings:
        now = time.monotonic()
        cached = self._settings_cache.get(int(guild_id))
        if cached is not None and cached[0] > now:
            return cached[1]

        try:
            settings = await self.repo.get_settings(guild_id)
        except Exception:
            # Cache a disabled snapshot briefly rather than turning a broken
            # database into one failing query per invocation.
            log.exception("Could not load economy settings for guild %s.", guild_id)
            settings = EconomySettings()

        if len(self._settings_cache) >= MAX_CACHED_GUILDS:
            stale = [
                key
                for key, (expires_at, _) in self._settings_cache.items()
                if expires_at <= now
            ]
            for key in stale:
                del self._settings_cache[key]
            if len(self._settings_cache) >= MAX_CACHED_GUILDS:
                self._settings_cache.clear()

        self._settings_cache[int(guild_id)] = (now + SETTINGS_TTL_SECONDS, settings)
        return settings

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    async def _ready(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, discord.Member, EconomySettings] | None:
        """Resolves the invoking context and checks the economy is enabled."""
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return None

        settings = await self.settings_for(guild.id)
        if not settings.enabled:
            await self._reject(
                interaction,
                "The economy is disabled in this server. An administrator can "
                "enable it with `/economy-toggle`.",
            )
            return None

        return guild, member, settings

    async def _authorize(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Re-checks ``Manage Server`` server side for administrative commands."""
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
        """Mirrors an administrative change into the guild's log channel."""
        embed = discord.Embed(
            title=f"Economy: {action}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Performed by", value=f"{actor} (`{actor.id}`)", inline=False
        )
        embed.add_field(name="Details", value=truncate(detail), inline=False)
        await send_log(self.db, guild, embed)

    # ------------------------------------------------------------------
    # /balance
    # ------------------------------------------------------------------

    @app_commands.command(
        name="balance", description="Check a member's wallet, bank and net worth."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to inspect (defaults to you)")
    async def balance_cmd(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, invoker, settings = context

        target = member or invoker
        if target.bot:
            await self._reject(interaction, "Bots do not hold currency.")
            return
        if target.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return

        await interaction.response.defer()

        account = await self.repo.get_account(guild.id, target.id)
        wallet = as_int(account.get("balance"))
        bank = as_int(account.get("bank"))
        limit = as_int(account.get("bank_limit"))
        net_worth = wallet + bank

        rank = await self.repo.rank_of(guild.id, target.id, net_worth)
        tracked = await self.repo.tracked_members(guild.id)

        embed = discord.Embed(
            title=f"\U0001f4b0 Balance: {target.display_name}",
            color=target.color if target.color.value else discord.Color.green(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="Wallet", value=format_money(wallet, settings.symbol), inline=True
        )
        embed.add_field(
            name="Bank",
            value=(
                f"{format_money(bank, settings.symbol)} / "
                f"{format_money(limit, settings.symbol)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Net worth",
            value=format_money(net_worth, settings.symbol),
            inline=True,
        )
        embed.add_field(
            name="Rank", value=f"#{rank} of {max(tracked, 1)}", inline=True
        )
        embed.add_field(
            name="Daily streak",
            value=f"{as_int(account.get('daily_streak'))} day(s)",
            inline=True,
        )
        embed.add_field(
            name="Lifetime",
            value=(
                f"earned {format_money(account.get('total_earned'), settings.symbol)}"
                "\n"
                f"spent {format_money(account.get('total_spent'), settings.symbol)}"
            ),
            inline=True,
        )

        room = max(0, limit - bank)
        if wallet > 0 and room > 0:
            embed.set_footer(
                text=(
                    "Currency in your wallet can be stolen with /rob. Bank it with "
                    "/deposit."
                )
            )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /deposit and /withdraw
    # ------------------------------------------------------------------

    @app_commands.command(
        name="deposit", description="Move currency from your wallet into your bank."
    )
    @app_commands.guild_only()
    @app_commands.describe(amount="How much to deposit (leave empty for everything)")
    async def deposit_cmd(
        self,
        interaction: discord.Interaction,
        amount: Optional[app_commands.Range[int, 1, MAX_TRANSFER]] = None,
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer(ephemeral=True)

        moved, error = await self.repo.deposit(guild.id, member.id, amount)
        if not moved:
            await self._reject(interaction, error or "Nothing could be deposited.")
            return

        account = await self.repo.get_account(guild.id, member.id)
        await self._respond(
            interaction,
            f"\U0001f3e6 Deposited **{format_money(moved, settings.symbol)}**.\n"
            f"Wallet: {format_money(account.get('balance'), settings.symbol)} "
            f"\u2022 bank: {format_money(account.get('bank'), settings.symbol)} / "
            f"{format_money(account.get('bank_limit'), settings.symbol)}",
        )

    @app_commands.command(
        name="withdraw", description="Move currency from your bank into your wallet."
    )
    @app_commands.guild_only()
    @app_commands.describe(amount="How much to withdraw (leave empty for everything)")
    async def withdraw_cmd(
        self,
        interaction: discord.Interaction,
        amount: Optional[app_commands.Range[int, 1, MAX_TRANSFER]] = None,
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer(ephemeral=True)

        moved, error = await self.repo.withdraw(guild.id, member.id, amount)
        if not moved:
            await self._reject(interaction, error or "Nothing could be withdrawn.")
            return

        account = await self.repo.get_account(guild.id, member.id)
        await self._respond(
            interaction,
            f"\U0001f4b5 Withdrew **{format_money(moved, settings.symbol)}**.\n"
            f"Wallet: {format_money(account.get('balance'), settings.symbol)} "
            f"\u2022 bank: {format_money(account.get('bank'), settings.symbol)}\n"
            "Currency in your wallet can be stolen with `/rob`.",
        )

    # ------------------------------------------------------------------
    # /pay
    # ------------------------------------------------------------------

    @app_commands.command(name="pay", description="Send currency to another member.")
    @app_commands.guild_only()
    @app_commands.describe(
        member="Who to pay", amount="How much to send from your wallet"
    )
    async def pay_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, MAX_TRANSFER],
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, sender, settings = context

        if member.id == sender.id:
            await self._reject(interaction, "You cannot pay yourself.")
            return
        if member.bot:
            await self._reject(interaction, "Bots cannot hold currency.")
            return
        if member.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return

        await interaction.response.defer()

        # Creating the recipient's row first keeps the transfer from failing on a
        # missing account.
        await self.repo.get_account(guild.id, member.id)

        try:
            await self.repo.transfer(guild.id, sender.id, member.id, int(amount))
        except InsufficientFundsError:
            account = await self.repo.get_account(guild.id, sender.id)
            await self._reject(
                interaction,
                f"You only have {format_money(account.get('balance'), settings.symbol)} "
                "in your wallet. Withdraw from your bank first if you need more.",
            )
            return
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return
        except Exception:
            log.exception(
                "Could not transfer currency from %s to %s in guild %s.",
                sender.id,
                member.id,
                guild.id,
            )
            await self._reject(
                interaction, "That payment could not be processed. Please try again."
            )
            return

        embed = discord.Embed(
            title="Payment sent",
            description=(
                f"{sender.mention} paid {member.mention} "
                f"**{format_money(amount, settings.symbol)}**."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )

        sender_account = await self.repo.get_account(guild.id, sender.id)
        embed.add_field(
            name="Your wallet",
            value=format_money(sender_account.get("balance"), settings.symbol),
            inline=True,
        )

        await self._send_embed(interaction, embed, mentions=NOTICE_MENTIONS)

    # ------------------------------------------------------------------
    # /daily
    # ------------------------------------------------------------------

    @app_commands.command(
        name="daily", description="Claim your daily reward and build a streak."
    )
    @app_commands.guild_only()
    async def daily_cmd(self, interaction: discord.Interaction) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer()

        if settings.daily_amount <= 0:
            await self._reject(
                interaction,
                "The daily reward is set to zero in this server, so there is "
                "nothing to claim.",
            )
            return

        try:
            result = await self.repo.claim_daily(guild.id, member.id)
        except Exception:
            log.exception("Could not claim the daily reward in guild %s.", guild.id)
            await self._reject(
                interaction, "That could not be processed. Please try again shortly."
            )
            return

        if not result.granted:
            await self._respond(
                interaction,
                "\u23f3 You have already claimed today. Come back in "
                f"**{format_delay(result.retry_after)}**.",
            )
            return

        bonus_days = min(result.streak, MAX_STREAK_BONUS_DAYS)
        bonus = bonus_days * DAILY_STREAK_BONUS
        total = settings.daily_amount + bonus

        balance = await self.repo.credit(guild.id, member.id, total)

        embed = discord.Embed(
            title="Daily reward claimed",
            description=(
                f"{member.mention} collected "
                f"**{format_money(total, settings.symbol)}**."
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Base",
            value=format_money(settings.daily_amount, settings.symbol),
            inline=True,
        )
        embed.add_field(
            name=f"Streak bonus ({bonus_days}x)",
            value=format_money(bonus, settings.symbol),
            inline=True,
        )
        embed.add_field(
            name="Wallet", value=format_money(balance, settings.symbol), inline=True
        )
        embed.add_field(
            name="Streak",
            value=(
                f"{result.streak} day(s)"
                + ("" if result.continued or result.streak == 1 else " (restarted)")
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Claim again in {format_delay(DAILY_COOLDOWN_SECONDS)}. Your streak "
                f"survives for {DAILY_STREAK_WINDOW_SECONDS // 3600} hours."
            )
        )

        await self._send_embed(interaction, embed, mentions=NOTICE_MENTIONS)

    # ------------------------------------------------------------------
    # /work
    # ------------------------------------------------------------------

    @app_commands.command(name="work", description="Work an honest shift for currency.")
    @app_commands.guild_only()
    async def work_cmd(self, interaction: discord.Interaction) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer()

        if settings.work_amount <= 0:
            await self._reject(
                interaction,
                "The work reward is set to zero in this server, so there is "
                "nothing to earn.",
            )
            return

        try:
            result = await self.repo.claim_work(guild.id, member.id)
        except Exception:
            log.exception("Could not claim work in guild %s.", guild.id)
            await self._reject(
                interaction, "That could not be processed. Please try again shortly."
            )
            return

        if not result.granted:
            await self._respond(
                interaction,
                "\u23f3 You are still on the clock. Try again in "
                f"**{format_delay(result.retry_after)}**.",
            )
            return

        base = settings.work_amount
        payout = self._rng.randint(max(1, base // 2), max(1, base * 3 // 2))
        balance = await self.repo.credit(guild.id, member.id, payout)

        embed = discord.Embed(
            title="Shift complete",
            description=(
                f"You {self._rng.choice(WORK_JOBS)} and earned "
                f"**{format_money(payout, settings.symbol)}**."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Wallet", value=format_money(balance, settings.symbol), inline=True
        )
        embed.set_footer(
            text=f"You can work again in {format_delay(WORK_COOLDOWN_SECONDS)}."
        )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /crime
    # ------------------------------------------------------------------

    @app_commands.command(
        name="crime", description="Attempt a risky job for a much larger payout."
    )
    @app_commands.guild_only()
    async def crime_cmd(self, interaction: discord.Interaction) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer()


        try:
            result = await self.repo.claim_action(
                guild.id, member.id, "crime", CRIME_COOLDOWN_SECONDS
            )
        except Exception:
            log.exception("Could not claim the crime cooldown in guild %s.", guild.id)
            await self._reject(
                interaction, "That could not be processed. Please try again shortly."
            )
            return

        if not result.granted:
            await self._respond(
                interaction,
                "\u23f3 Things are still too hot. Try again in "
                f"**{format_delay(result.retry_after)}**.",
            )
            return

        base = max(1, settings.work_amount)

        if self._rng.random() < CRIME_SUCCESS_CHANCE:
            payout = self._rng.randint(base, base * 3)
            balance = await self.repo.credit(guild.id, member.id, payout)
            embed = discord.Embed(
                title="The job paid off",
                description=(
                    f"{self._rng.choice(CRIME_SUCCESS_TEXT)}, netting "
                    f"**{format_money(payout, settings.symbol)}**."
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
        else:
            fine = self._rng.randint(max(1, base // 2), base * 2)
            taken = await self.repo.take_up_to(guild.id, member.id, fine)
            account = await self.repo.get_account(guild.id, member.id)
            balance = as_int(account.get("balance"))

            if taken:
                outcome = f"You were fined **{format_money(taken, settings.symbol)}**."
            else:
                # Nothing to seize is still a loss: the cooldown was consumed.
                outcome = (
                    "You had nothing left to seize, so you walked away with a "
                    "warning."
                )

            embed = discord.Embed(
                title="The job fell apart",
                description=f"{self._rng.choice(CRIME_FAILURE_TEXT)}. {outcome}",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )

        embed.add_field(
            name="Wallet", value=format_money(balance, settings.symbol), inline=True
        )
        embed.set_footer(
            text=(
                f"Success chance {int(CRIME_SUCCESS_CHANCE * 100)}% \u2022 next "
                f"attempt in {format_delay(CRIME_COOLDOWN_SECONDS)}."
            )
        )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /rob
    # ------------------------------------------------------------------

    @app_commands.command(
        name="rob", description="Try to steal currency from another member's wallet."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="Whose wallet to target")
    async def rob_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, robber, settings = context

        if member.id == robber.id:
            await self._reject(interaction, "You cannot rob yourself.")
            return
        if member.bot:
            await self._reject(interaction, "Bots do not carry currency.")
            return
        if member.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return

        await interaction.response.defer()

        target_account = await self.repo.get_account(guild.id, member.id)
        target_balance = as_int(target_account.get("balance"))
        if target_balance < ROB_MIN_TARGET_BALANCE:
            await self._reject(
                interaction,
                f"**{sanitize(member.display_name, 40)}** is carrying less than "
                f"{format_money(ROB_MIN_TARGET_BALANCE, settings.symbol)}, so "
                "there is nothing worth taking.",
            )
            return

        own_account = await self.repo.get_account(guild.id, robber.id)
        if as_int(own_account.get("balance")) < ROB_MIN_COLLATERAL:
            await self._reject(
                interaction,
                "You need at least "
                f"{format_money(ROB_MIN_COLLATERAL, settings.symbol)} in your "
                "wallet to cover the fine you are risking.",
            )
            return

        try:
            result = await self.repo.claim_action(
                guild.id, robber.id, "rob", ROB_COOLDOWN_SECONDS
            )
        except Exception:
            log.exception("Could not claim the rob cooldown in guild %s.", guild.id)
            await self._reject(
                interaction, "That could not be processed. Please try again shortly."
            )
            return

        if not result.granted:
            await self._respond(
                interaction,
                "\u23f3 You are still lying low. Try again in "
                f"**{format_delay(result.retry_after)}**.",
            )
            return

        share = self._rng.uniform(ROB_MIN_SHARE, ROB_MAX_SHARE)
        attempt = max(1, int(target_balance * share))

        if self._rng.random() < ROB_SUCCESS_CHANCE:
            stolen = await self.repo.take_up_to(guild.id, member.id, attempt)
            if stolen <= 0:
                await self._respond(
                    interaction,
                    "\u2139\ufe0f Their wallet was emptied before you got there. "
                    "Nothing changed hands.",
                    ephemeral=False,
                )
                return

            balance = await self.repo.credit(guild.id, robber.id, stolen)
            embed = discord.Embed(
                title="Robbery successful",
                description=(
                    f"{self._rng.choice(ROB_SUCCESS_TEXT)}. You took "
                    f"**{format_money(stolen, settings.symbol)}** from "
                    f"{member.mention}."
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
        else:
            fine = max(1, attempt // 2)
            paid = await self.repo.take_up_to(guild.id, robber.id, fine)
            if paid:
                # The fine compensates the intended victim, so a failed attempt
                # is not simply currency destruction.
                await self.repo.credit(guild.id, member.id, paid)

            account = await self.repo.get_account(guild.id, robber.id)
            balance = as_int(account.get("balance"))
            embed = discord.Embed(
                title="Robbery failed",
                description=(
                    f"{self._rng.choice(ROB_FAILURE_TEXT)}. You paid "
                    f"**{format_money(paid, settings.symbol)}** in damages to "
                    f"{member.mention}."
                ),
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )

        embed.add_field(
            name="Your wallet",
            value=format_money(balance, settings.symbol),
            inline=True,
        )
        embed.set_footer(
            text=(
                f"Success chance {int(ROB_SUCCESS_CHANCE * 100)}% \u2022 only wallet "
                "currency can be stolen, never banked currency."
            )
        )

        await self._send_embed(interaction, embed, mentions=NOTICE_MENTIONS)

    # ------------------------------------------------------------------
    # Wager helpers
    # ------------------------------------------------------------------

    async def _take_stake(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        member: discord.Member,
        settings: EconomySettings,
        stake: int,
    ) -> bool:
        """Debits a wager up front. Returns False after replying with the reason.

        Debiting before the outcome is drawn is what makes a crash or a lost
        interaction impossible to exploit: the stake is already gone.
        """
        try:
            await self.repo.debit(guild.id, member.id, stake)
        except InsufficientFundsError:
            account = await self.repo.get_account(guild.id, member.id)
            await self._reject(
                interaction,
                "You only have "
                f"{format_money(account.get('balance'), settings.symbol)} in your "
                "wallet. Withdraw from your bank first if you want to bet more.",
            )
            return False
        except Exception:
            log.exception(
                "Could not take a wager from member %s in guild %s.",
                member.id,
                guild.id,
            )
            await self._reject(
                interaction, "That bet could not be placed. Please try again."
            )
            return False
        return True

    # ------------------------------------------------------------------
    # /gamble
    # ------------------------------------------------------------------

    @app_commands.command(
        name="gamble", description="Bet currency on a coin flip against the house."
    )
    @app_commands.guild_only()
    @app_commands.describe(amount="How much to stake from your wallet")
    @app_commands.checks.cooldown(5, 20.0, key=lambda interaction: interaction.user.id)
    async def gamble_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, MIN_BET, MAX_BET],
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer()

        stake = int(amount)
        if not await self._take_stake(interaction, guild, member, settings, stake):
            return

        won = self._rng.random() < GAMBLE_WIN_CHANCE
        payout = stake * 2 if won else 0
        if payout:
            balance = await self.repo.credit(guild.id, member.id, payout)
        else:
            account = await self.repo.get_account(guild.id, member.id)
            balance = as_int(account.get("balance"))

        net = payout - stake
        embed = discord.Embed(
            title="\U0001fa99 " + ("You won" if won else "You lost"),
            description=(
                f"Stake: **{format_money(stake, settings.symbol)}**\n"
                + (
                    f"The house paid out **{format_money(payout, settings.symbol)}**."
                    if won
                    else "The house keeps your stake."
                )
            ),
            color=discord.Color.green() if won else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Net",
            value=f"{'+' if net >= 0 else '-'}{format_money(abs(net), settings.symbol)}",
            inline=True,
        )
        embed.add_field(
            name="Wallet", value=format_money(balance, settings.symbol), inline=True
        )
        embed.set_footer(
            text=(
                f"Win chance {int(GAMBLE_WIN_CHANCE * 100)}%, paid 2:1. The house "
                "keeps a small edge."
            )
        )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /slots
    # ------------------------------------------------------------------

    @app_commands.command(name="slots", description="Spin the slot machine.")
    @app_commands.guild_only()
    @app_commands.describe(amount="How much to stake from your wallet")
    @app_commands.checks.cooldown(5, 20.0, key=lambda interaction: interaction.user.id)
    async def slots_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, MIN_BET, MAX_BET],
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer()

        stake = int(amount)
        if not await self._take_stake(interaction, guild, member, settings, stake):
            return

        symbols = [entry[0] for entry in SLOT_REELS]
        weights = [entry[1] for entry in SLOT_REELS]
        multipliers = {entry[0]: entry[2] for entry in SLOT_REELS}

        reels = [
            self._rng.choices(symbols, weights=weights, k=1)[0] for _ in range(3)
        ]

        if reels[0] == reels[1] == reels[2]:
            multiplier = float(multipliers[reels[0]])
            outcome = f"Three of a kind at {multiplier:g}x."
        elif len(set(reels)) == 2:
            multiplier = SLOT_PAIR_MULTIPLIER
            outcome = f"A matching pair at {multiplier:g}x."
        else:
            multiplier = 0.0
            outcome = "No matches this time."

        payout = int(stake * multiplier)
        if payout:
            balance = await self.repo.credit(guild.id, member.id, payout)
        else:
            account = await self.repo.get_account(guild.id, member.id)
            balance = as_int(account.get("balance"))

        net = payout - stake
        embed = discord.Embed(
            title="\U0001f3b0 Slots",
            description=f"**{' | '.join(reels)}**\n{outcome}",
            color=(
                discord.Color.green()
                if net > 0
                else discord.Color.orange()
                if net == 0
                else discord.Color.red()
            ),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Stake", value=format_money(stake, settings.symbol), inline=True
        )
        embed.add_field(
            name="Returned", value=format_money(payout, settings.symbol), inline=True
        )
        embed.add_field(
            name="Net",
            value=f"{'+' if net >= 0 else '-'}{format_money(abs(net), settings.symbol)}",
            inline=True,
        )
        embed.add_field(
            name="Wallet", value=format_money(balance, settings.symbol), inline=True
        )
        embed.add_field(
            name="Payouts",
            value=truncate(
                " \u2022 ".join(
                    f"{symbol} {mult}x" for symbol, _, mult in SLOT_REELS
                )
                + f" \u2022 any pair {SLOT_PAIR_MULTIPLIER:g}x"
            ),
            inline=False,
        )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /blackjack
    # ------------------------------------------------------------------

    def blackjack_embed(
        self,
        game: BlackjackGame,
        player: discord.Member,
        settings: EconomySettings,
        *,
        reveal_dealer: bool = False,
        outcome: str | None = None,
        payout: int | None = None,
        balance: int | None = None,
        timed_out: bool = False,
    ) -> discord.Embed:
        """Renders the current or final state of a hand."""
        if outcome is None:
            color = discord.Color.blurple()
        elif payout and payout > game.stake:
            color = discord.Color.green()
        elif payout == game.stake:
            color = discord.Color.orange()
        else:
            color = discord.Color.red()

        embed = discord.Embed(
            title="\U0001f0cf Blackjack",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=player.display_name, icon_url=player.display_avatar.url
        )

        player_total = game.player.total
        embed.add_field(
            name=f"Your hand ({player_total}{' soft' if game.player.is_soft else ''})",
            value=game.player.render(),
            inline=False,
        )

        if reveal_dealer:
            embed.add_field(
                name=f"Dealer ({game.dealer.total})",
                value=game.dealer.render(),
                inline=False,
            )
        else:
            visible = CARD_VALUES[game.dealer.cards[0][0]] if game.dealer.cards else 0
            embed.add_field(
                name=f"Dealer (showing {visible})",
                value=game.dealer.render(hide_after=1),
                inline=False,
            )

        embed.add_field(
            name="Stake",
            value=format_money(game.stake, settings.symbol)
            + (" (doubled)" if game.doubled else ""),
            inline=True,
        )

        if outcome is not None:
            net = (payout or 0) - game.stake
            embed.add_field(
                name="Returned",
                value=format_money(payout or 0, settings.symbol),
                inline=True,
            )
            embed.add_field(
                name="Net",
                value=(
                    f"{'+' if net >= 0 else '-'}"
                    f"{format_money(abs(net), settings.symbol)}"
                ),
                inline=True,
            )
            if balance is not None:
                embed.add_field(
                    name="Wallet",
                    value=format_money(balance, settings.symbol),
                    inline=True,
                )
            embed.description = outcome
            embed.set_footer(
                text=(
                    "The hand timed out and was played as a stand."
                    if timed_out
                    else "Dealer stands on 17. Blackjack pays 3:2."
                )
            )
        else:
            embed.description = (
                "Hit to draw, stand to stop"
                + (", or double down to double your stake." if game.can_double else ".")
            )
            embed.set_footer(
                text=(
                    "Dealer stands on 17 \u2022 the hand settles automatically after "
                    f"{int(BLACKJACK_TIMEOUT)}s of inactivity."
                )
            )

        return embed

    async def settle_blackjack(
        self,
        game: BlackjackGame,
        player: discord.Member,
        settings: EconomySettings,
        *,
        timed_out: bool = False,
    ) -> discord.Embed:
        """Plays the dealer out, credits the payout and returns the final embed."""
        try:
            if not game.player.is_bust:
                # Dealer stands on any 17, soft or hard.
                while game.dealer.total < DEALER_STANDS_ON:
                    game.dealer.add(game.draw())

            outcome, payout = blackjack_payout(game.stake, game.player, game.dealer)

            balance: int | None = None
            if payout > 0:
                try:
                    balance = await self.repo.credit(
                        game.guild_id, game.user_id, payout
                    )
                except Exception:
                    log.exception(
                        "Could not pay out a blackjack hand for member %s in "
                        "guild %s (stake %s, payout %s).",
                        game.user_id,
                        game.guild_id,
                        game.stake,
                        payout,
                    )
                    outcome += (
                        " \u26a0\ufe0f The payout could not be credited; the failure "
                        "has been logged for this server's operator."
                    )
            if balance is None:
                try:
                    account = await self.repo.get_account(
                        game.guild_id, game.user_id
                    )
                    balance = as_int(account.get("balance"))
                except Exception:
                    log.exception("Could not read a wallet after blackjack.")

            game.finished = True
            return self.blackjack_embed(
                game,
                player,
                settings,
                reveal_dealer=True,
                outcome=outcome,
                payout=payout,
                balance=balance,
                timed_out=timed_out,
            )
        finally:
            game.finished = True
            self._in_play.discard((game.guild_id, game.user_id))

    @app_commands.command(
        name="blackjack", description="Play a hand of blackjack against the dealer."
    )
    @app_commands.guild_only()
    @app_commands.describe(amount="How much to stake from your wallet")
    @app_commands.checks.cooldown(4, 30.0, key=lambda interaction: interaction.user.id)
    async def blackjack_cmd(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, MIN_BET, MAX_BET],
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        key = (guild.id, member.id)
        if key in self._in_play:
            await self._reject(
                interaction,
                "You already have a hand in progress. Finish it before starting "
                "another.",
            )
            return

        # Reserved synchronously so a double invocation cannot open two hands.
        self._in_play.add(key)
        released = False

        try:
            await interaction.response.defer()

            stake = int(amount)
            if not await self._take_stake(
                interaction, guild, member, settings, stake
            ):
                released = True
                self._in_play.discard(key)
                return

            deck = build_deck(self._rng)
            game = BlackjackGame(
                guild_id=guild.id,
                user_id=member.id,
                stake=stake,
                deck=deck,
                player=Hand(),
                dealer=Hand(),
            )
            for _ in range(2):
                game.player.add(game.draw())
                game.dealer.add(game.draw())

            # A natural on either side ends the hand before any decision.
            if game.player.is_blackjack or game.dealer.is_blackjack:
                game.resolving = True
                embed = await self.settle_blackjack(game, member, settings)
                released = True
                await interaction.followup.send(
                    embed=embed, allowed_mentions=NO_MENTIONS
                )
                return

            view = BlackjackView(self, game, member, settings)
            message = await interaction.followup.send(
                embed=self.blackjack_embed(game, member, settings),
                view=view,
                allowed_mentions=NO_MENTIONS,
                wait=True,
            )
            # Needed so a timed-out hand can still edit its own message.
            view.message = message
            released = True  # ownership now belongs to the view
        except Exception:
            log.exception("Could not start a blackjack hand in guild %s.", guild.id)
            await self._reject(
                interaction, "That hand could not be started. Please try again."
            )
            raise
        finally:
            if not released:
                self._in_play.discard(key)

    # ------------------------------------------------------------------
    # /leaderboard-economy
    # ------------------------------------------------------------------

    @app_commands.command(
        name="leaderboard-economy",
        description="Show the wealthiest members in this server.",
    )
    @app_commands.guild_only()
    @app_commands.describe(limit="How many members to list (3-25)")
    async def leaderboard_economy_cmd(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, LEADERBOARD_MIN, LEADERBOARD_MAX] = 10,
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, _, settings = context

        await interaction.response.defer()

        rows = await self.repo.leaderboard(guild.id, int(limit))
        rows = [row for row in rows if as_int(row.get("net_worth")) > 0]

        if not rows:
            await self._note(
                interaction, "Nobody has earned anything in this server yet."
            )
            return

        tracked = await self.repo.tracked_members(guild.id)

        lines: list[str] = []
        for index, row in enumerate(rows, start=1):
            user_id = as_int(row.get("user_id"))
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"**{index}.**"
            lines.append(
                f"{prefix} <@{user_id}> \u2014 "
                f"**{format_money(row.get('net_worth'), settings.symbol)}** "
                f"(wallet {format_money(row.get('balance'), settings.symbol)} "
                f"\u2022 bank {format_money(row.get('bank'), settings.symbol)})"
            )

        embed = discord.Embed(
            title=f"\U0001f3c6 Wealth leaderboard: {guild.name}",
            description=truncate("\n".join(lines), 4000),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{tracked} account(s) tracked \u2022 ranked by net worth")
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /shop
    # ------------------------------------------------------------------

    @app_commands.command(name="shop", description="Browse this server's shop.")
    @app_commands.guild_only()
    async def shop_cmd(self, interaction: discord.Interaction) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer()

        try:
            items = await self.repo.list_items(guild.id)
        except Exception:
            log.exception("Could not read the shop for guild %s.", guild.id)
            await self._reject(
                interaction, "The shop could not be read right now. Please try again."
            )
            return

        if not items:
            await self._note(
                interaction,
                "The shop is empty. An administrator can stock it with "
                "`/shop-add`.",
            )
            return

        account = await self.repo.get_account(guild.id, member.id)
        wallet = as_int(account.get("balance"))
        inventory = load_inventory(account.get("inventory"))

        embed = discord.Embed(
            title=f"\U0001f6d2 Shop: {guild.name}",
            description=(
                f"Your wallet holds **{format_money(wallet, settings.symbol)}**. "
                "Buy something with `/buy`."
            ),
            color=discord.Color.blurple(),
        )

        for item in items:
            key = str(item.get("item_key"))
            price = as_int(item.get("price"))
            stock = item.get("stock")
            limit = item.get("max_per_user")
            role_id = item.get("role_id")

            details = [format_money(price, settings.symbol)]
            details.append(
                "unlimited stock" if stock is None else f"{as_int(stock)} in stock"
            )
            if limit:
                owned = owned_quantity(inventory, key)
                details.append(f"limit {as_int(limit)} each (you own {owned})")
            if role_id:
                role = guild.get_role(as_int(role_id))
                details.append(
                    f"grants {role.mention}" if role is not None else "grants a deleted role"
                )

            body = " \u2022 ".join(details)
            description = item.get("description")
            if description:
                body += "\n" + sanitize(description, MAX_ITEM_DESCRIPTION)
            body += f"\nBuy with `/buy item:{key}`"

            embed.add_field(
                name=sanitize(item.get("name"), MAX_ITEM_NAME),
                value=truncate(body),
                inline=False,
            )

        embed.set_footer(
            text=f"{len(items)} item(s) \u2022 prices are paid from your wallet"
        )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # /buy
    # ------------------------------------------------------------------

    @app_commands.command(name="buy", description="Buy an item from the shop.")
    @app_commands.guild_only()
    @app_commands.describe(
        item="The item to buy", quantity="How many to buy (1-10)"
    )
    async def buy_cmd(
        self,
        interaction: discord.Interaction,
        item: app_commands.Range[str, 1, MAX_ITEM_NAME],
        quantity: app_commands.Range[int, 1, MAX_PURCHASE_QUANTITY] = 1,
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, member, settings = context

        await interaction.response.defer(ephemeral=True)

        key = slugify_key(item)
        if not key:
            await self._reject(
                interaction,
                "That is not a usable item name. Pick one from `/shop`.",
            )
            return

        try:
            record = await self.repo.get_item(guild.id, key)
        except Exception:
            log.exception("Could not read a shop item in guild %s.", guild.id)
            await self._reject(
                interaction, "The shop could not be read right now. Please try again."
            )
            return

        if record is None or not record.get("enabled"):
            await self._reject(
                interaction,
                f"There is no item called `{sanitize(key, MAX_ITEM_KEY)}` in this "
                "server's shop. Run `/shop` to see what is available.",
            )
            return

        count = int(quantity)
        name = str(record.get("name") or key)
        price = as_int(record.get("price"))
        total = price * count
        item_id = as_int(record.get("item_id"))

        limit = record.get("max_per_user")
        if limit:
            inventory = await self.repo.get_inventory(guild.id, member.id)
            owned = owned_quantity(inventory, key)
            if owned + count > as_int(limit):
                await self._reject(
                    interaction,
                    f"You may only own {as_int(limit)} of **{sanitize(name, 40)}** "
                    f"and you already have {owned}.",
                )
                return

        role: discord.Role | None = None
        role_id = record.get("role_id")
        if role_id:
            role = guild.get_role(as_int(role_id))
            if role is None:
                await self._reject(
                    interaction,
                    "That item grants a role which no longer exists. Ask an "
                    "administrator to fix the shop entry.",
                )
                return

            me = guild.me
            if (
                me is None
                or not me.guild_permissions.manage_roles
                or role.managed
                or role.is_default()
                or me.top_role <= role
            ):
                # Refusing here is better than taking payment for a role that
                # Discord would never let Fyrion assign.
                await self._reject(
                    interaction,
                    f"I cannot assign {role.mention}, so this item cannot be sold. "
                    "Ask an administrator to move my role above it.",
                )
                return

        stock_claimed = False
        if record.get("stock") is not None:
            try:
                stock_claimed = await self.repo.claim_stock(item_id, count)
            except Exception:
                log.exception("Could not reserve shop stock in guild %s.", guild.id)
                await self._reject(
                    interaction, "That purchase could not be processed. Try again."
                )
                return

            if not stock_claimed:
                await self._reject(
                    interaction,
                    f"**{sanitize(name, 40)}** does not have {count} left in stock.",
                )
                return

        try:
            await self.repo.debit(guild.id, member.id, total)
        except InsufficientFundsError:
            if stock_claimed:
                await self.repo.release_stock(item_id, count)
            account = await self.repo.get_account(guild.id, member.id)
            await self._reject(
                interaction,
                f"That costs {format_money(total, settings.symbol)} but you only "
                f"have {format_money(account.get('balance'), settings.symbol)} in "
                "your wallet.",
            )
            return
        except Exception:
            if stock_claimed:
                await self.repo.release_stock(item_id, count)
            log.exception(
                "Could not take payment for a shop purchase in guild %s.", guild.id
            )
            await self._reject(
                interaction, "That purchase could not be processed. Try again."
            )
            return

        stored, error = await self.repo.add_to_inventory(
            guild.id, member.id, key=key, name=name, quantity=count
        )
        if not stored:
            # Nothing was delivered, so the payment is reversed and the stock
            # released.
            await self.repo.credit(guild.id, member.id, total)
            if stock_claimed:
                await self.repo.release_stock(item_id, count)
            await self._reject(
                interaction, error or "That item could not be added to your inventory."
            )
            return

        role_note = ""
        if role is not None:
            if any(existing.id == role.id for existing in member.roles):
                role_note = f"\nYou already had {role.mention}."
            else:
                try:
                    await member.add_roles(
                        role, reason=f"Purchased shop item {key}"
                    )
                    role_note = f"\n{role.mention} has been added to your roles."
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning(
                        "Could not grant the purchased role %s in guild %s: %s",
                        role.id,
                        guild.id,
                        exc,
                    )
                    role_note = (
                        f"\n\u26a0\ufe0f Discord refused to add {role.mention}; the "
                        "item is in your inventory, so ask staff to grant it "
                        "manually."
                    )

        account = await self.repo.get_account(guild.id, member.id)
        await self._respond(
            interaction,
            f"\u2705 Bought **{count}x {sanitize(name, 40)}** for "
            f"**{format_money(total, settings.symbol)}**.\n"
            f"Wallet: {format_money(account.get('balance'), settings.symbol)}"
            f"{role_note}",
        )

    @buy_cmd.autocomplete("item")
    async def _buy_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._item_choices(interaction, current, enabled_only=True)

    async def _item_choices(
        self,
        interaction: discord.Interaction,
        current: str,
        *,
        enabled_only: bool,
    ) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild_id
        if guild_id is None:
            return []

        try:
            items = await self.repo.list_items(
                guild_id, enabled_only=enabled_only, limit=SHOP_PAGE_LIMIT
            )
            settings = await self.settings_for(guild_id)
        except Exception:
            log.exception("Could not autocomplete shop items for guild %s.", guild_id)
            return []

        needle = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for item in items:
            key = str(item.get("item_key"))
            name = str(item.get("name") or key)
            label = (
                f"{name} \u2014 "
                f"{format_money(item.get('price'), settings.symbol)}"
            )[:100]
            if needle and needle not in label.lower() and needle not in key:
                continue
            choices.append(app_commands.Choice(name=label, value=key))
            if len(choices) >= 25:
                break
        return choices

    # ------------------------------------------------------------------
    # /inventory
    # ------------------------------------------------------------------

    @app_commands.command(
        name="inventory", description="View the items a member owns."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to inspect (defaults to you)")
    async def inventory_cmd(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        context = await self._ready(interaction)
        if context is None:
            return
        guild, invoker, settings = context

        target = member or invoker
        if target.bot:
            await self._reject(interaction, "Bots do not own items.")
            return
        if target.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return

        await interaction.response.defer()

        account = await self.repo.get_account(guild.id, target.id)
        items = load_inventory(account.get("inventory"))

        if not items:
            await self._note(
                interaction,
                (
                    "You do not own any items yet. Browse `/shop` to buy some."
                    if target.id == invoker.id
                    else f"**{sanitize(target.display_name, 40)}** does not own any items."
                ),
            )
            return

        catalogue: dict[str, dict[str, Any]] = {}
        try:
            for record in await self.repo.list_items(guild.id, enabled_only=False):
                catalogue[str(record.get("item_key"))] = record
        except Exception:
            log.exception("Could not read the shop while listing an inventory.")

        lines: list[str] = []
        for item in sorted(items, key=lambda entry: str(entry.get("name")).lower()):
            key = str(item.get("key"))
            quantity = as_int(item.get("quantity"), 1)
            detail = f"**{quantity}x** {sanitize(item.get('name'), 60)}"

            record = catalogue.get(key)
            if record is not None:
                detail += (
                    " \u2022 worth "
                    + format_money(as_int(record.get("price")) * quantity, settings.symbol)
                )
                if not record.get("enabled"):
                    detail += " \u2022 no longer sold"
            else:
                detail += " \u2022 no longer in the shop"

            acquired = parse_iso(item.get("acquired_at"))
            if acquired is not None:
                detail += (
                    " \u2022 " + discord.utils.format_dt(acquired, style="R")
                )
            lines.append(detail)

        embed = discord.Embed(
            title=f"\U0001f392 Inventory: {target.display_name}",
            description=truncate("\n".join(lines), 4000),
            color=target.color if target.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(
            text=(
                f"{len(items)} distinct item(s) \u2022 "
                f"{sum(as_int(entry.get('quantity'), 1) for entry in items)} total"
            )
        )

        await self._send_embed(interaction, embed)

    # ------------------------------------------------------------------
    # Administration
    # ------------------------------------------------------------------

    @app_commands.command(
        name="economy-toggle",
        description="Enable or disable the economy and tune its rewards.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="True enables every economy command, False disables them",
        currency_symbol="Symbol shown before amounts (default $)",
        daily_amount="Base /daily reward before the streak bonus",
        work_amount="Base /work reward; /crime scales from this too",
    )
    async def economy_toggle_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        currency_symbol: Optional[app_commands.Range[str, 1, 8]] = None,
        daily_amount: Optional[app_commands.Range[int, 0, 1_000_000]] = None,
        work_amount: Optional[app_commands.Range[int, 0, 1_000_000]] = None,
    ) -> None:
        context = await self._authorize(interaction)
        if context is None:
            return
        guild, member = context

        values: dict[str, Any] = {"economy_enabled": int(bool(enabled))}
        if currency_symbol is not None:
            symbol = currency_symbol.strip()
            if not symbol:
                await self._reject(
                    interaction, "The currency symbol cannot be blank."
                )
                return
            if any(char.isspace() for char in symbol):
                await self._reject(
                    interaction, "The currency symbol cannot contain spaces."
                )
                return
            values["economy_currency_symbol"] = symbol
        if daily_amount is not None:
            values["economy_daily_amount"] = int(daily_amount)
        if work_amount is not None:
            values["economy_work_amount"] = int(work_amount)

        await interaction.response.defer(ephemeral=True)

        try:
            await self.repo.update_settings(guild.id, **values)
        except Exception:
            log.exception("Could not update economy settings for guild %s.", guild.id)
            await self._reject(
                interaction, "Those settings could not be saved. Please try again."
            )
            return

        self.invalidate(guild.id)
        settings = await self.settings_for(guild.id)
        state = "enabled" if settings.enabled else "disabled"

        await self._audit(
            guild,
            member,
            "Settings changed",
            f"Economy: {state}\nSymbol: {settings.symbol}\n"
            f"Daily: {settings.daily_amount}\nWork: {settings.work_amount}",
        )

        lines = [
            f"\u2705 The economy is now **{state}**.",
            f"Symbol: `{settings.symbol}` \u2022 daily: "
            f"**{format_money(settings.daily_amount, settings.symbol)}** "
            f"\u2022 work: **{format_money(settings.work_amount, settings.symbol)}**",
        ]
        if settings.enabled:
            lines.append(
                "Stock the shop with `/shop-add` so members have something to "
                "spend on."
            )
        else:
            lines.append("Existing balances and inventories are kept untouched.")

        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(
        name="shop-add", description="Add or update an item in this server's shop."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        name="Display name of the item",
        price="Cost in this server's currency",
        description="What the item is or does",
        role="Role granted when the item is bought",
        stock="Units available (leave empty for unlimited)",
        max_per_user="How many one member may own",
        key="Optional lookup key (defaults to a slug of the name)",
    )
    async def shop_add_cmd(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_ITEM_NAME],
        price: app_commands.Range[int, 0, MAX_TRANSFER],
        description: Optional[app_commands.Range[str, 1, MAX_ITEM_DESCRIPTION]] = None,
        role: Optional[discord.Role] = None,
        stock: Optional[app_commands.Range[int, 0, 100_000]] = None,
        max_per_user: Optional[app_commands.Range[int, 1, 99]] = None,
        key: Optional[app_commands.Range[str, 1, MAX_ITEM_KEY]] = None,
    ) -> None:
        context = await self._authorize(interaction)
        if context is None:
            return
        guild, member = context

        item_key = slugify_key(key if key is not None else name)
        if not _KEY_VALID.match(item_key):
            await self._reject(
                interaction,
                "The item key must start with a letter or digit and may only "
                "contain letters, digits, hyphens and underscores, up to "
                f"{MAX_ITEM_KEY} characters.",
            )
            return

        if role is not None:
            if role.guild.id != guild.id:
                await self._reject(
                    interaction, "That role does not belong to this server."
                )
                return
            # A role Fyrion cannot assign would take payment and deliver
            # nothing, so it is refused at configuration time.
            if error := can_manage_role(member, role):
                await self._reject(interaction, error)
                return

        await interaction.response.defer(ephemeral=True)


        try:
            existing = await self.repo.get_item(guild.id, item_key)
            if existing is None and await self.repo.count_items(guild.id) >= MAX_SHOP_ITEMS:
                await self._reject(
                    interaction,
                    f"The shop already holds {MAX_SHOP_ITEMS} items, which is the "
                    "maximum. Remove one first.",
                )
                return

            saved = await self.repo.save_item(
                guild.id,
                key=item_key,
                name=name.strip(),
                price=int(price),
                description=description.strip() if description else None,
                role_id=role.id if role is not None else None,
                stock=int(stock) if stock is not None else None,
                max_per_user=int(max_per_user) if max_per_user else None,
                created_by=member.id,
            )
        except Exception:
            log.exception("Could not save a shop item in guild %s.", guild.id)
            await self._reject(
                interaction, "That item could not be saved. Please try again."
            )
            return

        settings = await self.settings_for(guild.id)
        verb = "Updated" if existing is not None else "Added"

        await self._audit(
            guild,
            member,
            f"Shop item {verb.lower()}",
            f"Key: {item_key}\nName: {saved.get('name')}\nPrice: {saved.get('price')}\n"
            + (f"Role: {role.name} (`{role.id}`)\n" if role is not None else "")
            + (
                "Stock: unlimited"
                if saved.get("stock") is None
                else f"Stock: {as_int(saved.get('stock'))}"
            ),
        )

        lines = [
            f"\u2705 {verb} **{sanitize(saved.get('name'), 40)}** at "
            f"**{format_money(saved.get('price'), settings.symbol)}**.",
            f"Members buy it with `/buy item:{item_key}`.",
        ]
        if saved.get("stock") is None:
            lines.append("Stock is unlimited.")
        else:
            lines.append(f"{as_int(saved.get('stock'))} unit(s) in stock.")
        if max_per_user:
            lines.append(f"Each member may own {int(max_per_user)}.")
        if role is not None:
            lines.append(f"Buying it grants {role.mention}.")
        if not settings.enabled:
            lines.append(
                "\u26a0\ufe0f The economy is disabled, so nobody can buy anything "
                "yet. Enable it with `/economy-toggle`."
            )

        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(
        name="shop-remove", description="Remove an item from this server's shop."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(item="The item to remove")
    async def shop_remove_cmd(
        self,
        interaction: discord.Interaction,
        item: app_commands.Range[str, 1, MAX_ITEM_NAME],
    ) -> None:
        context = await self._authorize(interaction)
        if context is None:
            return
        guild, member = context

        await interaction.response.defer(ephemeral=True)

        item_key = slugify_key(item)
        if not item_key:
            await self._reject(interaction, "That is not a usable item key.")
            return

        try:
            removed = await self.repo.delete_item(guild.id, item_key)
        except Exception:
            log.exception("Could not remove a shop item in guild %s.", guild.id)
            await self._reject(
                interaction, "That item could not be removed. Please try again."
            )
            return

        if not removed:
            await self._note(
                interaction,
                f"There is no item called `{sanitize(item_key, MAX_ITEM_KEY)}` in "
                "this server's shop.",
            )
            return

        await self._audit(
            guild, member, "Shop item removed", f"Key: {item_key}"
        )
        await self._respond(
            interaction,
            f"\u2705 Removed `{sanitize(item_key, MAX_ITEM_KEY)}` from the shop. "
            "Items members already own are kept in their inventories, and roles "
            "they were granted are untouched.",
        )

    @shop_remove_cmd.autocomplete("item")
    async def _shop_remove_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._item_choices(interaction, current, enabled_only=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))


__all__ = [
    "Economy",
    "EconomyRepository",
    "EconomySettings",
    "BlackjackGame",
    "BlackjackView",
    "ClaimResult",
    "Hand",
    "MAX_BET",
    "MIN_BET",
    "MAX_TRANSFER",
    "SLOT_REELS",
    "blackjack_payout",
    "build_deck",
    "dump_inventory",
    "format_delay",
    "format_money",
    "load_inventory",
    "owned_quantity",
    "slugify_key",
    "setup",
]
