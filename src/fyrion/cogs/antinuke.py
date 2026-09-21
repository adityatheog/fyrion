"""
AntiNuke cog: detects and stops mass-destructive actions by a single actor.

Two cogs live here, mirroring the AutoMod split:

* :class:`AntiNuke` is the detector. It listens on ``on_audit_log_entry_create``
  -- the one gateway event that names the *actor* directly -- and keeps a
  per-``(guild, actor, action)`` sliding window. When one actor crosses a
  guild's threshold for an action inside the window (for example, deleting more
  channels than allowed in 30 seconds) it applies the configured punishment to
  that actor, records a moderation case, and alerts the mod-log channel.
* :class:`AntiNukeCommands` exposes the ``/antinuke`` command group and writes
  through :class:`~fyrion.database.repositories.antinuke.AntiNukeRepository`,
  which bumps a per-guild revision so a config change takes effect on the next
  event rather than after a cache timeout.

Why the audit log, not the raw gateway events: ``on_member_ban`` and friends
tell you *what* happened, not *who* did it. ``on_audit_log_entry_create``
carries the responsible user, which is the whole point of anti-nuke. It needs
the ``View Audit Log`` permission and Discord only emits it for guilds where the
bot can read the audit log.

Safety properties worth stating explicitly:

* AntiNuke acts against server staff, so the guards are correctness-critical.
  The guild owner, Fyrion itself, and every whitelisted actor are never
  punished, and Discord's role hierarchy is respected before any action.
* The punishment path is idempotent. A burst of events that all breach the
  threshold results in exactly one punishment per actor within a cooldown, not
  one punishment per event, because the punished marker is set *before* the
  first ``await`` in the response path.
* Detection is fail-safe: a broken database query caches a disabled policy
  briefly rather than turning into one failing query per audit-log event.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from fyrion.database.repositories.antinuke import (
    ANTINUKE_ACTION_KEYS,
    MAX_THRESHOLD,
    MAX_WINDOW_SECONDS,
    MIN_THRESHOLD,
    MIN_WINDOW_SECONDS,
    AntiNukeRepository,
    threshold_column,
)
from fyrion.database.schema import ANTINUKE_ACTIONS, ANTINUKE_PUNISHMENTS
from fyrion.utils.modlog import send_log
from fyrion.utils.ratelimit import SlidingWindow

log = logging.getLogger("fyrion.cogs.antinuke")

NO_MENTIONS = discord.AllowedMentions.none()

# How long a compiled policy is trusted without re-reading the database. The
# revision counter normally invalidates it sooner; the TTL only covers writes
# that bypass the repository (for example a future dashboard endpoint).
POLICY_TTL_SECONDS = 60.0
# After punishing an actor, ignore further breaches by them for this long so a
# burst of events cannot enqueue N punishments of the same actor.
PUNISH_COOLDOWN_SECONDS = 60.0
# Discord truncates audit log reasons at 512 characters.
AUDIT_REASON_LIMIT = 512

# The single point of truth mapping Discord audit-log actions to the internal
# keys used by thresholds and settings columns. An action not in this map is
# not watched, so adding coverage is a one-line change plus a schema column.
AUDIT_ACTION_KEYS: dict[discord.AuditLogAction, str] = {
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.kick: "kick",
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.channel_create: "channel_create",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.role_create: "role_create",
    discord.AuditLogAction.webhook_create: "webhook_create",
}

# Which moderation_cases action to record for each punishment. ``strip_roles``
# has no dedicated case action, so it is filed as a note carrying the reason.
PUNISHMENT_CASE_ACTION: dict[str, str] = {
    "ban": "ban",
    "kick": "kick",
    "strip_roles": "note",
}

PUNISHMENT_LABELS: dict[str, str] = {
    "strip_roles": "strip roles",
    "kick": "kick",
    "ban": "ban",
}


@dataclass
class AntiNukePolicy:
    """A guild's AntiNuke configuration, pre-parsed for the hot path."""

    generation: int
    expires_at: float
    enabled: bool = False
    window_seconds: float = 30.0
    punishment: str = "strip_roles"
    # action key -> threshold; absent means the action is not watched.
    thresholds: dict[str, int] = field(default_factory=dict)
    whitelist: frozenset[int] = frozenset()


class AntiNuke(commands.Cog):
    """Detects mass-destructive actions via the audit log and stops the actor."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = AntiNukeRepository(self.db)

        self._policies: dict[int, AntiNukePolicy] = {}
        # (guild_id, actor_id, action) -> sliding window of event times.
        self._windows: dict[tuple[int, int, str], SlidingWindow] = {}
        # (guild_id, actor_id) -> monotonic time the last punishment fired.
        self._punished: dict[tuple[int, int], float] = {}

        self._prune_state.start()

    async def cog_unload(self) -> None:
        self._prune_state.cancel()

    # ------------------------------------------------------------------
    # Policy cache
    # ------------------------------------------------------------------

    def invalidate(self, guild_id: int) -> None:
        """Drops the cached policy for one guild."""
        self._policies.pop(int(guild_id), None)

    async def _policy(self, guild_id: int) -> AntiNukePolicy:
        now = time.monotonic()
        generation = AntiNukeRepository.generation(guild_id)

        cached = self._policies.get(guild_id)
        if (
            cached is not None
            and cached.generation == generation
            and cached.expires_at > now
        ):
            return cached

        expires_at = now + POLICY_TTL_SECONDS
        try:
            settings = await self.repo.get_settings(guild_id)
            whitelist = await self.repo.get_whitelist(guild_id)
        except Exception:
            # Cache a disabled policy briefly: a broken database must not turn
            # into one failing query per audit-log event.
            log.exception("Could not load the AntiNuke policy for guild %s.", guild_id)
            policy = AntiNukePolicy(generation=generation, expires_at=expires_at)
            self._policies[guild_id] = policy
            return policy

        if settings is None or not settings.get("enabled"):
            policy = AntiNukePolicy(
                generation=generation,
                expires_at=expires_at,
                whitelist=whitelist,
            )
            self._policies[guild_id] = policy
            return policy

        thresholds: dict[str, int] = {}
        for action in ANTINUKE_ACTION_KEYS:
            raw = settings.get(threshold_column(action))
            if raw is not None:
                thresholds[action] = int(raw)

        policy = AntiNukePolicy(
            generation=generation,
            expires_at=expires_at,
            enabled=True,
            window_seconds=float(settings.get("window_seconds") or 30),
            punishment=str(settings.get("punishment") or "strip_roles"),
            thresholds=thresholds,
            whitelist=whitelist,
        )
        self._policies[guild_id] = policy
        return policy

    @tasks.loop(minutes=5)
    async def _prune_state(self) -> None:
        """Drops idle windows, stale policies and expired punishment markers.

        Without this the dictionaries would grow with every actor who has ever
        performed a watched action in any guild.
        """
        now = time.monotonic()

        # A window is idle once every recorded event has aged past the widest
        # possible detection window, so no future count could reference it.
        stale_windows = [
            key
            for key, window in self._windows.items()
            if window.is_idle(now, float(MAX_WINDOW_SECONDS))
        ]
        for key in stale_windows:
            del self._windows[key]

        stale_marks = [
            key
            for key, at in self._punished.items()
            if now - at > PUNISH_COOLDOWN_SECONDS
        ]
        for key in stale_marks:
            del self._punished[key]

        stale_policies = [
            guild_id
            for guild_id, policy in self._policies.items()
            if policy.expires_at <= now
        ]
        for guild_id in stale_policies:
            del self._policies[guild_id]

        if stale_windows or stale_marks or stale_policies:
            log.debug(
                "AntiNuke pruned %d window(s), %d mark(s), %d policy snapshot(s).",
                len(stale_windows),
                len(stale_marks),
                len(stale_policies),
            )

    @_prune_state.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        if guild is None:
            return

        action_key = AUDIT_ACTION_KEYS.get(entry.action)
        if action_key is None:
            return

        actor_id = getattr(entry, "user_id", None)
        if actor_id is None and entry.user is not None:
            actor_id = entry.user.id
        if actor_id is None:
            return

        await self.handle_action(guild, int(actor_id), action_key)

    async def handle_action(
        self, guild: discord.Guild, actor_id: int, action_key: str
    ) -> bool:
        """Records one watched action and punishes on threshold breach.

        Returns True when this call triggered a punishment. Split out from the
        listener so a test can drive the detection logic with a fake event
        stream and a fake guild.
        """
        policy = await self._policy(guild.id)
        if not policy.enabled:
            return False

        threshold = policy.thresholds.get(action_key)
        if threshold is None:
            return False  # this action is not watched for this guild

        if self._is_exempt(guild, actor_id, policy):
            return False

        now = time.monotonic()
        key = (guild.id, actor_id, action_key)
        window = self._windows.get(key)
        if window is None:
            window = SlidingWindow()
            self._windows[key] = window

        count = window.hit(now, policy.window_seconds)
        if count < threshold:
            return False

        return await self._respond(guild, actor_id, action_key, count, policy)

    # ------------------------------------------------------------------
    # Exemptions
    # ------------------------------------------------------------------

    def _is_exempt(
        self, guild: discord.Guild, actor_id: int, policy: AntiNukePolicy
    ) -> bool:
        """The owner, Fyrion, and whitelisted actors are never actioned."""
        if actor_id == guild.owner_id:
            return True
        me = guild.me
        if me is not None and actor_id == me.id:
            return True
        if self.bot.user is not None and actor_id == self.bot.user.id:
            return True
        return actor_id in policy.whitelist

    @staticmethod
    def _can_act(me: discord.Member, member: discord.Member) -> bool:
        """True when the bot outranks the member and may act on them."""
        if member.id == me.id:
            return False
        if member.id == member.guild.owner_id:
            return False
        return me.top_role > member.top_role

    # ------------------------------------------------------------------
    # Response
    # ------------------------------------------------------------------

    async def _respond(
        self,
        guild: discord.Guild,
        actor_id: int,
        action_key: str,
        count: int,
        policy: AntiNukePolicy,
    ) -> bool:
        """Punishes the actor once, records the case and alerts the mod-log."""
        punish_key = (guild.id, actor_id)
        now = time.monotonic()

        last = self._punished.get(punish_key)
        if last is not None and now - last < PUNISH_COOLDOWN_SECONDS:
            # Already handling this actor; a burst must not stack punishments.
            return False

        # Claim the actor *before* any await. asyncio only switches tasks at an
        # await point, so setting the marker synchronously here makes the whole
        # punishment path idempotent against a flood of concurrent events.
        self._punished[punish_key] = now

        # Drop this actor's counters so post-punishment noise cannot re-trigger.
        for existing in list(self._windows):
            if existing[0] == guild.id and existing[1] == actor_id:
                del self._windows[existing]

        label = ANTINUKE_ACTIONS.get(action_key, action_key)
        reason = (
            f"AntiNuke: {count} {label.lower()} within "
            f"{policy.window_seconds:g}s by one actor"
        )

        outcome = await self._apply_punishment(guild, actor_id, policy, reason)
        await self._record_case(guild, actor_id, policy.punishment, reason)
        await self._alert(guild, actor_id, action_key, count, policy, outcome)

        log.warning(
            "AntiNuke breach in guild %s by actor %s (%s x%d): %s",
            guild.id,
            actor_id,
            action_key,
            count,
            outcome,
        )
        return True

    async def _apply_punishment(
        self,
        guild: discord.Guild,
        actor_id: int,
        policy: AntiNukePolicy,
        reason: str,
    ) -> str:
        """Applies the configured punishment. Returns what actually happened."""
        me = guild.me
        if me is None:
            return "skipped (guild state unavailable)"

        punishment = policy.punishment
        member = guild.get_member(actor_id)
        audit_reason = reason[:AUDIT_REASON_LIMIT]

        # A member who already left can still be banned by id, but never
        # kicked or stripped. The owner/bot are excluded upstream; re-check
        # hierarchy here as defense in depth.
        if member is not None and not self._can_act(me, member):
            return f"{punishment} skipped (role hierarchy)"

        if punishment == "ban":
            if not me.guild_permissions.ban_members:
                return "ban skipped (missing Ban Members)"
            try:
                await guild.ban(
                    discord.Object(id=actor_id),
                    reason=audit_reason,
                    delete_message_seconds=0,
                )
            except discord.Forbidden:
                return "ban refused by Discord"
            except discord.HTTPException as exc:
                log.warning(
                    "AntiNuke ban failed for %s in %s: %s", actor_id, guild.id, exc
                )
                return "ban failed"
            return "actor banned"

        if member is None:
            return f"{punishment} skipped (actor not in guild)"

        if punishment == "kick":
            if not me.guild_permissions.kick_members:
                return "kick skipped (missing Kick Members)"
            try:
                await member.kick(reason=audit_reason)
            except discord.Forbidden:
                return "kick refused by Discord"
            except discord.HTTPException as exc:
                log.warning(
                    "AntiNuke kick failed for %s in %s: %s", actor_id, guild.id, exc
                )
                return "kick failed"
            return "actor kicked"

        # strip_roles: remove every role the bot can manage, defanging the
        # actor without removing them, so an operator can review afterwards.
        if not me.guild_permissions.manage_roles:
            return "strip_roles skipped (missing Manage Roles)"

        removable = [
            role
            for role in member.roles
            if not role.is_default() and not role.managed and role < me.top_role
        ]
        if not removable:
            return "no removable roles"
        try:
            await member.remove_roles(*removable, reason=audit_reason)
        except discord.Forbidden:
            return "strip_roles refused by Discord"
        except discord.HTTPException as exc:
            log.warning(
                "AntiNuke role strip failed for %s in %s: %s",
                actor_id,
                guild.id,
                exc,
            )
            return "strip_roles failed"
        return f"stripped {len(removable)} role(s)"

    async def _record_case(
        self,
        guild: discord.Guild,
        actor_id: int,
        punishment: str,
        reason: str,
    ) -> None:
        """Writes the AntiNuke response into the moderation case history."""
        create = getattr(self.db, "create_moderation_case", None)
        if create is None:
            return

        member = guild.get_member(actor_id)
        moderator_id = self.bot.user.id if self.bot.user is not None else actor_id
        try:
            await create(
                guild_id=guild.id,
                action=PUNISHMENT_CASE_ACTION.get(punishment, "note"),
                target_id=actor_id,
                target_tag=str(member) if member is not None else None,
                moderator_id=moderator_id,
                reason=reason[:AUDIT_REASON_LIMIT],
                evidence="Automatic action by Fyrion AntiNuke",
            )
        except Exception:
            log.exception(
                "Could not record the AntiNuke case for %s in %s.",
                actor_id,
                guild.id,
            )

    async def _alert(
        self,
        guild: discord.Guild,
        actor_id: int,
        action_key: str,
        count: int,
        policy: AntiNukePolicy,
        outcome: str,
    ) -> None:
        """Mirrors the breach into the guild's moderation log channel."""
        member = guild.get_member(actor_id)
        actor = f"{member} (`{actor_id}`)" if member is not None else f"`{actor_id}`"

        embed = discord.Embed(
            title="AntiNuke Triggered",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Actor", value=actor, inline=False)
        embed.add_field(
            name="Trigger",
            value=(
                f"{count} × {ANTINUKE_ACTIONS.get(action_key, action_key)} "
                f"within {policy.window_seconds:g}s"
            ),
            inline=False,
        )
        embed.add_field(
            name="Punishment",
            value=f"{PUNISHMENT_LABELS.get(policy.punishment, policy.punishment)} — {outcome}",
            inline=False,
        )
        try:
            await send_log(self.db, guild, embed)
        except Exception:
            log.exception("Could not post the AntiNuke alert for %s.", guild.id)


# ---------------------------------------------------------------------------
# Configuration commands
# ---------------------------------------------------------------------------


ActionChoice = app_commands.Choice
_ACTION_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in ANTINUKE_ACTIONS.items()
]
_PUNISHMENT_CHOICES = [
    app_commands.Choice(name=PUNISHMENT_LABELS[value], value=value)
    for value in ("strip_roles", "kick", "ban")
]


class AntiNukeCommands(commands.Cog):
    """AntiNuke configuration commands under the ``/antinuke`` group."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = AntiNukeRepository(self.db)

    antinuke = app_commands.Group(
        name="antinuke",
        description="Configure protection against mass-destructive actions.",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def _invalidate(self, guild_id: int) -> None:
        """Drops the detector's cached policy so a change applies at once."""
        cog = self.bot.get_cog("AntiNuke")
        if isinstance(cog, AntiNuke):
            cog.invalidate(guild_id)

    async def _reply(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def _authorize(
        self, interaction: discord.Interaction
    ) -> discord.Guild | None:
        """Re-checks ``Administrator`` server-side before any config change.

        ``default_permissions`` only sets Discord's client-side default and a
        server admin can override it through Integrations, so it is never the
        authoritative gate. Arming or disarming nuke protection is the most
        sensitive action here, so the invoker must genuinely hold
        ``Administrator``. Returns the guild when the command may proceed,
        otherwise replies with the refusal and returns ``None``.
        """
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await self._reply(
                interaction, "❌ This command can only be used inside a server."
            )
            return None

        if not member.guild_permissions.administrator:
            await self._reply(
                interaction,
                "❌ You need the `Administrator` permission to configure AntiNuke.",
            )
            return None

        return guild

    @antinuke.command(name="enable", description="Turn AntiNuke protection on or off.")
    @app_commands.describe(enabled="True arms detection, False disarms it")
    async def enable_cmd(self, interaction: discord.Interaction, enabled: bool) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        await self.repo.set_enabled(guild_id, enabled)
        self._invalidate(guild_id)
        state = "enabled" if enabled else "disabled"
        note = "" if enabled else "\nThresholds are kept for when you re-enable it."
        await self._reply(interaction, f"✅ AntiNuke is now **{state}**.{note}")

    @antinuke.command(
        name="threshold",
        description="Set how many of an action by one actor trigger a response.",
    )
    @app_commands.describe(
        action="Which action to watch",
        count="Actions by one actor within the window that trigger the punishment",
    )
    @app_commands.choices(action=_ACTION_CHOICES)
    async def threshold_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        count: app_commands.Range[int, MIN_THRESHOLD, MAX_THRESHOLD],
    ) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        try:
            await self.repo.set_threshold(guild_id, action.value, int(count))
        except ValueError as exc:
            await self._reply(interaction, f"❌ {exc}")
            return
        self._invalidate(guild_id)
        await self._reply(
            interaction,
            f"✅ {action.name} threshold set to **{count}** per window.",
        )

    @antinuke.command(
        name="unwatch", description="Stop watching one action (clears its threshold)."
    )
    @app_commands.describe(action="Which action to stop watching")
    @app_commands.choices(action=_ACTION_CHOICES)
    async def unwatch_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        await self.repo.clear_threshold(guild_id, action.value)
        self._invalidate(guild_id)
        await self._reply(interaction, f"✅ No longer watching **{action.name}**.")

    @antinuke.command(name="window", description="Set the detection window in seconds.")
    @app_commands.describe(seconds="Length of the sliding detection window")
    async def window_cmd(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS],
    ) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        try:
            await self.repo.save_settings(guild_id, {"window_seconds": int(seconds)})
        except ValueError as exc:
            await self._reply(interaction, f"❌ {exc}")
            return
        self._invalidate(guild_id)
        await self._reply(interaction, f"✅ Detection window set to **{seconds}s**.")

    @antinuke.command(
        name="punishment",
        description="Choose what happens to an actor who trips a threshold.",
    )
    @app_commands.describe(punishment="strip roles, kick, or ban the offending actor")
    @app_commands.choices(punishment=_PUNISHMENT_CHOICES)
    async def punishment_cmd(
        self,
        interaction: discord.Interaction,
        punishment: app_commands.Choice[str],
    ) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        await self.repo.save_settings(guild_id, {"punishment": punishment.value})
        self._invalidate(guild_id)
        await self._reply(
            interaction,
            f"✅ Offending actors will now be **{punishment.name}**.",
        )

    @antinuke.command(
        name="trust", description="Exempt a trusted user or bot from AntiNuke."
    )
    @app_commands.describe(actor="The user or bot to trust")
    async def trust_cmd(
        self, interaction: discord.Interaction, actor: discord.User
    ) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        added = await self.repo.add_whitelist(
            guild_id, actor.id, added_by=interaction.user.id
        )
        self._invalidate(guild_id)
        if added:
            await self._reply(
                interaction, f"✅ {actor.mention} is now trusted by AntiNuke."
            )
        else:
            await self._reply(interaction, f"ℹ️ {actor.mention} was already trusted.")

    @antinuke.command(
        name="untrust", description="Remove a user or bot from the trusted list."
    )
    @app_commands.describe(actor="The user or bot to stop trusting")
    async def untrust_cmd(
        self, interaction: discord.Interaction, actor: discord.User
    ) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id
        removed = await self.repo.remove_whitelist(guild_id, actor.id)
        self._invalidate(guild_id)
        if removed:
            await self._reply(interaction, f"✅ {actor.mention} is no longer trusted.")
        else:
            await self._reply(interaction, f"ℹ️ {actor.mention} was not on the list.")

    @antinuke.command(
        name="status", description="Show AntiNuke settings and the trusted list."
    )
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        guild = await self._authorize(interaction)
        if guild is None:
            return
        guild_id = guild.id

        settings = await self.repo.get_settings(guild_id)
        whitelist = await self.repo.get_whitelist(guild_id)

        embed = discord.Embed(
            title="AntiNuke Status",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )

        if settings is None:
            embed.description = (
                "AntiNuke is **unconfigured**. Run `/antinuke enable True` to "
                "arm it with sensible defaults."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        state = "enabled" if settings.get("enabled") else "disabled"
        embed.description = (
            f"Engine: **{state}** • window: "
            f"**{int(settings.get('window_seconds') or 0)}s** • punishment: "
            f"**{PUNISHMENT_LABELS.get(str(settings.get('punishment')), '?')}**"
        )

        lines = []
        for action, label in ANTINUKE_ACTIONS.items():
            raw = settings.get(threshold_column(action))
            value = f"{int(raw)} / window" if raw is not None else "not watched"
            lines.append(f"**{label}**: {value}")
        embed.add_field(name="Thresholds", value="\n".join(lines), inline=False)

        trusted = (
            ", ".join(f"<@{actor}>" for actor in sorted(whitelist))
            if whitelist
            else "none"
        )
        embed.add_field(name="Trusted actors", value=trusted, inline=False)

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiNuke(bot))
    await bot.add_cog(AntiNukeCommands(bot))
