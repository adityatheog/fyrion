"""
Tests for the reputation path of the leveling repository.

give_rep claims the giver's cooldown and credits the recipient inside a single
transaction, so the two can never diverge (a burned cooldown with no point, or
a point with no cooldown).
"""
import pytest

from fyrion.database.repositories.leveling import LevelingRepository


@pytest.mark.asyncio
async def test_give_rep_grants_and_credits(db_manager):
    LevelingRepository.reset_schema_flag()
    repo = LevelingRepository(db_manager)

    outcome = await repo.give_rep(1, giver_id=10, target_id=20, cooldown_seconds=3600)
    assert outcome.granted is True
    assert outcome.total == 1
    assert await repo.get_rep(1, 20) == 1


@pytest.mark.asyncio
async def test_give_rep_respects_cooldown(db_manager):
    LevelingRepository.reset_schema_flag()
    repo = LevelingRepository(db_manager)

    first = await repo.give_rep(1, giver_id=10, target_id=20, cooldown_seconds=3600)
    assert first.granted is True

    # Same giver, still on cooldown: refused, and no extra point is credited.
    second = await repo.give_rep(1, giver_id=10, target_id=20, cooldown_seconds=3600)
    assert second.granted is False
    assert second.retry_after > 0
    assert await repo.get_rep(1, 20) == 1


@pytest.mark.asyncio
async def test_give_rep_from_distinct_givers_accumulates(db_manager):
    LevelingRepository.reset_schema_flag()
    repo = LevelingRepository(db_manager)

    await repo.give_rep(1, giver_id=10, target_id=20, cooldown_seconds=3600)
    await repo.give_rep(1, giver_id=11, target_id=20, cooldown_seconds=3600)

    assert await repo.get_rep(1, 20) == 2
