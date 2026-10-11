"""
Tests for the shared content-matching helpers used by AutoMod and purge.
"""

import pytest

from fyrion.utils.patterns import (
    CONTENT_SCAN_LIMIT,
    INVITE_REGEX,
    URL_REGEX,
    caps_ratio,
    count_lines,
    count_mentions,
    excerpt,
)

USER_ID = "123456789012345678"


@pytest.mark.parametrize(
    "text",
    [
        "join discord.gg/abc123 now",
        "https://discord.com/invite/XyZ",
        "discordapp.com/invite/abc",
        "http://www.discord.gg/abc",
        "dsc.gg/foo-bar_1",
        "DISCORD.GG/ABC",
    ],
)
def test_invite_regex_matches_invite_links(text):
    assert INVITE_REGEX.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "hello world",
        "see discord.gg/ only",
        "https://example.com/invite/abc",
        "discord is great",
    ],
)
def test_invite_regex_ignores_ordinary_text(text):
    assert INVITE_REGEX.search(text) is None


def test_invite_regex_captures_the_code():
    match = INVITE_REGEX.search("come in: discord.gg/abc123 please")
    assert match is not None
    assert match.group(0) == "discord.gg/abc123"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com/x", "https://example.com/x"),
        ("www.foo", "www.foo"),
        ("visit example.com today", "example.com"),
        ("x.io!", "x.io"),
    ],
)
def test_url_regex_matches_links(text, expected):
    match = URL_REGEX.search(text)
    assert match is not None
    assert match.group(0) == expected


@pytest.mark.parametrize("text", ["hello", "file.txt", "readme.md", "e.g. this", "ok."])
def test_url_regex_ignores_prose_and_file_names(text):
    assert URL_REGEX.search(text) is None


def test_url_regex_finishes_on_hostile_input_at_the_scan_limit():
    # A long run that nearly matches a host must still terminate and report no
    # match. Callers only ever scan up to CONTENT_SCAN_LIMIT characters.
    hostile = ("a-" * CONTENT_SCAN_LIMIT)[:CONTENT_SCAN_LIMIT]
    assert URL_REGEX.search(hostile) is None


def test_caps_ratio_counts_only_letters():
    assert caps_ratio("HELLO world") == (10, 50.0)
    assert caps_ratio("ABC") == (3, 100.0)


@pytest.mark.parametrize("text", ["", "123 !!!", "\U0001f600\U0001f600"])
def test_caps_ratio_without_letters_is_zero(text):
    assert caps_ratio(text) == (0, 0.0)


def test_caps_ratio_ignores_caseless_scripts():
    # CJK characters are alphabetic but have no case, so they never count as
    # uppercase.
    assert caps_ratio("\u65e5\u672c\u8a9e") == (3, 0.0)


@pytest.mark.parametrize(
    "text, expected",
    [("", 0), ("a", 1), ("a\nb", 2), ("a\n", 2), ("a\nb\nc", 3)],
)
def test_count_lines(text, expected):
    assert count_lines(text) == expected


def test_count_mentions_counts_users_and_roles():
    text = f"<@{USER_ID}> <@!{USER_ID}> <@&{USER_ID}>"
    assert count_mentions(text) == 3


def test_count_mentions_counts_repeats_but_not_lookalikes():
    assert count_mentions(f"<@{USER_ID}><@{USER_ID}>") == 2
    # Too-short ids, bare @everyone and plain text are not mentions.
    assert count_mentions("<@123> @everyone @here hello") == 0


def test_excerpt_short_text_is_unchanged():
    assert excerpt("short") == "short"


def test_excerpt_truncates_with_ellipsis_at_the_limit():
    result = excerpt("x" * 100, 10)
    assert len(result) == 10
    assert result.endswith("\u2026")
    assert result == "x" * 9 + "\u2026"


def test_excerpt_defuses_backticks_and_mentions_and_collapses_whitespace():
    result = excerpt("a`b @everyone  \n  c")
    assert "`" not in result
    assert "@everyone" not in result
    assert result == "a\u02cbb @\u200beveryone c"
