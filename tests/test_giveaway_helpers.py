"""
Tests for the pure helpers in the giveaways cog: duration parsing, countdown
formatting, refresh cadence, stored-value parsing and the weighted draw.
"""

import random

import pytest

from fyrion.cogs.giveaways import (
    FINAL_REFRESH_SECONDS,
    MAX_DURATION_SECONDS,
    MESSAGE_LINK_RE,
    MIN_DURATION_SECONDS,
    _weighted_index,
    format_delta,
    parse_duration,
    parse_iso,
    refresh_interval,
    winner_ids,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("30s", 30),
        ("45m", 2700),
        ("2h30m", 9000),
        ("3d", 259200),
        ("1w", 604800),
        ("2H", 7200),
        ("1h1h", 7200),
        ("  1 h ", 3600),
        ("60d", MAX_DURATION_SECONDS),
    ],
)
def test_parse_duration_accepts_valid_values(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw, expected", [("10", 600), ("30", 1800)])
def test_parse_duration_reads_a_bare_number_as_minutes(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "1h30", "h", "1 2 x y"])
def test_parse_duration_rejects_malformed_values(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_parse_duration_rejects_unknown_units():
    with pytest.raises(ValueError, match="not a duration unit"):
        parse_duration("5x")


def test_parse_duration_enforces_the_minimum():
    with pytest.raises(ValueError, match="at least"):
        parse_duration("0m")
    assert parse_duration(f"{MIN_DURATION_SECONDS}s") == MIN_DURATION_SECONDS


def test_parse_duration_enforces_the_maximum():
    with pytest.raises(ValueError, match="at most 60 days"):
        parse_duration("61d")


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "a moment"),
        (-5, "a moment"),
        (59, "59s"),
        (60, "1m"),
        (61, "1m 1s"),
        (3599, "59m 59s"),
        (3600, "1h"),
        (3661, "1h 1m"),
        (7200, "2h"),
        (86399, "23h 59m"),
        (86400, "1d"),
        (90061, "1d 1h"),
    ],
)
def test_format_delta(seconds, expected):
    assert format_delta(seconds) == expected


@pytest.mark.parametrize(
    "remaining, expected",
    [
        (4000, 300.0),
        (3600, 120.0),
        (900, 60.0),
        (300, 20.0),
        (60, FINAL_REFRESH_SECONDS),
        (0, FINAL_REFRESH_SECONDS),
        (-1, FINAL_REFRESH_SECONDS),
    ],
)
def test_refresh_interval_tightens_as_the_end_approaches(remaining, expected):
    assert refresh_interval(remaining) == expected


def test_refresh_interval_never_increases_as_time_runs_out():
    intervals = [refresh_interval(remaining) for remaining in range(7200, -1, -30)]
    assert all(later <= earlier for earlier, later in zip(intervals, intervals[1:]))


def test_parse_iso_reads_the_stored_format_as_utc():
    moment = parse_iso("2026-10-11T12:00:00Z")
    assert moment is not None
    assert (moment.year, moment.month, moment.day, moment.hour) == (2026, 10, 11, 12)
    assert moment.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("value", [None, "", "junk", 5, "2026-10-11 12:00:00"])
def test_parse_iso_tolerates_junk(value):
    assert parse_iso(value) is None


@pytest.mark.parametrize(
    "stored, expected",
    [
        (None, []),
        ("", []),
        ("[1,2,3]", [1, 2, 3]),
        ("[1,1,2]", [1, 2]),
        ('["9","z",10]', [9, 10]),
        ([5, "6", None, "x", 5], [5, 6]),
        ((7, 8), [7, 8]),
    ],
)
def test_winner_ids_parses_stored_winners(stored, expected):
    assert winner_ids({"winners": stored}) == expected


@pytest.mark.parametrize("stored", ["junk", '{"a": 1}', "123", 0])
def test_winner_ids_degrades_to_empty_on_corrupt_values(stored):
    assert winner_ids({"winners": stored}) == []


def test_winner_ids_without_the_key_is_empty():
    assert winner_ids({}) == []


class _FixedRandom:
    """A stand-in RNG whose ``uniform`` always returns the same value."""

    def __init__(self, value: float) -> None:
        self.value = value

    def uniform(self, low: float, high: float) -> float:
        return self.value


@pytest.mark.parametrize(
    "pick, expected",
    [(0.5, 0), (1.0, 0), (1.5, 1), (3.0, 1), (3.5, 2), (6.0, 2), (99.0, 2)],
)
def test_weighted_index_maps_a_pick_onto_cumulative_weights(pick, expected):
    assert _weighted_index(_FixedRandom(pick), [1, 2, 3]) == expected


def test_weighted_index_with_no_usable_weight_returns_zero():
    rng = random.Random(0)
    assert _weighted_index(rng, []) == 0
    assert _weighted_index(rng, [0, 0]) == 0
    assert _weighted_index(rng, [5]) == 0


def test_weighted_index_never_picks_a_zero_weight_entry():
    rng = random.Random(1234)
    for _ in range(500):
        assert _weighted_index(rng, [0, 0, 3]) == 2


def test_weighted_index_follows_the_weights_statistically():
    rng = random.Random(1)
    counts = [0, 0, 0]
    for _ in range(6000):
        counts[_weighted_index(rng, [1, 2, 3])] += 1
    # Expected share is 1/6, 2/6 and 3/6 of 6000; allow generous slack.
    assert counts[0] < counts[1] < counts[2]
    assert 700 < counts[0] < 1300
    assert 1700 < counts[1] < 2300
    assert 2700 < counts[2] < 3300


def test_message_link_regex_extracts_ids_from_all_discord_hosts():
    ids = "123456789012345678/223456789012345678/323456789012345678"
    for host in ("discord.com", "ptb.discord.com", "canary.discordapp.com"):
        match = MESSAGE_LINK_RE.search(f"https://{host}/channels/{ids}")
        assert match is not None
        assert match.groupdict() == {
            "guild": "123456789012345678",
            "channel": "223456789012345678",
            "message": "323456789012345678",
        }


def test_message_link_regex_rejects_direct_message_links():
    assert MESSAGE_LINK_RE.search("https://discord.com/channels/@me/1/2") is None
