"""
Interactive embed builder.

``/embed-builder`` opens a modal, validates every field, then shows an ephemeral
preview with a send/cancel prompt. Nothing is posted to a channel until the
author explicitly confirms, so a typo never becomes a public announcement.

Safety properties:

* Embed content is operator supplied, so the final message is sent with mentions
  disabled. An embed cannot ping ``@everyone`` even if the text contains it.
* Only ``http``/``https`` URLs are accepted for the image and thumbnail fields;
  other schemes are refused outright.
* Field lengths and the 6000 character total are checked before the API call,
  so Discord's rejection is turned into a readable message instead of a 400.
* The preview view is bound to its author and expires, so an abandoned draft
  cannot be sent later by someone else.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import discord

from fyrion.utils.colors import parse_color

log = logging.getLogger("fyrion.views.embeds")

NO_MENTIONS = discord.AllowedMentions.none()

MAX_TITLE = 256
MAX_DESCRIPTION = 4000
MAX_FOOTER = 2048
MAX_URL = 500
MAX_TOTAL = 6000
PREVIEW_TIMEOUT = 300.0


def validate_url(raw: str | None, *, field: str) -> str | None:
    """Returns a validated absolute http(s) URL, or ``None`` when unset.

    Raises:
        ValueError: when the value is not an absolute http(s) URL.
    """
    if raw is None:
        return None

    text = raw.strip()
    if not text:
        return None
    if len(text) > MAX_URL:
        raise ValueError(f"The {field} must be at most {MAX_URL} characters.")

    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"The {field} must be an absolute link starting with http:// or "
            "https://."
        )
    return text


def embed_length(embed: discord.Embed) -> int:
    """Returns the character count Discord applies its 6000 limit to."""
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer is not None and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author is not None and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


class EmbedPreviewView(discord.ui.View):
    """Send/cancel prompt shown alongside the rendered preview."""

    def __init__(
        self,
        author_id: int,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
        *,
        timeout: float = PREVIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.channel = channel
        self.embed = embed
        # Set after the preview is sent so an expired draft can retract its own
        # buttons instead of leaving a dead Send prompt behind.
        self.message: discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "\u274c Only the person who built this embed can send it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Send", style=discord.ButtonStyle.success, emoji="\U0001f4e4"
    )
    async def send_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        channel = self.channel

        if guild is not None and isinstance(channel, discord.abc.GuildChannel):
            me = guild.me
            if me is not None:
                permissions = channel.permissions_for(me)
                if not (permissions.send_messages and permissions.embed_links):
                    await interaction.response.edit_message(
                        content=(
                            "\u274c I need `Send Messages` and `Embed Links` in "
                            f"{channel.mention} to post this."
                        ),
                        embed=None,
                        view=None,
                    )
                    self.stop()
                    return

        try:
            message = await channel.send(embed=self.embed, allowed_mentions=NO_MENTIONS)
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="\u274c Discord refused to post the embed there.",
                embed=None,
                view=None,
            )
            self.stop()
            return
        except discord.HTTPException as exc:
            log.warning("Embed builder failed to post an embed: %s", exc)
            await interaction.response.edit_message(
                content=f"\u274c Discord rejected the embed (HTTP {exc.status}).",
                embed=None,
                view=None,
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content=f"\u2705 Embed posted: {message.jump_url}",
            embed=None,
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="\u2139\ufe0f Draft discarded; nothing was posted.",
            embed=None,
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        # Retract the Send prompt so an abandoned draft cannot be posted later
        # from a stale button; the preview embed is kept for reference.
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="\u23f1\ufe0f Preview expired; nothing was posted.",
                view=None,
            )
        except discord.HTTPException:
            pass


class EmbedBuilderModal(discord.ui.Modal, title="Embed Builder"):
    """Collects the embed fields, then shows a preview."""

    embed_title: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Title",
        placeholder="Optional heading",
        required=False,
        max_length=MAX_TITLE,
    )
    description: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="The body of the embed. Markdown is supported.",
        required=True,
        max_length=MAX_DESCRIPTION,
    )
    color: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Color",
        placeholder="#5865F2, blurple, random…",
        required=False,
        max_length=32,
    )
    image_url: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Image URL",
        placeholder="https://… (optional)",
        required=False,
        max_length=MAX_URL,
    )
    footer: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Footer",
        placeholder="Optional small print",
        required=False,
        max_length=MAX_FOOTER,
    )

    def __init__(self, channel: discord.abc.Messageable) -> None:
        super().__init__(timeout=600.0)
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            color = parse_color(self.color.value)
            image = validate_url(self.image_url.value, field="image URL")
        except ValueError as exc:
            await interaction.response.send_message(
                f"\u274c {exc}", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return

        embed = discord.Embed(
            description=self.description.value.strip(),
            color=color if color is not None else discord.Color.blurple(),
        )

        heading = (self.embed_title.value or "").strip()
        if heading:
            embed.title = heading

        footer_text = (self.footer.value or "").strip()
        if footer_text:
            embed.set_footer(text=footer_text)

        if image is not None:
            embed.set_image(url=image)

        if embed_length(embed) > MAX_TOTAL:
            await interaction.response.send_message(
                "\u274c That embed is too long. Discord allows "
                f"{MAX_TOTAL} characters across all fields.",
                ephemeral=True,
            )
            return

        target = getattr(self.channel, "mention", "this channel")
        view = EmbedPreviewView(interaction.user.id, self.channel, embed)
        await interaction.response.send_message(
            content=(
                f"Preview only \u2014 nothing has been posted yet. Press **Send** "
                f"to publish this to {target}."
            ),
            embed=embed,
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        # Needed so a timed-out preview can retract its own Send prompt.
        view.message = await interaction.original_response()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.error("Embed builder modal failed", exc_info=error)
        message = "\u274c The embed could not be built. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


__all__ = [
    "EmbedBuilderModal",
    "EmbedPreviewView",
    "embed_length",
    "validate_url",
    "MAX_TOTAL",
]
