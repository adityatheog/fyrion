"""
AutoMod rule repository.

Storage layout
--------------
Rules live in the ``automod_rules`` table declared by
:mod:`fyrion.database.schema`. Fyrion exposes exactly one rule per rule type per
guild, so ``automod_rules.name`` is set to the rule type and the table's
``UNIQUE (guild_id, name)`` constraint is what guarantees that invariant --
two concurrent ``/automod-antilink`` invocations cannot create two link rules.

The master switch is ``guild_settings.automod_enabled``, and the guild-wide
exemption list is the legacy ``whitelists`` table (``entity_type`` is one of
``user``, ``role`` or ``channel``), which the security commands already used.

Rule columns are generic, so each type interprets them:

============ ============================================================
rule type    meaning of the numeric columns
============ ============================================================
``spam``     ``threshold`` messages per ``interval_seconds``, escalating
             after ``strike_limit`` strikes for ``duration_seconds``
``caps``     ``threshold`` = uppercase percent; ``pattern`` carries
             ``{"min_length": N}``
``mention``  ``threshold`` = maximum mentions per message
``newline``  ``threshold`` = maximum lines per message
``word``     ``pattern`` carries ``{"words": [...], "whole_word": bool}``
``link``     no numeric configuration
``invite``   no numeric configuration
============ ============================================================

``pattern`` is therefore always a JSON object of rule-specific options rather
than a raw regex, so no operator-supplied expression is ever compiled.

Caching
-------
The message interceptor cannot afford a database round trip per message, so it
caches a compiled policy. Every write here bumps a per-guild generation counter;
the interceptor compares generations and rebuilds when they differ. The counter
is class level because each cog constructs its own repository instance while all
of them talk to the same database.

Security
--------
Every value is bound as a SQL parameter and every column name is validated
against the schema allow-list by the pool before any statement is built.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar, Final, Iterable, Mapping, Sequence

log = logging.getLogger("fyrion.database.repositories.automod_rules")

# The subset of schema rule types Fyrion's slash commands manage.
RULE_TYPES: Final[tuple[str, ...]] = (
    "spam",
    "invite",
    "link",
    "word",
    "caps",
    "mention",
    "newline",
)

# Human labels, used in replies and log embeds.
RULE_LABELS: Final[dict[str, str]] = {
    "spam": "Anti-spam",
    "invite": "Anti-invite",
    "link": "Anti-link",
    "word": "Word filter",
    "caps": "Anti-caps",
    "mention": "Anti-mention",
    "newline": "Anti-lines",
}

# Mirrors the CHECK constraint on automod_rules.action.
ACTIONS: Final[tuple[str, ...]] = ("log", "delete", "warn", "timeout", "kick", "ban")

WHITELIST_TYPES: Final[tuple[str, ...]] = ("user", "role", "channel")

# Columns a command is allowed to write. Anything else is a programming error.
WRITABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "enabled",
        "action",
        "threshold",
        "interval_seconds",
        "duration_seconds",
        "strike_limit",
        "pattern",
        "exempt_roles",
        "exempt_channels",
        "exempt_users",
        "created_by",
    }
)

# Seeded on first creation so a freshly enabled rule is immediately sensible;
# the table defaults alone would give a caps rule a 5% threshold.
RULE_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "spam": {
        "action": "delete",
        "threshold": 5,
        "interval_seconds": 5,
        "strike_limit": 3,
        "duration_seconds": 300,
    },
    "invite": {
        "action": "delete",
        "threshold": 1,
        "interval_seconds": 1,
        "duration_seconds": 300,
    },
    "link": {
        "action": "delete",
        "threshold": 1,
        "interval_seconds": 1,
        "duration_seconds": 300,
    },
    "word": {
        "action": "delete",
        "threshold": 1,
        "interval_seconds": 1,
        "duration_seconds": 300,
        "pattern": '{"words": [], "whole_word": true}',
    },
    "caps": {
        "action": "delete",
        "threshold": 70,
        "interval_seconds": 1,
        "duration_seconds": 300,
        "pattern": '{"min_length": 10}',
    },
    "mention": {
        "action": "delete",
        "threshold": 5,
        "interval_seconds": 1,
        "duration_seconds": 300,
    },
    "newline": {
        "action": "delete",
        "threshold": 10,
        "interval_seconds": 1,
        "duration_seconds": 300,
    },
}

# Bounds enforced here as well as by the slash-command Range annotations, so a
# dashboard or a future caller cannot store an unusable value.
MAX_WORDS: Final[int] = 200
MAX_WORD_LENGTH: Final[int] = 64


def load_options(raw: Any) -> dict[str, Any]:
    """Parses a rule's ``pattern`` column into an options mapping.

    Anything unparseable degrades to an empty mapping: a corrupt options blob
    must weaken a filter, never crash the message handler.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}

    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        # Tolerate a bare word list written by an older build.
        return {"words": parsed}
    return {}


def dump_options(options: Mapping[str, Any]) -> str:
    """Serializes a rule options mapping for the ``pattern`` column."""
    return json.dumps(options, separators=(",", ":"), ensure_ascii=False)


def normalize_words(words: Iterable[Any]) -> list[str]:
    """Lower-cases, trims, de-duplicates and bounds a word list."""
    seen: dict[str, None] = {}
    for candidate in words:
        text = str(candidate).strip().lower()
        if not text or len(text) > MAX_WORD_LENGTH:
            continue
        seen.setdefault(text, None)
        if len(seen) >= MAX_WORDS:
            break
    return sorted(seen)


def id_set(raw: Any) -> frozenset[int]:
    """Parses a JSON array of snowflakes into a set of ints."""
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        values: Iterable[Any] = raw
    elif isinstance(raw, str):
        if not raw.strip():
            return frozenset()
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return frozenset()
        if not isinstance(parsed, (list, tuple)):
            return frozenset()
        values = parsed
    else:
        return frozenset()

    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return frozenset(result)


class AutoModRuleRepository:
    """Reads and writes AutoMod rules, the master switch and the whitelist."""

    # guild_id -> monotonically increasing revision of that guild's policy.
    _generation: ClassVar[dict[int, int]] = {}

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Cache coordination
    # ------------------------------------------------------------------

    @classmethod
    def generation(cls, guild_id: int) -> int:
        """Returns the current policy revision for a guild."""
        return cls._generation.get(int(guild_id), 0)

    @classmethod
    def bump(cls, guild_id: int) -> int:
        """Marks a guild's cached policy as stale."""
        key = int(guild_id)
        revision = cls._generation.get(key, 0) + 1
        cls._generation[key] = revision
        return revision

    @classmethod
    def reset_generations(cls) -> None:
        """Clears every revision counter. Used by the test suite."""
        cls._generation.clear()

    # ------------------------------------------------------------------
    # Master switch
    # ------------------------------------------------------------------

    async def get_master(self, guild_id: int) -> bool:
        settings = await self.db.get_guild_settings(int(guild_id))
        return bool(settings.get("automod_enabled"))

    async def set_master(self, guild_id: int, enabled: bool) -> None:
        await self.db.update_guild_settings(
            int(guild_id), automod_enabled=int(bool(enabled))
        )
        self.bump(guild_id)

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    async def get_rules(
        self, guild_id: int, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"guild_id": int(guild_id)}
        if enabled_only:
            where["enabled"] = 1
        return await self.db.fetch_many("automod_rules", where, order_by="rule_id ASC")

    async def get_rule(self, guild_id: int, rule_type: str) -> dict[str, Any] | None:
        self._validate_type(rule_type)
        return await self.db.fetch_one(
            "automod_rules", {"guild_id": int(guild_id), "name": rule_type}
        )

    async def save_rule(
        self, guild_id: int, rule_type: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Creates or updates the guild's rule of ``rule_type``.

        ``None`` values are ignored so a command can pass every optional
        parameter through without clobbering settings the operator did not
        mention. Missing fields are seeded from :data:`RULE_DEFAULTS` the first
        time a rule is created.
        """
        self._validate_type(rule_type)

        unknown = set(values) - WRITABLE_FIELDS
        if unknown:
            raise ValueError(f"Unwritable AutoMod field(s): {sorted(unknown)}")

        existing = await self.get_rule(guild_id, rule_type)

        payload: dict[str, Any] = {
            "guild_id": int(guild_id),
            "name": rule_type,
            "rule_type": rule_type,
        }
        if existing is None:
            payload.update(RULE_DEFAULTS.get(rule_type, {}))

        for key, value in values.items():
            if value is None:
                continue
            if key == "action":
                if value not in ACTIONS:
                    raise ValueError(f"Unsupported AutoMod action: {value!r}")
                payload[key] = value
            elif key == "enabled":
                payload[key] = int(bool(value))
            elif key in {
                "threshold",
                "interval_seconds",
                "duration_seconds",
                "strike_limit",
                "created_by",
            }:
                payload[key] = int(value)
            else:
                payload[key] = value

        await self.db.upsert(
            "automod_rules", payload, conflict_columns=("guild_id", "name")
        )
        self.bump(guild_id)

        saved = await self.get_rule(guild_id, rule_type)
        if saved is None:  # pragma: no cover - the upsert just succeeded
            raise RuntimeError(
                f"AutoMod rule {rule_type!r} disappeared after being saved."
            )
        return saved

    async def set_enabled(
        self, guild_id: int, rule_type: str, enabled: bool
    ) -> dict[str, Any]:
        return await self.save_rule(guild_id, rule_type, {"enabled": enabled})

    async def delete_rule(self, guild_id: int, rule_type: str) -> bool:
        self._validate_type(rule_type)
        removed = await self.db.delete(
            "automod_rules", {"guild_id": int(guild_id), "name": rule_type}
        )
        if removed:
            self.bump(guild_id)
        return bool(removed)

    # ------------------------------------------------------------------
    # Word filter
    # ------------------------------------------------------------------

    async def get_words(self, guild_id: int) -> tuple[list[str], bool]:
        """Returns ``(words, whole_word)`` for the guild's word filter."""
        rule = await self.get_rule(guild_id, "word")
        options = load_options(rule.get("pattern")) if rule else {}
        words = normalize_words(options.get("words") or [])
        whole_word = bool(options.get("whole_word", True))
        return words, whole_word

    async def add_words(
        self, guild_id: int, words: Sequence[str], *, enable: bool = True
    ) -> tuple[list[str], list[str]]:
        """Adds words to the filter. Returns ``(added, current_words)``."""
        current, whole_word = await self.get_words(guild_id)
        incoming = normalize_words(words)

        added = [word for word in incoming if word not in current]
        if not added:
            return [], current

        merged = normalize_words([*current, *added])
        changes: dict[str, Any] = {
            "pattern": dump_options({"words": merged, "whole_word": whole_word})
        }
        if enable:
            # Adding a word without arming the filter is never the intent.
            changes["enabled"] = True

        await self.save_rule(guild_id, "word", changes)
        return added, merged

    async def remove_words(
        self, guild_id: int, words: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        """Removes words from the filter. Returns ``(removed, current_words)``."""
        current, whole_word = await self.get_words(guild_id)
        targets = set(normalize_words(words))

        removed = [word for word in current if word in targets]
        if not removed:
            return [], current

        remaining = [word for word in current if word not in targets]
        await self.save_rule(
            guild_id,
            "word",
            {"pattern": dump_options({"words": remaining, "whole_word": whole_word})},
        )
        return removed, remaining

    async def set_whole_word(self, guild_id: int, whole_word: bool) -> None:
        words, _ = await self.get_words(guild_id)
        await self.save_rule(
            guild_id,
            "word",
            {"pattern": dump_options({"words": words, "whole_word": bool(whole_word)})},
        )

    # ------------------------------------------------------------------
    # Whitelist (guild-wide exemptions)
    # ------------------------------------------------------------------

    async def add_whitelist(
        self, guild_id: int, entity_id: int, entity_type: str
    ) -> bool:
        """Exempts an entity. Returns False when it was already exempt."""
        self._validate_entity_type(entity_type)

        # The whitelists table has a foreign key onto guild_configs, which
        # ensure_guild() creates alongside guild_settings.
        await self.db.ensure_guild(int(guild_id))
        changed = await self.db.execute(
            "INSERT OR IGNORE INTO whitelists (guild_id, entity_id, entity_type) "
            "VALUES (?, ?, ?)",
            (int(guild_id), int(entity_id), entity_type),
        )
        self.bump(guild_id)
        return bool(changed)

    async def remove_whitelist(
        self, guild_id: int, entity_id: int, entity_type: str
    ) -> bool:
        """Removes an exemption. Returns False when there was none."""
        self._validate_entity_type(entity_type)
        changed = await self.db.execute(
            "DELETE FROM whitelists "
            "WHERE guild_id = ? AND entity_id = ? AND entity_type = ?",
            (int(guild_id), int(entity_id), entity_type),
        )
        if changed:
            self.bump(guild_id)
        return bool(changed)

    async def get_whitelist(self, guild_id: int) -> dict[str, frozenset[int]]:
        """Returns the guild's exemptions grouped by entity type."""
        rows = await self.db.fetchall(
            "SELECT entity_id, entity_type FROM whitelists WHERE guild_id = ?",
            (int(guild_id),),
        )

        grouped: dict[str, set[int]] = {kind: set() for kind in WHITELIST_TYPES}
        for row in rows:
            entity_type = str(row["entity_type"])
            if entity_type not in grouped:
                continue
            try:
                grouped[entity_type].add(int(row["entity_id"]))
            except (TypeError, ValueError):
                continue
        return {kind: frozenset(values) for kind, values in grouped.items()}

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_type(rule_type: str) -> None:
        if rule_type not in RULE_TYPES:
            raise ValueError(f"Unsupported AutoMod rule type: {rule_type!r}")

    @staticmethod
    def _validate_entity_type(entity_type: str) -> None:
        if entity_type not in WHITELIST_TYPES:
            raise ValueError(f"Unsupported whitelist entity type: {entity_type!r}")


__all__ = [
    "AutoModRuleRepository",
    "ACTIONS",
    "MAX_WORDS",
    "MAX_WORD_LENGTH",
    "RULE_DEFAULTS",
    "RULE_LABELS",
    "RULE_TYPES",
    "WHITELIST_TYPES",
    "WRITABLE_FIELDS",
    "dump_options",
    "id_set",
    "load_options",
    "normalize_words",
]
