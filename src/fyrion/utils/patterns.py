"""
Shared content-matching patterns.

The AutoMod interceptor and the purge filters need the same notion of "this
message contains a link" or "this message contains an invite". Keeping the
expressions in one module means the two features can never drift apart, and it
keeps the cogs from importing each other.

Every pattern here is applied to attacker-controlled text on the message hot
path, so they are deliberately linear: alternations of literals and bounded
character classes only, with no nested quantifiers that could backtrack
explosively.
"""

from __future__ import annotations

import re
from typing import Final

# Discord invite hosts, including the common vanity redirectors.
INVITE_REGEX: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|discord\.io"
    r"|dsc\.gg|invite\.gg|discord\.link)/+[a-z0-9\-_]+",
    re.IGNORECASE,
)

# Explicit schemes, www hosts, or a bare domain with a common TLD. The TLD list
# is finite on purpose: matching "anything.anything" would flag ordinary prose
# such as file names and abbreviations.
URL_REGEX: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\."
    r"(?:com|net|org|io|gg|me|xyz|dev|app|co|ru|tv|link|site|shop|info|biz"
    r"|top|online|store|club|fun|live|cc|to|ly|sh|pw|su|space|website|icu)\b",
    re.IGNORECASE,
)

# Raw mention forms. Counting these instead of ``message.mentions`` avoids
# false positives from the implicit reply mention, and avoids counting the same
# user twice when discord.py resolves duplicates.
USER_MENTION_REGEX: Final[re.Pattern[str]] = re.compile(r"<@!?\d{15,25}>")
ROLE_MENTION_REGEX: Final[re.Pattern[str]] = re.compile(r"<@&\d{15,25}>")

# How much of a message body is ever handed to a regex. Discord caps messages at
# 4000 characters for boosted users; bounding it keeps worst-case scan cost flat.
CONTENT_SCAN_LIMIT: Final[int] = 4000


def caps_ratio(content: str) -> tuple[int, float]:
    """Returns the letter count and the percentage of them that are uppercase.

    Only alphabetic characters count, so emoji, digits, punctuation and CJK text
    (which has no case) can never push a message over the threshold.
    """
    letters = [char for char in content if char.isalpha()]
    if not letters:
        return 0, 0.0
    uppercase = sum(1 for char in letters if char.isupper())
    return len(letters), uppercase / len(letters) * 100.0


def count_lines(content: str) -> int:
    """Returns how many lines a message body spans."""
    if not content:
        return 0
    return content.count("\n") + 1


def count_mentions(content: str) -> int:
    """Returns the number of user and role mentions written in ``content``."""
    return len(USER_MENTION_REGEX.findall(content)) + len(
        ROLE_MENTION_REGEX.findall(content)
    )


def excerpt(text: str, limit: int = 80) -> str:
    """Returns a short, inert excerpt safe to place inside an embed field.

    Backticks are stripped so the excerpt cannot break out of the code span it
    is rendered in, and every mention-like sequence is defused.
    """
    cleaned = text.replace("`", "\u02cb").replace("@", "@\u200b")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "\u2026"


__all__ = [
    "INVITE_REGEX",
    "URL_REGEX",
    "USER_MENTION_REGEX",
    "ROLE_MENTION_REGEX",
    "CONTENT_SCAN_LIMIT",
    "caps_ratio",
    "count_lines",
    "count_mentions",
    "excerpt",
]
