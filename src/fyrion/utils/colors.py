"""
Color parsing for operator-supplied input.

Used by the embed builder and by ``/role-create``. Accepts a named color, a hex
triplet with or without a leading ``#``/``0x``, or the literal ``random``.
Invalid input raises :class:`ValueError` carrying a message that is safe to show
to the user, so callers never have to guess how to phrase the failure.
"""

from __future__ import annotations

import random
import re
from typing import Final

import discord

NAMED_COLORS: Final[dict[str, int]] = {
    "default": 0x000000,
    "blurple": 0x5865F2,
    "greyple": 0x99AAB5,
    "grayple": 0x99AAB5,
    "green": 0x57F287,
    "dark_green": 0x1F8B4C,
    "red": 0xED4245,
    "dark_red": 0x992D22,
    "yellow": 0xFEE75C,
    "gold": 0xF1C40F,
    "orange": 0xE67E22,
    "blue": 0x3498DB,
    "dark_blue": 0x206694,
    "teal": 0x1ABC9C,
    "purple": 0x9B59B6,
    "magenta": 0xE91E63,
    "pink": 0xEB459E,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
    "dark_grey": 0x607D8B,
    "dark_gray": 0x607D8B,
    "white": 0xFFFFFF,
    "black": 0x010101,
}

_HEX = re.compile(r"^(?:#|0x)?([0-9a-f]{6})$", re.IGNORECASE)
_SHORT_HEX = re.compile(r"^(?:#|0x)?([0-9a-f]{3})$", re.IGNORECASE)


def parse_color(raw: str | None) -> discord.Color | None:
    """Parses a color string. Returns ``None`` when nothing was supplied.

    Raises:
        ValueError: when the value cannot be interpreted as a color.
    """
    if raw is None:
        return None

    text = raw.strip()
    if not text:
        return None

    lowered = text.lower().replace(" ", "_")

    if lowered == "random":
        return discord.Color(random.randint(0, 0xFFFFFF))

    if lowered in NAMED_COLORS:
        return discord.Color(NAMED_COLORS[lowered])

    match = _HEX.match(text)
    if match is not None:
        return discord.Color(int(match.group(1), 16))

    short = _SHORT_HEX.match(text)
    if short is not None:
        digits = short.group(1)
        expanded = "".join(digit * 2 for digit in digits)
        return discord.Color(int(expanded, 16))

    raise ValueError(
        f"`{text[:32]}` is not a color. Use a hex value such as `#5865F2` or "
        "one of: " + ", ".join(sorted(set(NAMED_COLORS))[:8]) + ", random."
    )


__all__ = ["NAMED_COLORS", "parse_color"]
