"""Shared custom-emoji parsing, validation, and safe resolution.

Historically the same parse/validate logic was copied into ``cogs/admin.py``
(reaction roles) and ``utils/tickets.py`` (panel buttons), and every cog carried
its own scattered ``\\U0001...`` escapes for the handful of status glyphs it
drew. This module is the single owner of all three concerns:

* a **catalog** (:data:`FALLBACKS`) of the unicode glyphs used across cogs,
  reachable by name so a call site reads ``FALLBACKS["success"]`` instead of an
  opaque escape;
* :func:`emoji_storage_key`, :func:`parse_emoji_input` and :func:`parse_emoji`,
  the operator-input validators shared by reaction roles and ticket panels;
* :func:`resolve`, which turns a stored emoji key into something a
  :class:`discord.Embed` or a button can render, degrading a custom emoji the
  bot can no longer see to a unicode fallback instead of a dead ``:name:``.

The distinction between :func:`parse_emoji_input` and :func:`resolve` is
deliberate. Parsing runs when an operator *configures* an emoji, so an
unreachable custom emoji is rejected loudly and the operator is told why.
Resolution runs when the emoji is *rendered*, long after configuration, where
the same unreachable emoji must degrade to a fallback rather than raise on the
interaction path.
"""
from __future__ import annotations

from typing import NoReturn

import discord

# A standard emoji is never plain ASCII and never long. Bounding the length
# keeps arbitrary operator text out of the reaction, button and message APIs.
MAX_UNICODE_EMOJI_LENGTH = 16

# ---------------------------------------------------------------------------
# Fallback catalog
# ---------------------------------------------------------------------------

# Named unicode glyphs used across cogs. Referencing ``FALLBACKS["success"]``
# reads better than a bare ``\U0001...`` escape and gives :func:`resolve` a
# stable place to pull a degrade-to value from. Values are plain ``str`` so they
# drop straight into embeds, buttons and reactions.
FALLBACKS: dict[str, str] = {
    "success": "✅",  # white check mark
    "error": "❌",  # cross mark
    "warn": "⚠️",  # warning sign
    "info": "ℹ️",  # information source
    "loading": "⏳",  # hourglass
    "ticket": "\U0001f3ab",  # ticket
    "lock": "\U0001f512",  # closed lock
    "unlock": "\U0001f513",  # open lock
    "wave": "\U0001f44b",  # waving hand
    "yes": "\U0001f44d",  # thumbs up
    "no": "\U0001f44e",  # thumbs down
}

# Keycap digits 1-9 followed by the "keycap ten" glyph, used for numbered poll
# options. Kept here so the catalog owns every reusable glyph in one place.
POLL_DIGITS: tuple[str, ...] = (
    "1️⃣",
    "2️⃣",
    "3️⃣",
    "4️⃣",
    "5️⃣",
    "6️⃣",
    "7️⃣",
    "8️⃣",
    "9️⃣",
    "\U0001f51f",
)


def fallback(name: str) -> str:
    """Returns the catalog glyph for ``name``.

    Raises:
        KeyError: when ``name`` is not a known catalog entry.
    """
    return FALLBACKS[name]


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def _reject_not_emoji(text: str) -> NoReturn:
    raise ValueError(
        f"`{text[:32]}` is not an emoji. Use a single standard emoji, or a "
        "custom emoji from a server I am also in."
    )


def _validate(bot: discord.Client, text: str) -> discord.PartialEmoji | str:
    """Validates already-stripped, non-empty operator input.

    Returns the unicode glyph as a ``str`` or a reachable custom emoji as a
    :class:`discord.PartialEmoji`.

    Raises:
        ValueError: when the value is not a usable emoji, or names a custom
            emoji the bot cannot access.
    """
    partial = discord.PartialEmoji.from_str(text)

    if partial.id is None:
        candidate = partial.name or text
        if candidate.isascii() or len(candidate) > MAX_UNICODE_EMOJI_LENGTH:
            _reject_not_emoji(text)
        return candidate

    if bot.get_emoji(partial.id) is None:
        raise ValueError(
            "I cannot use that custom emoji. I must be a member of the server "
            "it belongs to."
        )
    return partial


def emoji_storage_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    """Returns the canonical database key for an emoji.

    Unicode emoji are stored verbatim; custom emoji as ``name:id``. The id is
    what actually identifies a custom emoji, so lookups fall back to matching on
    the id alone when an emoji has been renamed since the mapping was created.
    """
    if isinstance(emoji, str):
        return emoji
    if emoji.id is None:
        return emoji.name or ""
    return f"{emoji.name}:{emoji.id}"


def parse_emoji_input(
    bot: discord.Client, raw: str
) -> tuple[discord.PartialEmoji | str, str]:
    """Parses operator input into ``(reaction_argument, storage_key)``.

    Used by reaction roles, where the parsed value is added as a reaction and
    the storage key is persisted for later matching.

    Raises:
        ValueError: when the value is not a usable emoji.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("No emoji was supplied.")

    result = _validate(bot, text)
    if isinstance(result, str):
        return result, result
    return result, emoji_storage_key(result)


def parse_emoji(bot: discord.Client, raw: str) -> str | None:
    """Validates an operator-supplied emoji for use on a button.

    Returns ``None`` for empty input, the unicode glyph for a standard emoji, or
    the ``<:name:id>`` mention form for a reachable custom emoji.

    Raises:
        ValueError: when the value is not a usable emoji.
    """
    text = (raw or "").strip()
    if not text:
        return None

    result = _validate(bot, text)
    if isinstance(result, str):
        return result

    prefix = "a" if result.animated else ""
    return f"<{prefix}:{result.name}:{result.id}>"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def resolve(
    bot: discord.Client, key: str | None, *, fallback: str
) -> discord.PartialEmoji | str:
    """Turns a stored emoji key into a renderable emoji, degrading gracefully.

    ``key`` is a unicode glyph, a ``name:id`` storage key, or a ``<:name:id>``
    mention. Unicode is returned unchanged. A custom emoji is returned as a
    :class:`discord.PartialEmoji` when the bot shares its server
    (``bot.get_emoji`` finds it), and as ``fallback`` when it does not, so an
    emoji the bot has lost access to renders as a glyph instead of dead text.
    An empty or missing key also yields ``fallback``.

    Raises:
        ValueError: when ``key`` is non-empty but is not a usable emoji at all
            (garbage input), so a misconfiguration surfaces rather than silently
            rendering as a fallback.
    """
    text = (key or "").strip()
    if not text:
        return fallback

    partial = discord.PartialEmoji.from_str(text)

    if partial.id is None:
        candidate = partial.name or text
        if candidate.isascii() or len(candidate) > MAX_UNICODE_EMOJI_LENGTH:
            _reject_not_emoji(text)
        return candidate

    if bot.get_emoji(partial.id) is None:
        return fallback
    return partial


__all__ = [
    "MAX_UNICODE_EMOJI_LENGTH",
    "FALLBACKS",
    "POLL_DIGITS",
    "fallback",
    "emoji_storage_key",
    "parse_emoji_input",
    "parse_emoji",
    "resolve",
]
