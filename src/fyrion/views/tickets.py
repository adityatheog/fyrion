"""
Persistent views for the ticket system.

Both views use fixed ``custom_id``s and ``timeout=None``, so Discord routes
interactions from panels posted before a restart back to this process. They are
registered in :data:`fyrion.bot.PERSISTENT_VIEWS`.

A persistent view cannot carry per-guild state, because it is constructed once at
boot rather than per message. The panel therefore declares a fixed set of topic
slots (``fyrion:ticket:create:0`` through ``:4``); the label, emoji and style of
each slot are read from the guild's stored panel configuration when the button is
pressed, and unconfigured slots simply report that the panel needs rebuilding.

All ticket logic lives in :mod:`fyrion.utils.tickets`, which the slash commands
also call, so a button and a command can never diverge in behaviour or in the
permission checks they apply.

Every reply here is ephemeral: a failed ticket attempt is the member's business,
not the channel's.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ui import Button, View

from fyrion.database.repositories.tickets import TicketRepository
from fyrion.utils import tickets as service

log = logging.getLogger("fyrion.views.tickets")

NO_MENTIONS = discord.AllowedMentions.none()

CREATE_PREFIX = "fyrion:ticket:create"
CLOSE_ID = "fyrion:ticket:close"
CLAIM_ID = "fyrion:ticket:claim"


def _repo(interaction: discord.Interaction) -> TicketRepository:
    return TicketRepository(interaction.client.db)  # type: ignore[attr-defined]


async def _reply(
    interaction: discord.Interaction, message: str, *, ephemeral: bool = True
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )
    except discord.NotFound:
        # The interaction token expired; there is nothing left to answer.
        log.debug("Ticket interaction expired before it could be answered.")
    except discord.HTTPException as exc:
        log.warning("Could not answer a ticket interaction: %s", exc)


class TicketControlView(View):
    """Claim and close controls posted inside a ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        custom_id=CLAIM_ID,
        emoji="\U0001f44b",
    )
    async def claim_ticket(
        self, interaction: discord.Interaction, button: Button
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(interaction, "\u274c This only works inside a server.")
            return

        await interaction.response.defer(ephemeral=True)
        repo = _repo(interaction)

        try:
            config = await repo.get_config(guild.id)
            ticket = await repo.get_ticket_by_channel(interaction.channel_id or 0)
        except Exception:
            log.exception("Could not read the ticket for a claim interaction.")
            await _reply(interaction, "\u274c I could not read that ticket.")
            return

        if ticket is None:
            await _reply(interaction, "\u274c This channel is not a Fyrion ticket.")
            return

        # Staff status is re-checked here rather than trusted from the button's
        # visibility, which Discord does not restrict.
        if not service.is_support(member, config):
            await _reply(
                interaction, "\U0001f6ab Only ticket staff can claim a ticket."
            )
            return

        if str(ticket.get("status")) == "closed":
            await _reply(interaction, "\u274c That ticket is already closed.")
            return

        claimed_by = ticket.get("claimed_by")
        if claimed_by and int(claimed_by) == member.id:
            await _reply(
                interaction, "\u2139\ufe0f You have already claimed this ticket."
            )
            return

        if not await repo.claim_ticket(interaction.channel_id or 0, member.id):
            holder = f"<@{int(claimed_by)}>" if claimed_by else "another staff member"
            await _reply(
                interaction, f"\u2139\ufe0f This ticket is already claimed by {holder}."
            )
            return

        await _reply(interaction, "\u2705 You claimed this ticket.")

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                description=f"{member.mention} claimed this ticket.",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            try:
                await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id=CLOSE_ID,
        emoji="\U0001f512",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: Button
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(interaction, "\u274c This only works inside a server.")
            return

        # Deferring immediately also makes a double click harmless: the second
        # close is rejected by the conditional UPDATE in the service layer.
        await interaction.response.defer(ephemeral=True)
        repo = _repo(interaction)

        try:
            config = await repo.get_config(guild.id)
            ticket = await repo.get_ticket_by_channel(interaction.channel_id or 0)
        except Exception:
            log.exception("Could not read the ticket for a close interaction.")
            await _reply(interaction, "\u274c I could not read that ticket.")
            return

        if ticket is None:
            await _reply(interaction, "\u274c This channel is not a Fyrion ticket.")
            return

        # The member who opened the ticket may close their own; everyone else
        # needs to be staff.
        if not (service.is_owner(ticket, member) or service.is_support(member, config)):
            await _reply(
                interaction,
                "\U0001f6ab Only the member who opened this ticket or a staff "
                "member can close it.",
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await _reply(
                interaction, "\u274c Tickets can only be closed from their own channel."
            )
            return

        result = await service.close_ticket(
            guild=guild,
            channel=channel,
            repo=repo,
            closed_by=member,
            reason="Closed with the ticket button",
            config=config,
        )

        if not result.closed:
            await _reply(
                interaction,
                f"\u274c {result.error or 'That ticket could not be closed.'}",
            )
            return

        lines = ["\u2705 Ticket closed. The channel will be deleted in a few seconds."]
        if result.log_url:
            lines.append(f"Transcript archived: {result.log_url}")
        lines.extend(f"\u26a0\ufe0f {warning}" for warning in result.warnings)
        await _reply(interaction, "\n".join(lines))


class TicketPanelView(View):
    """The persistent panel exposing one button per configured topic.

    Every slot is declared up front so the ``custom_id``s are stable across
    restarts. Labels are refreshed from the database each time a panel is posted
    (see :meth:`for_config`); the pressed slot is resolved against the stored
    configuration, so a panel posted months ago still opens the right topic.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for index in range(service.MAX_TOPICS):
            self.add_item(TicketCreateButton(index))

    @classmethod
    def for_config(cls, config: dict[str, Any]) -> "TicketPanelView":
        """Returns a view whose buttons reflect the guild's stored topics."""
        view = cls()
        topics = service.sanitize_topics(config.get("topics"))

        for item in list(view.children):
            if not isinstance(item, TicketCreateButton):
                continue
            if item.index >= len(topics):
                # Unused slots are removed from the posted message, but their
                # custom ids remain registered for older panels.
                view.remove_item(item)
                continue

            topic = topics[item.index]
            item.label = str(topic.get("label") or "Create Ticket")[
                : service.MAX_TOPIC_LABEL
            ]
            item.style = service.button_style(topic.get("style"))
            emoji = topic.get("emoji")
            if emoji:
                try:
                    item.emoji = discord.PartialEmoji.from_str(str(emoji))
                except (ValueError, TypeError):
                    item.emoji = None
            else:
                item.emoji = None

        return view


class TicketCreateButton(Button["TicketPanelView"]):
    """One topic slot on the ticket panel."""

    def __init__(self, index: int) -> None:
        super().__init__(
            label="Create Ticket" if index == 0 else f"Topic {index + 1}",
            style=discord.ButtonStyle.primary,
            custom_id=f"{CREATE_PREFIX}:{index}",
            emoji="\U0001f3ab" if index == 0 else None,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(interaction, "\u274c Tickets can only be opened in a server.")
            return

        # Channel creation and the greeting take longer than the three second
        # interaction window allows.
        await interaction.response.defer(ephemeral=True)
        repo = _repo(interaction)

        try:
            config = await repo.get_config(guild.id)
        except Exception:
            log.exception("Could not read the ticket config for guild %s.", guild.id)
            await _reply(
                interaction,
                "\u274c I could not read the ticket configuration. Please try again.",
            )
            return

        topics = service.sanitize_topics(config.get("topics"))
        if self.index >= len(topics):
            await _reply(
                interaction,
                "\u274c That option is no longer available. Ask an administrator "
                "to run `/ticket-setup` again.",
            )
            return

        result = await service.open_ticket(
            guild=guild,
            member=member,
            repo=repo,
            config=config,
            topic=topics[self.index],
            panel_message_id=interaction.message.id if interaction.message else None,
            control_view=TicketControlView(),
        )

        if not result.ok or result.channel is None:
            await _reply(
                interaction,
                f"\u274c {result.error or 'Your ticket could not be created.'}",
            )
            return

        await _reply(
            interaction, f"\u2705 Your ticket is ready: {result.channel.mention}"
        )


__all__ = [
    "TicketControlView",
    "TicketPanelView",
    "TicketCreateButton",
    "CREATE_PREFIX",
    "CLAIM_ID",
    "CLOSE_ID",
]
