"""
AutoMod cog: real-time message interception and rule configuration.

Two cogs live here:

* :class:`AutoMod` is the interceptor. It runs on ``on_message`` and
  ``on_message_edit`` for every guild message, so it is written to be cheap:
  a cached policy lookup, an exemption test, then the enabled filters in
  increasing cost order.
* :class:`AutoModCommands` exposes the ``/automod-*`` commands and writes
  through :class:`~fyrion.database.repositories.automod_rules.AutoModRuleRepository`,
  which bumps a per-guild revision counter so configuration changes take effect
  on the very next message instead of after a cache timeout.

Filter order (cheapest first, first match wins):

1. ``spam``    - per-member token bucket, escalating on repeated strikes
2. ``invite``  - literal Discord invite hosts
3. ``link``    - any URL
4. ``word``    - operator-supplied word list (escaped, never a raw regex)
5. ``mention`` - user and role mention count
6. ``newline`` - line count
7. ``caps``    - uppercase ratio above a minimum length

Exemptions are evaluated before any filter runs: bots and webhooks, the guild
owner, members holding ``Manage Messages``, and the whitelisted users, roles and
channels. Thread messages also inherit their parent channel's exemption.

Safety properties worth stating explicitly:

* Message content is attacker controlled. It is never re-sent, only excerpted
  into a fenced embed field with mentions defused, and every reply Fyrion makes
  disables mention parsing except for the single user being addressed.
* Punishments respect Discord's role hierarchy and the bot's own permissions.
  A rule that cannot be enforced is reported in the audit log rather than
  retried or silently dropped.
* Word lists are ``re.escape``-d before compilation, so no operator input is
  ever interpreted as a pattern.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from fyrion.database.repositories.automod_rules import (
    ACTIONS,
    MAX_WORD_LENGTH,
    MAX_WORDS,
    RULE_LABELS,
    RULE_TYPES,
    AutoModRuleRepository,
    dump_options,
    id_set,
    load_options,
    normalize_words,
)
from fyrion.utils.modlog import send_log
from fyrion.utils.ratelimit import TokenBucket
from fyrion.utils.patterns import (
    CONTENT_SCAN_LIMIT,
    INVITE_REGEX,
    URL_REGEX,
    caps_ratio,
    count_lines,
    count_mentions,
    excerpt,
)

log = logging.getLogger("fyrion.cogs.automod")

NO_MENTIONS = discord.AllowedMentions.none()
# Notices address exactly one member, so user mentions are allowed and nothing
# else is: a crafted message can never make Fyrion ping a role or @everyone.
NOTICE_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)

# Order the filters are evaluated in.
EVALUATION_ORDER: tuple[str, ...] = (
    "spam",
    "invite",
    "link",
    "word",
    "mention",
    "newline",
    "caps",
)

# How long a compiled policy is trusted without re-reading the database. The
# revision counter normally invalidates it sooner; the TTL only covers writes
# that bypass the repository (for example a future dashboard endpoint).
POLICY_TTL_SECONDS = 60.0
# How long an idle token bucket is kept before the pruning loop drops it.
BUCKET_IDLE_TTL_SECONDS = 300.0
# Lifetime of the in-channel violation notice.
NOTICE_DELETE_AFTER = 6.0
# Discord rejects communication timeouts longer than 28 days.
MAX_TIMEOUT_SECONDS = 28 * 24 * 3600
MIN_TIMEOUT_SECONDS = 60
# Discord truncates audit log reasons at 512 characters.
AUDIT_REASON_LIMIT = 512
# Excerpt of the offending message included in the audit embed.
LOG_EXCERPT_LIMIT = 900

ActionLiteral = Literal["log", "delete", "warn", "timeout", "kick", "ban"]


def _as_int(value: Any, default: int) -> int:
    """Coerces a stored value to int, falling back on anything unusable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fence(text: str) -> str:
    """Wraps user supplied text so markdown and mentions stay inert."""
    safe = text[:LOG_EXCERPT_LIMIT].replace("```", "`\u200b``")
    safe = safe.replace("@", "@\u200b")
    return f"```\n{safe}\n```"


def compile_words(words: list[str], *, whole_word: bool) -> re.Pattern[str] | None:
    """Builds a matcher for a word list.

    Every entry is ``re.escape``-d, so an operator cannot inject a pattern (and
    therefore cannot inject a catastrophically backtracking one). Word-boundary
    mode uses lookarounds rather than ``\\b`` so entries containing punctuation
    still behave sensibly.
    """
    if not words:
        return None

    alternation = "|".join(re.escape(word) for word in words)
    if whole_word:
        expression = rf"(?<!\w)(?:{alternation})(?!\w)"
    else:
        expression = f"(?:{alternation})"

    try:
        return re.compile(expression, re.IGNORECASE)
    except re.error:  # pragma: no cover - escaped input cannot fail to compile
        log.exception("Could not compile the AutoMod word filter.")
        return None


# The message-spam limiter lives in ``utils.ratelimit`` so AntiNuke can reuse it.
_TokenBucket = TokenBucket


@dataclass
class GuildPolicy:
    """A guild's AutoMod configuration, pre-parsed for the hot path."""

    generation: int
    expires_at: float
    enabled: bool = False
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    options: dict[str, dict[str, Any]] = field(default_factory=dict)
    rule_exempt_roles: dict[str, frozenset[int]] = field(default_factory=dict)
    rule_exempt_channels: dict[str, frozenset[int]] = field(default_factory=dict)
    rule_exempt_users: dict[str, frozenset[int]] = field(default_factory=dict)
    exempt_roles: frozenset[int] = frozenset()
    exempt_channels: frozenset[int] = frozenset()
    exempt_users: frozenset[int] = frozenset()
    word_matcher: Optional[re.Pattern[str]] = None

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.rules)


class AutoMod(commands.Cog):
    """Real-time message filtering: spam, invites, links, words, mentions, caps."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = AutoModRuleRepository(self.db)

        self._policies: dict[int, GuildPolicy] = {}
        # (guild_id, user_id) -> bucket
        self._buckets: dict[tuple[int, int], _TokenBucket] = {}

        self._prune_state.start()

    async def cog_unload(self) -> None:
        self._prune_state.cancel()

    # ------------------------------------------------------------------
    # Policy cache
    # ------------------------------------------------------------------

    def invalidate(self, guild_id: int) -> None:
        """Drops the cached policy for one guild."""
        self._policies.pop(int(guild_id), None)

    async def _policy(self, guild_id: int) -> GuildPolicy:
        now = time.monotonic()
        generation = AutoModRuleRepository.generation(guild_id)

        cached = self._policies.get(guild_id)
        if (
            cached is not None
            and cached.generation == generation
            and cached.expires_at > now
        ):
            return cached

        expires_at = now + POLICY_TTL_SECONDS
        try:
            enabled = await self.repo.get_master(guild_id)
            rows = await self.repo.get_rules(guild_id, enabled_only=True)
            whitelist = await self.repo.get_whitelist(guild_id)
        except Exception:
            # Cache a disabled policy briefly: a broken database must not turn
            # into one failing query per message.
            log.exception("Could not load the AutoMod policy for guild %s.", guild_id)
            policy = GuildPolicy(generation=generation, expires_at=expires_at)
            self._policies[guild_id] = policy
            return policy

        policy = GuildPolicy(
            generation=generation,
            expires_at=expires_at,
            enabled=enabled,
            exempt_users=whitelist.get("user", frozenset()),
            exempt_roles=whitelist.get("role", frozenset()),
            exempt_channels=whitelist.get("channel", frozenset()),
        )

        for row in rows:
            rule_type = str(row.get("rule_type") or "")
            if rule_type not in RULE_TYPES:
                continue

            policy.rules[rule_type] = row
            options = load_options(row.get("pattern"))
            policy.options[rule_type] = options
            policy.rule_exempt_roles[rule_type] = id_set(row.get("exempt_roles"))
            policy.rule_exempt_channels[rule_type] = id_set(row.get("exempt_channels"))
            policy.rule_exempt_users[rule_type] = id_set(row.get("exempt_users"))

            if rule_type == "word":
                policy.word_matcher = compile_words(
                    normalize_words(options.get("words") or []),
                    whole_word=bool(options.get("whole_word", True)),
                )

        if policy.word_matcher is None:
            # An armed word filter with no words would scan every message for
            # nothing, so drop it from the evaluation set.
            policy.rules.pop("word", None)

        self._policies[guild_id] = policy
        return policy

    @tasks.loop(minutes=5)
    async def _prune_state(self) -> None:
        """Drops idle token buckets and expired policy snapshots.

        Without this the bucket dictionary would grow with every member who has
        ever spoken in any guild.
        """
        now = time.monotonic()

        stale_buckets = [
            key
            for key, bucket in self._buckets.items()
            if bucket.is_idle(now, BUCKET_IDLE_TTL_SECONDS)
        ]
        for key in stale_buckets:
            del self._buckets[key]

        stale_policies = [
            guild_id
            for guild_id, policy in self._policies.items()
            if policy.expires_at <= now
        ]
        for guild_id in stale_policies:
            del self._policies[guild_id]

        if stale_buckets or stale_policies:
            log.debug(
                "AutoMod pruned %d bucket(s) and %d policy snapshot(s).",
                len(stale_buckets),
                len(stale_policies),
            )

    @_prune_state.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.inspect(message, count_spam=True)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        # Re-scan edits so a clean message cannot be turned into an invite after
        # the fact. Edits do not consume spam tokens.
        if before.content == after.content:
            return
        await self.inspect(after, count_spam=False)

    async def inspect(self, message: discord.Message, *, count_spam: bool) -> None:
        """Evaluates one message against the guild's policy."""
        guild = message.guild
        author = message.author

        if guild is None or message.webhook_id is not None:
            return
        if not isinstance(author, discord.Member) or author.bot:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            # System messages (pins, joins, boosts) carry no user content.
            return

        policy = await self._policy(guild.id)
        if not policy.active:
            return

        # Staff who can already delete messages are trusted by definition.
        if author.id == guild.owner_id or author.guild_permissions.manage_messages:
            return
        if self._is_exempt(policy, message, author):
            return

        content = (message.content or "")[:CONTENT_SCAN_LIMIT]

        for rule_type in EVALUATION_ORDER:
            rule = policy.rules.get(rule_type)
            if rule is None:
                continue
            if self._rule_exempt(policy, rule_type, message, author):
                continue

            if rule_type == "spam":
                if count_spam and await self._check_spam(message, rule):
                    return
                continue

            if not content:
                continue

            detail = self._match(rule_type, rule, policy, content)
            if detail is None:
                continue

            outcome = await self._punish(
                message, rule, f"AutoMod: {RULE_LABELS[rule_type].lower()}"
            )
            await self._write_log(message, rule_type, detail, outcome)
            return

    # ------------------------------------------------------------------
    # Exemptions
    # ------------------------------------------------------------------

    @staticmethod
    def _channel_ids(message: discord.Message) -> tuple[int, ...]:
        """Returns the channel and, for threads, its parent channel."""
        channel = message.channel
        ids = [channel.id]
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            ids.append(int(parent_id))
        return tuple(ids)

    def _is_exempt(
        self, policy: GuildPolicy, message: discord.Message, author: discord.Member
    ) -> bool:
        if author.id in policy.exempt_users:
            return True
        if policy.exempt_channels and any(
            channel_id in policy.exempt_channels
            for channel_id in self._channel_ids(message)
        ):
            return True
        if policy.exempt_roles and any(
            role.id in policy.exempt_roles for role in author.roles
        ):
            return True
        return False

    def _rule_exempt(
        self,
        policy: GuildPolicy,
        rule_type: str,
        message: discord.Message,
        author: discord.Member,
    ) -> bool:
        """Honors the per-rule exemption lists stored on the rule row."""
        users = policy.rule_exempt_users.get(rule_type)
        if users and author.id in users:
            return True

        channels = policy.rule_exempt_channels.get(rule_type)
        if channels and any(
            channel_id in channels for channel_id in self._channel_ids(message)
        ):
            return True

        roles = policy.rule_exempt_roles.get(rule_type)
        if roles and any(role.id in roles for role in author.roles):
            return True
        return False

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _match(
        self,
        rule_type: str,
        rule: dict[str, Any],
        policy: GuildPolicy,
        content: str,
    ) -> str | None:
        """Returns a human-readable trigger description, or None on no match."""
        if rule_type == "invite":
            found = INVITE_REGEX.search(content)
            return f"invite `{excerpt(found.group(0))}`" if found else None

        if rule_type == "link":
            found = URL_REGEX.search(content)
            return f"link `{excerpt(found.group(0))}`" if found else None

        if rule_type == "word":
            matcher = policy.word_matcher
            if matcher is None:
                return None
            found = matcher.search(content)
            return f"blocked word `{excerpt(found.group(0), 40)}`" if found else None

        if rule_type == "mention":
            limit = max(1, _as_int(rule.get("threshold"), 5))
            count = count_mentions(content)
            if count > limit:
                return f"{count} mention(s), limit {limit}"
            return None

        if rule_type == "newline":
            limit = max(1, _as_int(rule.get("threshold"), 10))
            lines = count_lines(content)
            if lines > limit:
                return f"{lines} line(s), limit {limit}"
            return None

        if rule_type == "caps":
            threshold = min(100, max(1, _as_int(rule.get("threshold"), 70)))
            options = policy.options.get("caps", {})
            minimum = max(1, _as_int(options.get("min_length"), 10))
            letters, ratio = caps_ratio(content)
            if letters >= minimum and ratio >= threshold:
                return (
                    f"{ratio:.0f}% uppercase over {letters} letters, "
                    f"limit {threshold}%"
                )
            return None

        return None

    async def _check_spam(
        self, message: discord.Message, rule: dict[str, Any]
    ) -> bool:
        """Applies the token bucket. Returns True when the message was actioned."""
        guild = message.guild
        if guild is None:
            return False

        limit = _as_int(rule.get("threshold"), 5)
        interval = float(_as_int(rule.get("interval_seconds"), 5))
        if limit <= 0 or interval <= 0:
            return False

        now = time.monotonic()
        key = (guild.id, message.author.id)

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(tokens=float(limit), updated_at=now)
            self._buckets[key] = bucket

        if bucket.consume(float(limit), limit / interval, now):
            return False

        # Strikes expire after a few refill windows, so an occasional burst
        # never escalates on its own.
        strikes = bucket.register_strike(now, interval * 3)
        strike_limit = max(1, _as_int(rule.get("strike_limit"), 3))
        action = str(rule.get("action") or "delete")

        if action != "log":
            await self._delete(message, "AutoMod: spam")

        detail = (
            f"{limit} message(s) per {interval:g}s exceeded "
            f"(strike {strikes}/{strike_limit})"
        )

        if action == "log":
            outcome = "logged (no action taken)"
        elif strikes >= strike_limit and action != "delete":
            bucket.strikes = 0
            escalation = await self._escalate(
                message, rule, "AutoMod: repeated spam"
            )
            outcome = f"message deleted, {escalation}"
        else:
            outcome = "message deleted"
            if strikes == 1:
                # Only warn on the first strike; repeating it would itself spam.
                await self._notify(
                    message,
                    f"\u26a0\ufe0f {message.author.mention}, slow down \u2014 you "
                    "are sending messages too quickly.",
                )

        await self._write_log(message, "spam", detail, outcome)
        return True

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    async def _punish(
        self, message: discord.Message, rule: dict[str, Any], reason: str
    ) -> str:
        """Applies a rule's configured action. Returns what actually happened."""
        action = str(rule.get("action") or "delete")
        if action == "log":
            return "logged (no action taken)"

        await self._delete(message, reason)

        if action == "delete":
            await self._notify(
                message,
                f"\u26a0\ufe0f {message.author.mention}, your message was removed "
                "by AutoMod.",
            )
            return "message deleted"

        escalation = await self._escalate(message, rule, reason)
        return f"message deleted, {escalation}"

    async def _escalate(
        self, message: discord.Message, rule: dict[str, Any], reason: str
    ) -> str:
        """Applies warn/timeout/kick/ban, respecting hierarchy and permissions."""
        action = str(rule.get("action") or "delete")
        member = message.author
        if not isinstance(member, discord.Member):
            return "escalation skipped (member left)"

        guild = member.guild
        me = guild.me
        audit_reason = reason[:AUDIT_REASON_LIMIT]

        if action == "warn":
            await self._notify(
                message,
                f"\u26a0\ufe0f {member.mention}, that message broke this server's "
                "AutoMod rules. Please review the rules before posting again.",
            )
            await self._record_case(guild, "warn", member, reason)
            return "member warned"

        if me is None:
            return f"{action} skipped (guild state unavailable)"
        if not self._can_act(me, member):
            return f"{action} skipped (role hierarchy)"

        if action == "timeout":
            if not me.guild_permissions.moderate_members:
                return "timeout skipped (missing Moderate Members)"
            seconds = min(
                MAX_TIMEOUT_SECONDS,
                max(
                    MIN_TIMEOUT_SECONDS,
                    _as_int(rule.get("duration_seconds"), 300),
                ),
            )
            try:
                await member.timeout(timedelta(seconds=seconds), reason=audit_reason)
            except discord.Forbidden:
                return "timeout refused by Discord"
            except discord.HTTPException as exc:
                log.warning(
                    "AutoMod timeout failed for %s in %s: %s",
                    member.id,
                    guild.id,
                    exc,
                )
                return "timeout failed"

            await self._record_case(
                guild, "timeout", member, reason, duration_seconds=seconds
            )
            await self._notify(
                message,
                f"\u23f1\ufe0f {member.mention} has been timed out for "
                f"{seconds} second(s) by AutoMod.",
            )
            return f"timed out for {seconds}s"

        if action == "kick":
            if not me.guild_permissions.kick_members:
                return "kick skipped (missing Kick Members)"
            try:
                await member.kick(reason=audit_reason)
            except discord.Forbidden:
                return "kick refused by Discord"
            except discord.HTTPException as exc:
                log.warning(
                    "AutoMod kick failed for %s in %s: %s", member.id, guild.id, exc
                )
                return "kick failed"
            await self._record_case(guild, "kick", member, reason)
            return "member kicked"

        if action == "ban":
            if not me.guild_permissions.ban_members:
                return "ban skipped (missing Ban Members)"
            try:
                await guild.ban(
                    member, reason=audit_reason, delete_message_seconds=0
                )
            except discord.Forbidden:
                return "ban refused by Discord"
            except discord.HTTPException as exc:
                log.warning(
                    "AutoMod ban failed for %s in %s: %s", member.id, guild.id, exc
                )
                return "ban failed"
            await self._record_case(guild, "ban", member, reason)
            return "member banned"

        return "no action"

    @staticmethod
    def _can_act(me: discord.Member, member: discord.Member) -> bool:
        """Returns True when the bot outranks the member and may act on them."""
        if member.id == me.id:
            return False
        if member.id == member.guild.owner_id:
            return False
        return me.top_role > member.top_role

    async def _delete(self, message: discord.Message, reason: str) -> None:
        try:
            await message.delete()
        except discord.Forbidden:
            log.warning(
                "Missing `Manage Messages` to enforce AutoMod in guild %s channel %s.",
                message.guild.id if message.guild else "?",
                message.channel.id,
            )
        except discord.NotFound:
            pass  # Already gone: another moderator or filter got there first.
        except discord.HTTPException as exc:
            log.warning("Failed to delete a message for %s: %s", reason, exc)

    async def _notify(self, message: discord.Message, text: str) -> None:
        """Posts a short, self-deleting notice in the offending channel."""
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        me = message.guild.me if message.guild is not None else None
        if me is not None and not channel.permissions_for(me).send_messages:
            return

        try:
            await channel.send(
                text,
                delete_after=NOTICE_DELETE_AFTER,
                allowed_mentions=NOTICE_MENTIONS,
            )
        except discord.HTTPException:
            pass  # Notices are cosmetic; the enforcement already happened.

    async def _record_case(
        self,
        guild: discord.Guild,
        action: str,
        member: discord.Member,
        reason: str,
        *,
        duration_seconds: int | None = None,
    ) -> None:
        """Writes an AutoMod punishment into the moderation case history."""
        create = getattr(self.db, "create_moderation_case", None)
        if create is None:
            return

        moderator_id = self.bot.user.id if self.bot.user is not None else member.id
        try:
            await create(
                guild_id=guild.id,
                action=action,
                target_id=member.id,
                target_tag=str(member),
                moderator_id=moderator_id,
                reason=reason[:AUDIT_REASON_LIMIT],
                evidence="Automatic action by Fyrion AutoMod",
                duration_seconds=duration_seconds,
            )
        except Exception:
            log.exception(
                "Could not record the AutoMod %s case for %s in %s.",
                action,
                member.id,
                guild.id,
            )

    async def _write_log(
        self,
        message: discord.Message,
        rule_type: str,
        detail: str,
        outcome: str,
    ) -> None:
        """Mirrors a violation into the guild's moderation log channel."""
        guild = message.guild
        if guild is None:
            return

        embed = discord.Embed(
            title=f"AutoMod: {RULE_LABELS.get(rule_type, rule_type)}",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Member",
            value=f"{message.author} (`{message.author.id}`)",
            inline=False,
        )
        embed.add_field(
            name="Channel",
            value=getattr(message.channel, "mention", f"`{message.channel.id}`"),
            inline=False,
        )
        embed.add_field(name="Trigger", value=detail, inline=False)
        embed.add_field(name="Action", value=outcome, inline=False)
        if message.content:
            embed.add_field(
                name="Content", value=_fence(message.content), inline=False
            )

        await send_log(self.db, guild, embed)


# ---------------------------------------------------------------------------
# Configuration commands
# ---------------------------------------------------------------------------


def _state(value: Any) -> str:
    return "enabled" if value else "disabled"


class AutoModCommands(commands.Cog):
    """AutoMod configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Any = bot.db  # type: ignore[attr-defined]
        self.repo = AutoModRuleRepository(self.db)

    wordfilter = app_commands.Group(
        name="automod-wordfilter",
        description="Manage the AutoMod banned word list.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _invalidate(self, guild_id: int) -> None:
        """Drops the interceptor's cached policy so a change applies at once."""
        cog = self.bot.get_cog("AutoMod")
        if isinstance(cog, AutoMod):
            cog.invalidate(guild_id)

    @staticmethod
    def _describe(rule: dict[str, Any] | None) -> str:
        """Renders one rule's configuration for a reply or the status embed."""
        if rule is None:
            return "not configured"

        rule_type = str(rule.get("rule_type") or "")
        options = load_options(rule.get("pattern"))
        lines = [f"**{_state(rule.get('enabled'))}** \u2022 action: `{rule.get('action')}`"]

        if rule_type == "spam":
            lines.append(
                f"{_as_int(rule.get('threshold'), 5)} message(s) per "
                f"{_as_int(rule.get('interval_seconds'), 5)}s"
            )
            lines.append(
                f"{_as_int(rule.get('strike_limit'), 3)} strike(s) before escalation"
            )
        elif rule_type == "caps":
            lines.append(
                f"{_as_int(rule.get('threshold'), 70)}% uppercase over "
                f"{_as_int(options.get('min_length'), 10)} letters"
            )
        elif rule_type == "mention":
            lines.append(f"max {_as_int(rule.get('threshold'), 5)} mention(s)")
        elif rule_type == "newline":
            lines.append(f"max {_as_int(rule.get('threshold'), 10)} line(s)")
        elif rule_type == "word":
            words = normalize_words(options.get("words") or [])
            mode = "whole words" if options.get("whole_word", True) else "substrings"
            lines.append(f"{len(words)} word(s), matching {mode}")

        if str(rule.get("action")) in {"timeout"}:
            lines.append(
                f"timeout: {_as_int(rule.get('duration_seconds'), 300)}s"
            )
        return "\n".join(lines)

    async def _save(
        self,
        interaction: discord.Interaction,
        rule_type: str,
        values: dict[str, Any],
        *,
        headline: str,
    ) -> None:
        """Persists a rule change and replies with its effective configuration."""
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "\u274c This command can only be used in a server.", ephemeral=True
            )
            return

        try:
            rule = await self.repo.save_rule(guild_id, rule_type, values)
        except ValueError as exc:
            await interaction.response.send_message(
                f"\u274c {exc}", ephemeral=True
            )
            return

        self._invalidate(guild_id)

        master = await self.repo.get_master(guild_id)
        footer = (
            ""
            if master
            else "\n\u26a0\ufe0f AutoMod is switched off. Run `/automod-enable` "
            "to arm it."
        )
        await interaction.response.send_message(
            f"\u2705 {headline}\n{self._describe(rule)}{footer}",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @staticmethod
    def _base_values(
        enabled: bool,
        action: Optional[str],
        timeout_seconds: Optional[int],
    ) -> dict[str, Any]:
        """Common fields every rule command accepts."""
        values: dict[str, Any] = {"enabled": enabled}
        if action is not None:
            values["action"] = action
        if timeout_seconds is not None:
            values["duration_seconds"] = timeout_seconds
        return values

    # ------------------------------------------------------------------
    # Master switch and status
    # ------------------------------------------------------------------

    @app_commands.command(
        name="automod-enable",
        description="Turn the AutoMod engine on or off for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(enabled="True arms every enabled filter, False disarms all")
    async def enable_cmd(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        await self.repo.set_master(guild_id, enabled)
        self._invalidate(guild_id)

        rules = await self.repo.get_rules(guild_id, enabled_only=True)
        if enabled and not rules:
            note = (
                "\nNo filters are configured yet. Enable one with "
                "`/automod-antilink`, `/automod-antispam` and friends."
            )
        elif enabled:
            names = ", ".join(
                RULE_LABELS.get(str(rule["rule_type"]), str(rule["rule_type"]))
                for rule in rules
            )
            note = f"\nActive filters: {names}."
        else:
            note = "\nEvery filter is now dormant; individual settings are kept."

        await interaction.response.send_message(
            f"\u2705 AutoMod is now **{_state(enabled)}**.{note}", ephemeral=True
        )

    @app_commands.command(
        name="automod-status",
        description="Show every AutoMod filter, its settings and the whitelist.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        master = await self.repo.get_master(guild_id)
        rows = await self.repo.get_rules(guild_id)
        whitelist = await self.repo.get_whitelist(guild_id)

        by_type = {str(row.get("rule_type")): row for row in rows}

        embed = discord.Embed(
            title="AutoMod Status",
            description=f"Engine: **{_state(master)}**",
            color=discord.Color.blurple() if master else discord.Color.dark_grey(),
        )

        for rule_type in RULE_TYPES:
            embed.add_field(
                name=RULE_LABELS[rule_type],
                value=self._describe(by_type.get(rule_type)),
                inline=True,
            )

        roles = whitelist.get("role", frozenset())
        channels = whitelist.get("channel", frozenset())
        users = whitelist.get("user", frozenset())

        embed.add_field(
            name=f"Exempt roles ({len(roles)})",
            value=", ".join(f"<@&{role_id}>" for role_id in sorted(roles)) or "none",
            inline=False,
        )
        embed.add_field(
            name=f"Exempt channels ({len(channels)})",
            value=", ".join(f"<#{channel_id}>" for channel_id in sorted(channels))
            or "none",
            inline=False,
        )
        if users:
            embed.add_field(
                name=f"Exempt members ({len(users)})",
                value=", ".join(f"<@{user_id}>" for user_id in sorted(users)),
                inline=False,
            )

        embed.set_footer(
            text=(
                "Members with Manage Messages and the server owner are always "
                "exempt. Violations are logged to the moderation log channel."
            )
        )

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # Content filters
    # ------------------------------------------------------------------

    @app_commands.command(
        name="automod-antilink",
        description="Block links posted by members without Manage Messages.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Turn the link filter on or off",
        action="What to do when a link is posted",
        timeout_seconds="Timeout length when action is timeout",
    )
    async def antilink_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        action: Optional[ActionLiteral] = None,
        timeout_seconds: Optional[app_commands.Range[int, 60, MAX_TIMEOUT_SECONDS]] = None,
    ) -> None:
        await self._save(
            interaction,
            "link",
            self._base_values(enabled, action, timeout_seconds),
            headline=f"Link filter **{_state(enabled)}**.",
        )

    @app_commands.command(
        name="automod-antiinvite",
        description="Block Discord invite links from non-staff members.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Turn the invite filter on or off",
        action="What to do when an invite is posted",
        timeout_seconds="Timeout length when action is timeout",
    )
    async def antiinvite_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        action: Optional[ActionLiteral] = None,
        timeout_seconds: Optional[app_commands.Range[int, 60, MAX_TIMEOUT_SECONDS]] = None,
    ) -> None:
        await self._save(
            interaction,
            "invite",
            self._base_values(enabled, action, timeout_seconds),
            headline=f"Invite filter **{_state(enabled)}**.",
        )

    @app_commands.command(
        name="automod-antispam",
        description="Rate limit messages with a per-member token bucket.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Turn anti-spam on or off",
        messages="Messages allowed inside the interval",
        seconds="Length of the interval in seconds",
        strikes="Violations inside the window before escalating",
        action="What to do once the strike limit is reached",
        timeout_seconds="Timeout length when action is timeout",
    )
    async def antispam_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        messages: Optional[app_commands.Range[int, 2, 30]] = None,
        seconds: Optional[app_commands.Range[int, 1, 120]] = None,
        strikes: Optional[app_commands.Range[int, 1, 10]] = None,
        action: Optional[ActionLiteral] = None,
        timeout_seconds: Optional[app_commands.Range[int, 60, MAX_TIMEOUT_SECONDS]] = None,
    ) -> None:
        values = self._base_values(enabled, action, timeout_seconds)
        if messages is not None:
            values["threshold"] = messages
        if seconds is not None:
            values["interval_seconds"] = seconds
        if strikes is not None:
            values["strike_limit"] = strikes

        await self._save(
            interaction,
            "spam",
            values,
            headline=f"Anti-spam **{_state(enabled)}**.",
        )

    @app_commands.command(
        name="automod-anticaps",
        description="Remove messages that are mostly uppercase.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Turn the caps filter on or off",
        percent="Percentage of uppercase letters that trips the filter",
        min_length="Minimum number of letters before the filter applies",
        action="What to do when the filter trips",
        timeout_seconds="Timeout length when action is timeout",
    )
    async def anticaps_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        percent: Optional[app_commands.Range[int, 50, 100]] = None,
        min_length: Optional[app_commands.Range[int, 4, 500]] = None,
        action: Optional[ActionLiteral] = None,
        timeout_seconds: Optional[app_commands.Range[int, 60, MAX_TIMEOUT_SECONDS]] = None,
    ) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        values = self._base_values(enabled, action, timeout_seconds)
        if percent is not None:
            values["threshold"] = percent

        if min_length is not None:
            existing = await self.repo.get_rule(guild_id, "caps")
            options = load_options(existing.get("pattern")) if existing else {}
            options["min_length"] = int(min_length)
            values["pattern"] = dump_options(options)

        await self._save(
            interaction,
            "caps",
            values,
            headline=f"Caps filter **{_state(enabled)}**.",
        )

    @app_commands.command(
        name="automod-antimention",
        description="Limit how many users and roles one message may mention.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Turn the mention filter on or off",
        max_mentions="Mentions allowed in a single message",
        action="What to do when the limit is exceeded",
        timeout_seconds="Timeout length when action is timeout",
    )
    async def antimention_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        max_mentions: Optional[app_commands.Range[int, 1, 50]] = None,
        action: Optional[ActionLiteral] = None,
        timeout_seconds: Optional[app_commands.Range[int, 60, MAX_TIMEOUT_SECONDS]] = None,
    ) -> None:
        values = self._base_values(enabled, action, timeout_seconds)
        if max_mentions is not None:
            values["threshold"] = max_mentions

        await self._save(
            interaction,
            "mention",
            values,
            headline=f"Mention filter **{_state(enabled)}**.",
        )

    @app_commands.command(
        name="automod-antilines",
        description="Limit how many lines a single message may span.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Turn the line filter on or off",
        max_lines="Lines allowed in a single message",
        action="What to do when the limit is exceeded",
        timeout_seconds="Timeout length when action is timeout",
    )
    async def antilines_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        max_lines: Optional[app_commands.Range[int, 2, 100]] = None,
        action: Optional[ActionLiteral] = None,
        timeout_seconds: Optional[app_commands.Range[int, 60, MAX_TIMEOUT_SECONDS]] = None,
    ) -> None:
        values = self._base_values(enabled, action, timeout_seconds)
        if max_lines is not None:
            values["threshold"] = max_lines

        await self._save(
            interaction,
            "newline",
            values,
            headline=f"Line filter **{_state(enabled)}**.",
        )

    # ------------------------------------------------------------------
    # Word filter
    # ------------------------------------------------------------------

    @wordfilter.command(name="add", description="Add words to the AutoMod word filter.")
    @app_commands.describe(
        words="Comma-separated words or phrases to block",
        action="What to do when a blocked word is used",
        whole_word="Match whole words only (default) or any substring",
    )
    async def wordfilter_add(
        self,
        interaction: discord.Interaction,
        words: app_commands.Range[str, 1, 1000],
        action: Optional[ActionLiteral] = None,
        whole_word: Optional[bool] = None,
    ) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        candidates = [part.strip() for part in words.split(",")]
        cleaned = normalize_words(candidates)
        if not cleaned:
            await interaction.response.send_message(
                "\u274c No usable words were supplied. Separate entries with "
                f"commas; each must be 1-{MAX_WORD_LENGTH} characters.",
                ephemeral=True,
            )
            return

        added, current = await self.repo.add_words(guild_id, cleaned)

        follow_up: dict[str, Any] = {}
        if action is not None:
            follow_up["action"] = action
        if whole_word is not None:
            _, existing_whole = await self.repo.get_words(guild_id)
            if bool(whole_word) != existing_whole:
                follow_up["pattern"] = dump_options(
                    {"words": current, "whole_word": bool(whole_word)}
                )
        if follow_up:
            await self.repo.save_rule(guild_id, "word", follow_up)

        self._invalidate(guild_id)

        if not added:
            await interaction.response.send_message(
                "\u2139\ufe0f Every supplied word was already on the list "
                f"({len(current)} total).",
                ephemeral=True,
            )
            return

        limit_note = (
            f"\n\u26a0\ufe0f The list is capped at {MAX_WORDS} words."
            if len(current) >= MAX_WORDS
            else ""
        )
        await interaction.response.send_message(
            f"\u2705 Added {len(added)} word(s); the filter now blocks "
            f"{len(current)} word(s) and is **enabled**.{limit_note}",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @wordfilter.command(
        name="remove", description="Remove words from the AutoMod word filter."
    )
    @app_commands.describe(words="Comma-separated words or phrases to unblock")
    async def wordfilter_remove(
        self,
        interaction: discord.Interaction,
        words: app_commands.Range[str, 1, 1000],
    ) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        cleaned = normalize_words(part.strip() for part in words.split(","))
        if not cleaned:
            await interaction.response.send_message(
                "\u274c No usable words were supplied.", ephemeral=True
            )
            return

        removed, remaining = await self.repo.remove_words(guild_id, cleaned)
        self._invalidate(guild_id)

        if not removed:
            await interaction.response.send_message(
                "\u2139\ufe0f None of those words were on the list.", ephemeral=True
            )
            return

        note = (
            "\nThe list is now empty, so the filter no longer scans messages."
            if not remaining
            else ""
        )
        await interaction.response.send_message(
            f"\u2705 Removed {len(removed)} word(s); {len(remaining)} remain.{note}",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @wordfilter.command(name="list", description="List the blocked words.")
    async def wordfilter_list(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        words, whole_word = await self.repo.get_words(guild_id)
        rule = await self.repo.get_rule(guild_id, "word")

        if not words:
            await interaction.response.send_message\
                (
                    "\u2139\ufe0f No words are blocked in this server. Add some "
                    "with `/automod-wordfilter add`.",
                    ephemeral=True,
                )
            return

        embed = discord.Embed(
            title="AutoMod Word Filter",
            description=(
                f"{len(words)} word(s) \u2022 "
                f"{'whole word' if whole_word else 'substring'} matching \u2022 "
                f"filter **{_state(rule.get('enabled') if rule else 0)}**"
            ),
            color=discord.Color.blurple(),
        )

        # Rendered inside a code fence so the blocked terms cannot themselves
        # mention anyone or break the embed formatting.
        chunk: list[str] = []
        page = 1
        length = 0
        for word in words:
            entry = word.replace("`", "\u02cb")
            if length + len(entry) + 2 > 1000:
                embed.add_field(
                    name=f"Words ({page})",
                    value="```\n" + ", ".join(chunk) + "\n```",
                    inline=False,
                )
                chunk, length, page = [], 0, page + 1
                if page > 5:  # embeds are finite; five pages is plenty
                    break
            chunk.append(entry)
            length += len(entry) + 2

        if chunk and page <= 5:
            embed.add_field(
                name=f"Words ({page})",
                value="```\n" + ", ".join(chunk) + "\n```",
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    # ------------------------------------------------------------------
    # Whitelist
    # ------------------------------------------------------------------

    @app_commands.command(
        name="automod-whitelist-role",
        description="Exempt a role from every AutoMod filter.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        role="The role to exempt",
        remove="Set to True to revoke the exemption instead",
    )
    async def whitelist_role_cmd(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        remove: bool = False,
    ) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        if remove:
            changed = await self.repo.remove_whitelist(guild_id, role.id, "role")
            message = (
                f"\u2705 {role.mention} is no longer exempt from AutoMod."
                if changed
                else f"\u2139\ufe0f {role.mention} was not exempt."
            )
        else:
            changed = await self.repo.add_whitelist(guild_id, role.id, "role")
            message = (
                f"\u2705 {role.mention} is now exempt from every AutoMod filter."
                if changed
                else f"\u2139\ufe0f {role.mention} was already exempt."
            )

        self._invalidate(guild_id)
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(
        name="automod-whitelist-channel",
        description="Exempt a channel from every AutoMod filter.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="The channel to exempt (threads inherit their parent)",
        remove="Set to True to revoke the exemption instead",
    )
    async def whitelist_channel_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.Thread | discord.VoiceChannel,
        remove: bool = False,
    ) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        if remove:
            changed = await self.repo.remove_whitelist(guild_id, channel.id, "channel")
            message = (
                f"\u2705 {channel.mention} is no longer exempt from AutoMod."
                if changed
                else f"\u2139\ufe0f {channel.mention} was not exempt."
            )
        else:
            changed = await self.repo.add_whitelist(guild_id, channel.id, "channel")
            message = (
                f"\u2705 {channel.mention} is now exempt from every AutoMod filter."
                if changed
                else f"\u2139\ufe0f {channel.mention} was already exempt."
            )

        self._invalidate(guild_id)
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
    await bot.add_cog(AutoModCommands(bot))


__all__ = [
    "AutoMod",
    "AutoModCommands",
    "GuildPolicy",
    "ACTIONS",
    "EVALUATION_ORDER",
    "compile_words",
    "setup",
]
