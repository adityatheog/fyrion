"""Unit tests for the shared emoji resolver and parsers.

A fake bot stands in for ``discord.Client`` so nothing here needs a live
gateway connection. ``get_emoji`` is the only method the emoji utilities call,
and it returns whichever ids the fake was told the bot can see.
"""

from __future__ import annotations

import discord
import pytest

from fyrion.utils import emojis


class FakeBot:
    """Minimal stand-in exposing only ``get_emoji``.

    ``visible_ids`` is the set of custom-emoji ids the bot shares a server with;
    ``get_emoji`` returns a truthy sentinel for those and ``None`` otherwise,
    mirroring ``discord.Client.get_emoji``.
    """

    def __init__(self, visible_ids: set[int] | None = None) -> None:
        self.visible_ids = visible_ids or set()

    def get_emoji(self, emoji_id: int):
        if emoji_id in self.visible_ids:
            return object()  # a truthy stand-in for a discord.Emoji
        return None


VISIBLE_ID = 123456789012345678
HIDDEN_ID = 987654321098765432


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_unicode_returns_unchanged():
    bot = FakeBot()
    assert emojis.resolve(bot, "\U0001f3ab", fallback="✅") == "\U0001f3ab"


def test_resolve_hidden_custom_emoji_returns_fallback():
    bot = FakeBot(visible_ids={VISIBLE_ID})
    key = f"party:{HIDDEN_ID}"
    assert emojis.resolve(bot, key, fallback="✅") == "✅"


def test_resolve_visible_custom_emoji_returns_partial_emoji():
    bot = FakeBot(visible_ids={VISIBLE_ID})
    key = f"party:{VISIBLE_ID}"
    result = emojis.resolve(bot, key, fallback="✅")
    assert isinstance(result, discord.PartialEmoji)
    assert result.id == VISIBLE_ID
    assert result.name == "party"


def test_resolve_visible_custom_emoji_from_mention_form():
    bot = FakeBot(visible_ids={VISIBLE_ID})
    result = emojis.resolve(bot, f"<:party:{VISIBLE_ID}>", fallback="✅")
    assert isinstance(result, discord.PartialEmoji)
    assert result.id == VISIBLE_ID


def test_resolve_empty_key_returns_fallback():
    bot = FakeBot()
    assert emojis.resolve(bot, "", fallback="✅") == "✅"
    assert emojis.resolve(bot, None, fallback="✅") == "✅"


def test_resolve_garbage_raises_value_error():
    bot = FakeBot()
    with pytest.raises(ValueError):
        emojis.resolve(bot, "not-an-emoji", fallback="✅")


# ---------------------------------------------------------------------------
# parse_emoji_input (reaction roles)
# ---------------------------------------------------------------------------


def test_parse_emoji_input_unicode_roundtrips():
    bot = FakeBot()
    reaction, key = emojis.parse_emoji_input(bot, "\U0001f44d")
    assert reaction == "\U0001f44d"
    assert key == "\U0001f44d"


def test_parse_emoji_input_visible_custom_returns_partial_and_key():
    bot = FakeBot(visible_ids={VISIBLE_ID})
    reaction, key = emojis.parse_emoji_input(bot, f"<:party:{VISIBLE_ID}>")
    assert isinstance(reaction, discord.PartialEmoji)
    assert key == f"party:{VISIBLE_ID}"


def test_parse_emoji_input_hidden_custom_raises():
    bot = FakeBot()
    with pytest.raises(ValueError):
        emojis.parse_emoji_input(bot, f"<:party:{HIDDEN_ID}>")


def test_parse_emoji_input_empty_raises():
    bot = FakeBot()
    with pytest.raises(ValueError):
        emojis.parse_emoji_input(bot, "   ")


def test_parse_emoji_input_garbage_raises():
    bot = FakeBot()
    with pytest.raises(ValueError):
        emojis.parse_emoji_input(bot, "hello")


# ---------------------------------------------------------------------------
# parse_emoji (ticket buttons)
# ---------------------------------------------------------------------------


def test_parse_emoji_empty_returns_none():
    bot = FakeBot()
    assert emojis.parse_emoji(bot, "") is None
    assert emojis.parse_emoji(bot, "   ") is None


def test_parse_emoji_unicode_returns_glyph():
    bot = FakeBot()
    assert emojis.parse_emoji(bot, "\U0001f3ab") == "\U0001f3ab"


def test_parse_emoji_visible_custom_returns_mention():
    bot = FakeBot(visible_ids={VISIBLE_ID})
    assert emojis.parse_emoji(bot, f"party:{VISIBLE_ID}") == f"<:party:{VISIBLE_ID}>"


def test_parse_emoji_hidden_custom_raises():
    bot = FakeBot()
    with pytest.raises(ValueError):
        emojis.parse_emoji(bot, f"<:party:{HIDDEN_ID}>")


def test_parse_emoji_garbage_raises():
    bot = FakeBot()
    with pytest.raises(ValueError):
        emojis.parse_emoji(bot, "just text")


# ---------------------------------------------------------------------------
# storage keys and catalog
# ---------------------------------------------------------------------------


def test_emoji_storage_key_unicode_verbatim():
    assert emojis.emoji_storage_key("\U0001f44d") == "\U0001f44d"


def test_emoji_storage_key_custom_uses_name_and_id():
    partial = discord.PartialEmoji(name="party", id=VISIBLE_ID)
    assert emojis.emoji_storage_key(partial) == f"party:{VISIBLE_ID}"


def test_fallback_catalog_has_core_glyphs():
    for name in ("success", "error", "warn", "loading", "ticket", "lock", "wave"):
        assert emojis.fallback(name)
    assert len(emojis.POLL_DIGITS) == 10
