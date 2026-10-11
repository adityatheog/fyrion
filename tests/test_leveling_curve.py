"""
Tests for the pure leveling curve helpers.

These need no database: they pin the quadratic XP curve and the clamping
behaviour that keeps a corrupt XP value from becoming an unbounded loop.
"""

import pytest

from fyrion.database.repositories.leveling import (
    MAX_LEVEL,
    level_from_xp,
    level_progress,
    total_xp_for_level,
    xp_to_next_level,
)


@pytest.mark.parametrize(
    "level, expected",
    [(0, 100), (1, 155), (2, 220), (10, 1100), (-3, 100)],
)
def test_xp_to_next_level_follows_the_quadratic_curve(level, expected):
    assert xp_to_next_level(level) == expected


@pytest.mark.parametrize(
    "level, expected",
    [(0, 0), (1, 100), (2, 255), (3, 475), (10, 4675), (-1, 0)],
)
def test_total_xp_for_level_is_cumulative(level, expected):
    assert total_xp_for_level(level) == expected


def test_total_xp_is_strictly_increasing():
    totals = [total_xp_for_level(level) for level in range(0, 60)]
    assert all(later > earlier for earlier, later in zip(totals, totals[1:]))


def test_total_xp_clamps_to_the_level_cap():
    assert total_xp_for_level(MAX_LEVEL + 1000) == total_xp_for_level(MAX_LEVEL)


@pytest.mark.parametrize(
    "xp, expected",
    [(0, 0), (99, 0), (100, 1), (254, 1), (255, 2), (256, 2), (-10, 0)],
)
def test_level_from_xp_boundaries(xp, expected):
    assert level_from_xp(xp) == expected


def test_level_from_xp_round_trips_every_threshold():
    for level in range(0, 80):
        threshold = total_xp_for_level(level)
        assert level_from_xp(threshold) == level
        if level > 0:
            assert level_from_xp(threshold - 1) == level - 1


def test_level_from_xp_never_exceeds_the_cap():
    assert level_from_xp(10**12) == MAX_LEVEL


@pytest.mark.parametrize(
    "xp, expected",
    [(0, (0, 0, 100)), (150, (1, 50, 155)), (255, (2, 0, 220))],
)
def test_level_progress_reports_position_inside_the_level(xp, expected):
    assert level_progress(xp) == expected


def test_level_progress_at_the_cap_has_no_next_level():
    assert level_progress(10**12) == (MAX_LEVEL, 0, 0)


def test_level_progress_one_below_the_cap_has_a_final_step():
    level, into, required = level_progress(total_xp_for_level(MAX_LEVEL) - 1)
    assert level == MAX_LEVEL - 1
    assert required > 0
    assert into == required - 1
