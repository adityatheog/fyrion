"""
Support ticket commands.

``/ticket-setup`` builds the panel: it stores the category, the support role, the
optional transcript log channel and up to five dynamic topic buttons, then posts
the panel message. ``/ticket-close``, ``/ticket-add``, ``/ticket-remove`` and
``/ticket-claim`` operate inside a ticket channel.

All of the actual work — creating the private channel, exporting the transcript,
deleting the channel — lives in :mod:`fyrion.utils.tickets`, which the panel
buttons call as well. That is deliberate: a button and a command must apply the
same permission checks and produce the same result.

Authorization model
-------------------
``default_permissions`` only decides whether Discord *shows* a command, so it is
never the gate. ``/ticket-setup`` re-checks ``Manage Server`` server side. The
per-ticket commands check ticket staff status — someone who can already manage
the server's channels, or a holder of the configured support role — against the
live guild state, and ``/ticket-close`` additionally allows the member who opened
the ticket to close their own.

Participant management is bounded on purpose: ``/ticket-add`` grants a member
read and write access to one ticket channel and nothing else, and it refuses to
grant access to a bot or to someone who already has it. ``/ticket-remove``
refuses to eject the member who opened the ticket, since that would leave them
unable to reach their own conversation.

Every reply that echoes operator text (topic labels, close reasons, display
names) disables mention parsing, so crafted input cannot make Fyrion ping a role
or ``@everyone``.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from fyrion.database.repositories.tickets import TicketRepository
from fyrion.utils import tickets as service
from fyrion.utils.modlog import send_log
from fyrion.utils.permissions import missing_channel_permissions
from fyrion.views.tickets import TicketControlView, TicketPanelView

log = logging.getLogger("fyrion.cogs.tickets")

NO_MENTIONS = discord.AllowedMentions.none()

MAX_CLOSE_REASON = 400
AUDIT_REASON_LIMIT = 512

ButtonStyleName = Literal["primary", "secondary", "success", "danger"]


class Tickets(commands.Cog):
    """Support ticket configuration and management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = TicketRepository(self.db)

    async def cog_load(self) -> None:
        try:
            await self.repo.ensure_schema()
        except Exception:
            # A missing panel table only degrades presentation; ticket rows
            # themselves live in the core schema and still work.
            log.exception("Could not prepare the ticket panel table.")

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    async def _respond(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                message, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )

    async def _reject(self, interaction: discord.Interaction, reason: str) -> None:
        await self._respond(interaction, f"\u274c {reason}")

    async def _ok(self, interaction: discord.Interaction, message: str) -> None:
        await self._respond(interaction, f"\u2705 {message}")

    @staticmethod
    def _audit_reason(actor: discord.abc.User, description: str) -> str:
        return f"{actor} ({actor.id}): {description}"[:AUDIT_REASON_LIMIT]

    async def _audit(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        action: str,
        detail: str,
    ) -> None:
        embed = discord.Embed(
            title=f"Tickets: {action}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Performed by", value=f"{actor} (`{actor.id}`)", inline=False
        )
        embed.add_field(name="Details", value=detail[:1024], inline=False)
        await send_log(self.db, guild, embed)

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    async def _authorize_admin(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Re-checks ``Manage Server`` server side."""
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return None

        permissions = member.guild_permissions
        if not (permissions.administrator or permissions.manage_guild):
            await self._reject(
                interaction, "You need the `Manage Server` permission for that."
            )
            return None

        if guild.me is None:
            await self._reject(
                interaction, "I could not resolve my own membership in this server."
            )
            return None

        return guild, member

    async def _ticket_context(
        self, interaction: discord.Interaction, *, require_staff: bool = True
    ) -> tuple[
        discord.Guild, discord.Member, discord.TextChannel, dict[str, Any], dict[str, Any]
    ] | None:
        """Resolves the ticket the command was invoked in, and authorizes it.

        Returns ``(guild, member, channel, ticket, config)`` when the command may
        proceed, otherwise replies with the refusal and returns ``None``.
        """
        guild = interaction.guild
        member = interaction.user
        channel = interaction.channel

        if guild is None or not isinstance(member, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return None
        if not isinstance(channel, discord.TextChannel):
            await self._reject(
                interaction,
                "Run this inside the ticket's own channel.",
            )
            return None

        try:
            config = await self.repo.get_config(guild.id)
            ticket = await self.repo.get_ticket_by_channel(channel.id)
        except Exception:
            log.exception("Could not read the ticket for channel %s.", channel.id)
            await self._reject(
                interaction, "I could not read that ticket. Please try again."
            )
            return None

        if ticket is None:
            await self._reject(interaction, "This channel is not a Fyrion ticket.")
            return None
        if str(ticket.get("status")) == "closed":
            await self._reject(interaction, "That ticket is already closed.")
            return None

        if require_staff and not service.is_support(member, config):
            await self._reject(
                interaction, "Only ticket staff can use that command."
            )
            return None

        return guild, member, channel, ticket, config

    # ------------------------------------------------------------------
    # /ticket-setup
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ticket-setup",
        description="Configure the ticket system and post the ticket panel.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        category="Category the private ticket channels are created in",
        panel_channel="Where the panel is posted (defaults to this channel)",
        support_role="Role granted access to every ticket",
        log_channel="Where closing transcripts are archived",
        topics=(
            "Up to 5 buttons, comma separated. Use 'Label | emoji' to add an "
            "emoji, e.g. 'Bug report | \U0001f41b, Billing | \U0001f4b3'"
        ),
        title="Heading shown above the buttons",
        description="Body text shown above the buttons",
        button_style="Colour used for the topic buttons",
    )
    async def ticket_setup_cmd(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        panel_channel: Optional[discord.TextChannel] = None,
        support_role: Optional[discord.Role] = None,
        log_channel: Optional[discord.TextChannel] = None,
        topics: Optional[app_commands.Range[str, 1, 400]] = None,
        title: Optional[app_commands.Range[str, 1, service.MAX_PANEL_TITLE]] = None,
        description: Optional[
            app_commands.Range[str, 1, service.MAX_PANEL_DESCRIPTION]
        ] = None,
        button_style: ButtonStyleName = "primary",
    ) -> None:
        context = await self._authorize_admin(interaction)
        if context is None:
            return
        guild, member = context

        me = guild.me
        assert me is not None  # guaranteed by _authorize_admin

        if category.guild.id != guild.id:
            await self._reject(
                interaction, "That category does not belong to this server."
            )
            return

        # Ticket channels are created with explicit overwrites, which requires
        # Manage Roles in addition to Manage Channels.
        missing = missing_channel_permissions(
            me, category, view_channel=True, manage_channels=True
        )
        if missing:
            names = ", ".join(f"`{name}`" for name in missing)
            await self._reject(
                interaction, f"I am missing {names} in {category.mention}."
            )
            return
        if not me.guild_permissions.manage_roles:
            await self._reject(
                interaction,
                "I need the `Manage Roles` permission so ticket channels can be "
                "made private.",
            )
            return

        target = panel_channel if panel_channel is not None else interaction.channel
        if not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
            await self._reject(
                interaction,
                "The panel must be posted to a text channel in this server.",
            )
            return

        panel_missing = missing_channel_permissions(
            me, target, view_channel=True, send_messages=True, embed_links=True
        )
        if panel_missing:
            names = ", ".join(f"`{name}`" for name in panel_missing)
            await self._reject(
                interaction, f"I am missing {names} in {target.mention}."
            )
            return

        if support_role is not None:
            if support_role.guild.id != guild.id:
                await self._reject(
                    interaction, "That role does not belong to this server."
                )
                return
            if support_role.is_default():
                await self._reject(
                    interaction,
                    "`@everyone` cannot be the support role \u2014 that would make "
                    "every ticket public.",
                )
                return

        if log_channel is not None:
            if log_channel.guild.id != guild.id:
                await self._reject(
                    interaction, "That log channel does not belong to this server."
                )
                return
            log_missing = missing_channel_permissions(
                me,
                log_channel,
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
            )
            if log_missing:
                names = ", ".join(f"`{name}`" for name in log_missing)
                await self._reject(
                    interaction,
                    f"I am missing {names} in {log_channel.mention}; transcripts "
                    "are uploaded as files.",
                )
                return

        try:
            parsed_topics = service.parse_topics(
                self.bot, topics, style=button_style
            )
        except ValueError as exc:
            await self._reject(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)

        try:
            await self.repo.set_config(
                guild.id,
                category.id,
                log_channel.id if log_channel is not None else None,
                support_role.id if support_role is not None else None,
            )
        except Exception:
            log.exception("Could not store the ticket config for guild %s.", guild.id)
            await self._reject(
                interaction,
                "The ticket configuration could not be saved. Please try again.",
            )
            return

        panel_title = (title or "Support Tickets").strip()
        panel_body = description.strip() if description else None

        config: dict[str, Any] = {
            "title": panel_title,
            "description": panel_body,
            "topics": parsed_topics,
        }

        previous = None
        try:
            previous = await self.repo.get_panel(guild.id)
        except Exception:
            log.exception("Could not read the previous ticket panel.")

        try:
            message = await target.send(
                embed=service.panel_embed(config),
                view=TicketPanelView.for_config(config),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.Forbidden:
            await self._reject(
                interaction, f"Discord refused to post the panel in {target.mention}."
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not post the ticket panel in guild %s: %s", guild.id, exc)
            await self._reject(
                interaction, f"Discord rejected the panel (HTTP {exc.status})."
            )
            return

        try:
            await self.repo.save_panel(
                guild.id,
                channel_id=target.id,
                message_id=message.id,
                title=panel_title,
                description=panel_body,
                topics=parsed_topics,
                created_by=member.id,
            )
        except Exception:
            log.exception("Could not record the ticket panel for guild %s.", guild.id)
            # A panel whose topics are not stored would open the wrong ticket, so
            # the message is withdrawn rather than left live.
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await self._reject(
                interaction,
                "The panel could not be recorded, so it was removed again. "
                "Please try once more.",
            )
            return

        await self._retire_old_panel(guild, previous, message.id)

        await self._audit(
            guild,
            member,
            "Panel configured",
            f"Category: {category.name} (`{category.id}`)\n"
            f"Panel: #{target.name} (`{target.id}`)\n"
            f"Support role: "
            + (
                f"{support_role.name} (`{support_role.id}`)"
                if support_role is not None
                else "none"
            )
            + "\nLog channel: "
            + (f"#{log_channel.name}" if log_channel is not None else "none")
            + f"\nTopics: {len(parsed_topics)}",
        )

        lines = [
            f"\u2705 Ticket panel posted in {target.mention}.",
            f"Tickets are created in **{category.name}** with "
            f"{len(parsed_topics)} topic button(s): "
            + ", ".join(f"`{topic['label']}`" for topic in parsed_topics),
        ]
        if support_role is not None:
            lines.append(f"{support_role.mention} is granted access to every ticket.")
        else:
            lines.append(
                "\u26a0\ufe0f No support role is set, so only members who can "
                "manage channels will see new tickets."
            )
        if log_channel is not None:
            lines.append(f"Closing transcripts are archived in {log_channel.mention}.")
        else:
            lines.append(
                "No transcript channel is set, so closing transcripts fall back "
                "to the server's moderation log channel."
            )

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def _retire_old_panel(
        self,
        guild: discord.Guild,
        previous: dict[str, Any] | None,
        new_message_id: int,
    ) -> None:
        """Removes a superseded panel message, best effort.

        Leaving the old panel in place would present stale topic buttons whose
        slots may no longer be configured.
        """
        if not previous:
            return

        channel_id = previous.get("channel_id")
        message_id = previous.get("message_id")
        if not channel_id or not message_id or int(message_id) == new_message_id:
            return

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return

        me = guild.me
        if me is None or not channel.permissions_for(me).read_message_history:
            return

        try:
            stale = await channel.fetch_message(int(message_id))
            await stale.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except discord.HTTPException as exc:
            log.debug("Could not remove the previous ticket panel: %s", exc)

    # ------------------------------------------------------------------
    # /ticket-close
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ticket-close",
        description="Close this ticket, export a transcript and delete the channel.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        reason="Why the ticket is being closed (included in the transcript)",
        notify_owner="Send the transcript to the member who opened the ticket",
    )
    async def ticket_close_cmd(
        self,
        interaction: discord.Interaction,
        reason: Optional[app_commands.Range[str, 1, MAX_CLOSE_REASON]] = None,
        notify_owner: bool = True,
    ) -> None:
        # Staff is not required up front: the member who opened the ticket may
        # close their own, which is checked below.
        context = await self._ticket_context(interaction, require_staff=False)
        if context is None:
            return
        guild, member, channel, ticket, config = context

        if not (
            service.is_owner(ticket, member) or service.is_support(member, config)
        ):
            await self._reject(
                interaction,
                "Only the member who opened this ticket or a staff member can "
                "close it.",
            )
            return

        # Exporting the history and uploading the transcript regularly outruns
        # the three second interaction window.
        await interaction.response.defer(ephemeral=True)

        result = await service.close_ticket(
            guild=guild,
            channel=channel,
            repo=self.repo,
            closed_by=member,
            reason=reason,
            config=config,
            notify_owner=notify_owner,
        )

        if not result.closed:
            await interaction.followup.send(
                f"\u274c {result.error or 'That ticket could not be closed.'}",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        number = ticket.get("ticket_number")
        label = f"Ticket #{int(number)}" if number else "The ticket"

        lines = [
            f"\u2705 {label} is closed. This channel will be deleted in a few "
            "seconds."
        ]
        if result.log_url:
            lines.append(f"Transcript archived: {result.log_url}")
        else:
            lines.append(
                "\u26a0\ufe0f No transcript channel was available, so the "
                "transcript was stored with the ticket record instead."
            )
        if result.truncated:
            lines.append(
                "\u2139\ufe0f The ticket was long, so only the first "
                f"{service_max_messages()} messages were exported."
            )
        if notify_owner:
            lines.append(
                "The opener received a copy by direct message."
                if result.owner_notified
                else "The opener could not be sent a copy (their DMs are closed)."
            )
        lines.extend(f"\u26a0\ufe0f {warning}" for warning in result.warnings)

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

        await self._audit(
            guild,
            member,
            "Ticket closed",
            f"Channel: #{channel.name} (`{channel.id}`)\n"
            + (f"Ticket: #{int(number)}\n" if number else "")
            + f"Reason: {reason or 'No reason provided'}",
        )

    # ------------------------------------------------------------------
    # /ticket-claim
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ticket-claim",
        description="Claim this ticket so other staff know you are handling it.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        release="Set to True to release a ticket you previously claimed"
    )
    async def ticket_claim_cmd(
        self, interaction: discord.Interaction, release: bool = False
    ) -> None:
        context = await self._ticket_context(interaction)
        if context is None:
            return
        guild, member, channel, ticket, _ = context

        claimed_by = ticket.get("claimed_by")

        if release:
            if not claimed_by:
                await self._respond(
                    interaction, "\u2139\ufe0f This ticket is not claimed."
                )
                return
            # Only the holder, or someone who can manage the server, may hand a
            # ticket back to the queue.
            if int(claimed_by) != member.id and not (
                member.guild_permissions.administrator
                or member.guild_permissions.manage_guild
            ):
                await self._reject(
                    interaction,
                    f"This ticket is claimed by <@{int(claimed_by)}>. Only they "
                    "or a server manager can release it.",
                )
                return

            if not await self.repo.release_ticket(channel.id):
                await self._respond(
                    interaction, "\u2139\ufe0f This ticket is not claimed."
                )
                return

            await self._ok(interaction, "Ticket released back to the queue.")
            await self._announce(
                channel, f"{member.mention} released this ticket.", member
            )
            return

        if claimed_by and int(claimed_by) == member.id:
            await self._respond(
                interaction, "\u2139\ufe0f You have already claimed this ticket."
            )
            return

        if not await self.repo.claim_ticket(channel.id, member.id):
            holder = f"<@{int(claimed_by)}>" if claimed_by else "another staff member"
            await self._respond(
                interaction,
                f"\u2139\ufe0f This ticket is already claimed by {holder}.",
            )
            return

        await self._ok(
            interaction,
            "You claimed this ticket. Other staff can still read it, but they "
            "know it is yours.",
        )
        await self._announce(
            channel, f"{member.mention} claimed this ticket.", member
        )

    async def _announce(
        self,
        channel: discord.TextChannel,
        text: str,
        member: discord.Member,
    ) -> None:
        """Posts a short in-channel notice, best effort."""
        me = channel.guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return

        embed = discord.Embed(
            description=text,
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[member], replied_user=False
                ),
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # /ticket-add and /ticket-remove
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ticket-add", description="Give another member access to this ticket."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to add to this ticket")
    async def ticket_add_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        context = await self._ticket_context(interaction)
        if context is None:
            return
        guild, invoker, channel, ticket, _ = context

        if member.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return
        if member.bot:
            await self._reject(
                interaction, "Bots cannot be added to a ticket this way."
            )
            return

        me = guild.me
        if me is None or not channel.permissions_for(me).manage_channels:
            await self._reject(
                interaction,
                "I need `Manage Channels` in this ticket to change its access.",
            )
            return

        if channel.permissions_for(member).view_channel:
            await self._respond(
                interaction,
                f"\u2139\ufe0f **{member.display_name}** can already see this ticket.",
            )
            return

        # Grants exactly what a participant needs, and nothing that would let
        # them manage the ticket itself.
        overwrite = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            add_reactions=True,
        )

        try:
            await channel.set_permissions(
                member,
                overwrite=overwrite,
                reason=self._audit_reason(invoker, "Added to ticket"),
            )
        except discord.Forbidden:
            await self._reject(
                interaction, "Discord refused the permission change."
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not add member %s to ticket %s: %s", member.id, channel.id, exc)
            await self._reject(
                interaction, f"Discord rejected the change (HTTP {exc.status})."
            )
            return

        await self._ok(
            interaction, f"**{member.display_name}** now has access to this ticket."
        )
        await self._announce(
            channel,
            f"{member.mention} was added to this ticket by {invoker.mention}.",
            member,
        )

    @app_commands.command(
        name="ticket-remove", description="Revoke a member's access to this ticket."
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to remove from this ticket")
    async def ticket_remove_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        context = await self._ticket_context(interaction)
        if context is None:
            return
        guild, invoker, channel, ticket, _ = context

        if member.guild.id != guild.id:
            await self._reject(interaction, "That member is not in this server.")
            return
        if service.is_owner(ticket, member):
            await self._reject(
                interaction,
                "The member who opened this ticket cannot be removed from it. "
                "Close the ticket instead.",
            )
            return

        me = guild.me
        if me is None:
            await self._reject(
                interaction, "I could not resolve my own membership in this server."
            )
            return
        if member.id == me.id:
            await self._reject(interaction, "I cannot remove myself from a ticket.")
            return
        if not channel.permissions_for(me).manage_channels:
            await self._reject(
                interaction,
                "I need `Manage Channels` in this ticket to change its access.",
            )
            return

        if channel.overwrites_for(member).is_empty():
            # No member-specific overwrite exists, so their access (if any) comes
            # from a role and removing it here would achieve nothing.
            visible = channel.permissions_for(member).view_channel
            await self._respond(
                interaction,
                (
                    f"\u2139\ufe0f **{member.display_name}** sees this ticket "
                    "through a role, not a personal override. Adjust that role's "
                    "permissions instead."
                )
                if visible
                else (
                    f"\u2139\ufe0f **{member.display_name}** does not have access "
                    "to this ticket."
                ),
            )
            return

        try:
            await channel.set_permissions(
                member,
                overwrite=None,
                reason=self._audit_reason(invoker, "Removed from ticket"),
            )
        except discord.Forbidden:
            await self._reject(interaction, "Discord refused the permission change.")
            return
        except discord.HTTPException as exc:
            log.warning(
                "Could not remove member %s from ticket %s: %s",
                member.id,
                channel.id,
                exc,
            )
            await self._reject(
                interaction, f"Discord rejected the change (HTTP {exc.status})."
            )
            return

        note = ""
        if channel.permissions_for(member).view_channel:
            note = (
                "\n\u26a0\ufe0f They can still see it through a role; adjust that "
                "role to fully revoke access."
            )

        await self._ok(
            interaction,
            f"Removed **{member.display_name}**'s personal access to this "
            f"ticket.{note}",
        )

    # ------------------------------------------------------------------
    # /ticket-list
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ticket-list", description="List the tickets that are currently open."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(limit="How many tickets to list (1-25)")
    async def ticket_list_cmd(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._reject(
                interaction, "This command can only be used inside a server."
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            config = await self.repo.get_config(guild.id)
        except Exception:
            log.exception("Could not read the ticket config for guild %s.", guild.id)
            await self._reject(interaction, "I could not read the ticket configuration.")
            return

        if not service.is_support(member, config):
            await self._reject(interaction, "Only ticket staff can list tickets.")
            return

        rows = await self.repo.list_tickets(
            guild.id, status=list(service_open_statuses()), limit=limit
        )
        if not rows:
            await interaction.followup.send(
                "\u2139\ufe0f There are no open tickets right now.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        total = await self.repo.count_open(guild.id)

        lines: list[str] = []
        for row in rows:
            number = row.get("ticket_number")
            channel = guild.get_channel(int(row.get("channel_id") or 0))
            location = (
                channel.mention
                if channel is not None
                else f"deleted channel `{row.get('channel_id')}`"
            )
            label = f"**#{int(number)}**" if number else "**Ticket**"
            owner = row.get("user_id")
            claimed = row.get("claimed_by")

            detail = f"{label} {location} \u2022 opened by <@{int(owner)}>"
            if claimed:
                detail += f" \u2022 claimed by <@{int(claimed)}>"
            else:
                detail += " \u2022 unclaimed"
            if row.get("subject"):
                detail += f" \u2022 {str(row['subject'])[:40]}"
            lines.append(detail)

        embed = discord.Embed(
            title="Open tickets",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{total} ticket(s) open in total")

        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )


def service_max_messages() -> int:
    """Returns the transcript message cap, for use in user-facing messages."""
    from fyrion.utils.transcripts import MAX_MESSAGES

    return MAX_MESSAGES


def service_open_statuses() -> tuple[str, ...]:
    """Returns the statuses that count as an open ticket."""
    from fyrion.database.repositories.tickets import OPEN_STATUSES

    return OPEN_STATUSES


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))


__all__ = ["Tickets", "setup"]
