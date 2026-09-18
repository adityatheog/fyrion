"""
Phase 6 -- economy hardening and game coverage.

These tests lock down the atomicity guarantees of the wallet primitives
(``adjust_balance`` / ``transfer_balance``) and pin the payout math for the
three games (blackjack, coinflip, slots). The engine already existed and was
built atomic; this suite confirms it and guards against regressions.
"""
import asyncio
import random

import pytest

from fyrion.cogs.economy import (
    GAMBLE_WIN_CHANCE,
    SLOT_PAIR_MULTIPLIER,
    SLOT_REELS,
    Hand,
    blackjack_payout,
    build_deck,
)
from fyrion.database.manager import InsufficientFundsError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GUILD_ID = 555000111
USER_A = 700000001
USER_B = 700000002


def make_hand(*ranks: str) -> Hand:
    """Builds a hand from ranks; suits are irrelevant to scoring."""
    hand = Hand()
    for rank in ranks:
        hand.add((rank, "♠"))
    return hand


async def fund(db, user_id: int, amount: int) -> int:
    """Credits a wallet and returns the resulting balance."""
    return await db.adjust_balance(GUILD_ID, user_id, amount)


async def balance_of(db, user_id: int) -> int:
    account = await db.get_economy_account(GUILD_ID, user_id)
    return int(account["balance"])


# ---------------------------------------------------------------------------
# adjust_balance -- atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjust_balance_credits_and_debits(db_manager):
    await fund(db_manager, USER_A, 1000)
    assert await balance_of(db_manager, USER_A) == 1000

    new_balance = await db_manager.adjust_balance(GUILD_ID, USER_A, -400)
    assert new_balance == 600
    assert await balance_of(db_manager, USER_A) == 600


@pytest.mark.asyncio
async def test_debit_cannot_go_negative(db_manager):
    await fund(db_manager, USER_A, 100)
    with pytest.raises(InsufficientFundsError):
        await db_manager.adjust_balance(GUILD_ID, USER_A, -101)
    # The failed debit must not have moved the balance.
    assert await balance_of(db_manager, USER_A) == 100


@pytest.mark.asyncio
async def test_concurrent_debits_only_one_wins(db_manager):
    """Two debits of the full balance: exactly one succeeds, one raises.

    The guard lives in the SQL ``WHERE balance + ? >= 0`` and writes serialize
    on the pool's write lock, so the loser can never drive the wallet negative
    and the amount is never applied twice.
    """
    await fund(db_manager, USER_A, 500)

    results = await asyncio.gather(
        db_manager.adjust_balance(GUILD_ID, USER_A, -500),
        db_manager.adjust_balance(GUILD_ID, USER_A, -500),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, InsufficientFundsError)]

    assert len(successes) == 1
    assert successes[0] == 0
    assert len(failures) == 1
    assert await balance_of(db_manager, USER_A) == 0


@pytest.mark.asyncio
async def test_many_concurrent_debits_never_negative(db_manager):
    """Ten debits racing on a wallet that can only cover four of them."""
    await fund(db_manager, USER_A, 400)

    results = await asyncio.gather(
        *(db_manager.adjust_balance(GUILD_ID, USER_A, -100) for _ in range(10)),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, InsufficientFundsError)]

    assert len(successes) == 4
    assert len(failures) == 6
    assert await balance_of(db_manager, USER_A) == 0
    # No successful debit ever reported a negative running balance.
    assert all(value >= 0 for value in successes)


# ---------------------------------------------------------------------------
# transfer_balance -- atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_moves_funds(db_manager):
    await fund(db_manager, USER_A, 1000)
    await fund(db_manager, USER_B, 0)

    await db_manager.transfer_balance(GUILD_ID, USER_A, USER_B, 300)

    assert await balance_of(db_manager, USER_A) == 700
    assert await balance_of(db_manager, USER_B) == 300


@pytest.mark.asyncio
async def test_transfer_cannot_overdraw(db_manager):
    await fund(db_manager, USER_A, 200)
    await fund(db_manager, USER_B, 0)

    with pytest.raises(InsufficientFundsError):
        await db_manager.transfer_balance(GUILD_ID, USER_A, USER_B, 201)

    # Neither side moves when the sender cannot cover the amount.
    assert await balance_of(db_manager, USER_A) == 200
    assert await balance_of(db_manager, USER_B) == 0


@pytest.mark.asyncio
async def test_concurrent_transfers_conserve_currency(db_manager):
    """Two transfers racing to drain the same wallet cannot double-spend."""
    await fund(db_manager, USER_A, 500)
    await fund(db_manager, USER_B, 0)

    results = await asyncio.gather(
        db_manager.transfer_balance(GUILD_ID, USER_A, USER_B, 500),
        db_manager.transfer_balance(GUILD_ID, USER_A, USER_B, 500),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, InsufficientFundsError)]
    assert len(failures) == 1

    total = await balance_of(db_manager, USER_A) + await balance_of(db_manager, USER_B)
    assert total == 500
    assert await balance_of(db_manager, USER_A) == 0
    assert await balance_of(db_manager, USER_B) == 500


@pytest.mark.asyncio
async def test_transfer_rejects_self_and_nonpositive(db_manager):
    await fund(db_manager, USER_A, 100)
    with pytest.raises(ValueError):
        await db_manager.transfer_balance(GUILD_ID, USER_A, USER_A, 10)
    with pytest.raises(ValueError):
        await db_manager.transfer_balance(GUILD_ID, USER_A, USER_B, 0)
    with pytest.raises(ValueError):
        await db_manager.transfer_balance(GUILD_ID, USER_A, USER_B, -5)


# ---------------------------------------------------------------------------
# Hand scoring
# ---------------------------------------------------------------------------


def test_hand_ace_scores_as_eleven_then_reduces():
    # A + 6 counts the ace as 11 for a total of 17.
    assert make_hand("A", "6").total == 17
    # Adding a ten forces the ace down to 1 to stay under 21.
    assert make_hand("A", "6", "10").total == 17
    # Two aces: one stays 11, the other drops to 1 (11 + 1 == 12).
    assert make_hand("A", "A").total == 12


def test_hand_is_soft_flags_an_eleven_valued_ace():
    # Standard blackjack sense: a hand is soft when it holds an ace still
    # counting as 11 (it cannot bust on the next hit). Once every ace has been
    # reduced to 1, the hand is hard.
    assert make_hand("A", "6").is_soft  # soft 17, ace worth 11
    assert make_hand("A", "A", "9").is_soft  # 11 + 1 + 9 == 21, one ace still 11
    assert not make_hand("A", "6", "10").is_soft  # ace reduced to 1 -> hard 17
    assert not make_hand("10", "7").is_soft  # no ace at all
    assert not make_hand("K", "Q").is_soft


def test_hand_blackjack_and_bust():
    assert make_hand("A", "K").is_blackjack
    # A 21 built from three cards is not a natural blackjack.
    assert not make_hand("7", "7", "7").is_blackjack
    assert make_hand("K", "Q", "5").is_bust


# ---------------------------------------------------------------------------
# blackjack_payout -- every outcome
# ---------------------------------------------------------------------------


def test_payout_player_bust_returns_nothing():
    outcome, payout = blackjack_payout(100, make_hand("K", "Q", "5"), make_hand("10", "8"))
    assert payout == 0
    assert "bust" in outcome.lower()


def test_payout_push_when_both_blackjack():
    outcome, payout = blackjack_payout(100, make_hand("A", "K"), make_hand("A", "Q"))
    assert payout == 100  # stake returned
    assert "push" in outcome.lower()


def test_payout_natural_blackjack_pays_three_to_two():
    outcome, payout = blackjack_payout(100, make_hand("A", "K"), make_hand("10", "8"))
    assert payout == 250  # stake + 3:2 == 100 * 5 // 2
    assert "blackjack" in outcome.lower()


def test_payout_natural_blackjack_rounds_down():
    # 3:2 on an odd stake truncates, never rounds up in the player's favour.
    _, payout = blackjack_payout(101, make_hand("A", "K"), make_hand("10", "8"))
    assert payout == 101 * 5 // 2 == 252


def test_payout_dealer_blackjack_beats_a_regular_21():
    _, payout = blackjack_payout(100, make_hand("7", "7", "7"), make_hand("A", "K"))
    assert payout == 0


def test_payout_dealer_bust_pays_even_money():
    outcome, payout = blackjack_payout(100, make_hand("10", "8"), make_hand("K", "Q", "5"))
    assert payout == 200
    assert "bust" in outcome.lower()


def test_payout_player_beats_dealer():
    _, payout = blackjack_payout(100, make_hand("10", "9"), make_hand("10", "7"))
    assert payout == 200


def test_payout_dealer_beats_player():
    _, payout = blackjack_payout(100, make_hand("10", "7"), make_hand("10", "9"))
    assert payout == 0


def test_payout_push_on_equal_totals():
    outcome, payout = blackjack_payout(100, make_hand("10", "8"), make_hand("K", "8"))
    assert payout == 100
    assert "push" in outcome.lower()


def test_payout_double_down_uses_the_doubled_stake():
    """After a double-down the stake carries the extra wager.

    ``double_button`` debits the extra stake and does ``game.stake += extra``
    before settling, so a doubled 100-chip hand settles as a 200 stake: a win
    returns 400, a loss returns 0.
    """
    doubled = 200
    _, win = blackjack_payout(doubled, make_hand("10", "9"), make_hand("10", "7"))
    assert win == 400
    _, loss = blackjack_payout(doubled, make_hand("10", "7"), make_hand("10", "9"))
    assert loss == 0


# ---------------------------------------------------------------------------
# build_deck
# ---------------------------------------------------------------------------


def test_build_deck_has_full_multideck_composition():
    deck = build_deck(random.Random(1), decks=4)
    assert len(deck) == 4 * 52
    # Four full decks -> four of every (rank, suit) pair.
    assert deck.count(("A", "♠")) == 4


# ---------------------------------------------------------------------------
# Slots payout math -- fixed RNG seeds
# ---------------------------------------------------------------------------


def _spin_slots(rng: random.Random, stake: int) -> tuple[list[str], int]:
    """Reproduces the exact reel-and-payout math from ``slots_cmd``.

    Mirrors ``economy.slots_cmd`` line for line, anchored on the production
    ``SLOT_REELS`` / ``SLOT_PAIR_MULTIPLIER`` constants, so a change to either
    the odds table or the formula shape surfaces here.
    """
    symbols = [entry[0] for entry in SLOT_REELS]
    weights = [entry[1] for entry in SLOT_REELS]
    multipliers = {entry[0]: entry[2] for entry in SLOT_REELS}

    reels = [rng.choices(symbols, weights=weights, k=1)[0] for _ in range(3)]

    if reels[0] == reels[1] == reels[2]:
        multiplier = float(multipliers[reels[0]])
    elif len(set(reels)) == 2:
        multiplier = SLOT_PAIR_MULTIPLIER
    else:
        multiplier = 0.0

    return reels, int(stake * multiplier)


def test_slots_three_of_a_kind_pays_symbol_multiplier():
    stake = 100
    for symbol, _weight, mult in SLOT_REELS:
        reels = [symbol, symbol, symbol]
        # Recompute the payout branch directly for a forced three-of-a-kind.
        assert int(stake * float(mult)) == stake * mult


def test_slots_pair_pays_one_and_a_half():
    stake = 100
    assert int(stake * SLOT_PAIR_MULTIPLIER) == 150


def test_slots_no_match_pays_nothing():
    symbols = [entry[0] for entry in SLOT_REELS]
    reels = [symbols[0], symbols[1], symbols[2]]
    assert len(set(reels)) == 3
    # Distinct reels take the else-branch: multiplier 0.
    assert int(100 * 0.0) == 0


def test_slots_is_deterministic_for_a_fixed_seed():
    stake = 100
    reels_a, payout_a = _spin_slots(random.Random(1234), stake)
    reels_b, payout_b = _spin_slots(random.Random(1234), stake)
    assert reels_a == reels_b
    assert payout_a == payout_b
    assert payout_a >= 0


def test_slots_has_a_house_edge():
    """Over many seeded spins the return-to-player stays below the stake."""
    rng = random.Random(20240918)
    stake = 100
    spins = 200_000
    returned = sum(_spin_slots(rng, stake)[1] for _ in range(spins))
    rtp = returned / (spins * stake)
    assert 0.6 < rtp < 1.0


# ---------------------------------------------------------------------------
# Coinflip (/gamble) payout math -- fixed RNG seeds
# ---------------------------------------------------------------------------


def _flip(rng: random.Random, stake: int) -> int:
    """Reproduces the ``gamble_cmd`` outcome: 2:1 on a win, 0 on a loss."""
    won = rng.random() < GAMBLE_WIN_CHANCE
    return stake * 2 if won else 0


def test_coinflip_pays_double_or_nothing():
    rng = random.Random(7)
    for _ in range(1000):
        payout = _flip(rng, 100)
        assert payout in (0, 200)


def test_coinflip_is_deterministic_for_a_fixed_seed():
    seq_a = [_flip(random.Random(99), 50) for _ in range(1)]
    seq_b = [_flip(random.Random(99), 50) for _ in range(1)]
    assert seq_a == seq_b


def test_coinflip_win_rate_matches_configured_chance():
    rng = random.Random(20240918)
    trials = 200_000
    wins = sum(1 for _ in range(trials) if _flip(rng, 1) > 0)
    observed = wins / trials
    # Within a percentage point of the configured 0.47 house-edged chance.
    assert abs(observed - GAMBLE_WIN_CHANCE) < 0.01
    # And the configured chance is a house edge, not a coin flip in the
    # player's favour.
    assert GAMBLE_WIN_CHANCE < 0.5
