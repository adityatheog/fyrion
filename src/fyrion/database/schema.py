"""
Fyrion database schema.

Every statement is idempotent (``IF NOT EXISTS``) because the whole script is
replayed on every boot. Adding a table, index or trigger here is safe; changing
the type or the constraints of an existing column is not, and needs an explicit
migration instead.

Two groups of statements live here:

* ``LEGACY_SCHEMA`` keeps the original tables (``guild_configs``, ``warnings``,
  ``whitelists``, ``automod_configs``, ``ticket_configs``, ``invite_stats``,
  ``member_inviters``) so the repositories written against them keep working.
  The original ``tickets`` table moved into ``CORE_SCHEMA``, which is a strict
  superset of it: the columns the old repository reads and writes
  (``guild_id``, ``channel_id``, ``user_id``, ``status``, ``created_at``) are
  still present and every added column is nullable or defaulted.
* ``CORE_SCHEMA`` is the production data model used from this phase onwards.

``TABLE_COLUMNS`` and ``PRIMARY_KEYS`` mirror ``CORE_SCHEMA`` and act as the
allow-list for the generic CRUD helpers in :mod:`fyrion.database.manager`.
Identifiers can never be parameterized in SQL, so every table and column name
that reaches a statement is validated against these sets first, while values are
always bound as parameters.

Conventions:

* Snowflakes are stored as ``INTEGER`` (SQLite integers are 64-bit).
* Timestamps are ISO-8601 UTC strings (``YYYY-MM-DDTHH:MM:SSZ``) so they sort
  lexicographically and survive a dump/restore unambiguously.
* Booleans are ``INTEGER`` 0/1, which is what SQLite actually stores.
* Collections that are only ever read as a whole (exempt role lists, giveaway
  winners, inventories) are JSON text; anything that needs to be queried or
  counted gets its own table.
* Guild-scoped tables cascade from ``guild_settings`` so removing a guild leaves
  no orphaned rows behind.
"""
from __future__ import annotations

from typing import Final

# Bumped whenever CORE_SCHEMA changes in a way operators should notice. Stored
# in ``PRAGMA user_version`` and in ``schema_meta`` by the connection pool.
SCHEMA_VERSION: Final[int] = 2

# Portable "now" expression. Kept in one place so every default matches.
_NOW: Final[str] = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


LEGACY_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS guild_configs (
    guild_id INTEGER PRIMARY KEY,
    welcome_channel_id INTEGER,
    log_channel_id INTEGER,
    autorole_id INTEGER,
    anti_link_enabled BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_warnings_guild_user
    ON warnings (guild_id, user_id);

CREATE TABLE IF NOT EXISTS whitelists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE,
    UNIQUE (guild_id, entity_id, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_whitelists_guild
    ON whitelists (guild_id);

CREATE TABLE IF NOT EXISTS automod_configs (
    guild_id INTEGER PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    anti_spam_enabled BOOLEAN NOT NULL DEFAULT 0,
    spam_message_limit INTEGER NOT NULL DEFAULT 5,
    spam_interval_seconds INTEGER NOT NULL DEFAULT 5,
    spam_strike_limit INTEGER NOT NULL DEFAULT 3,
    spam_timeout_seconds INTEGER NOT NULL DEFAULT 300,
    anti_invite_enabled BOOLEAN NOT NULL DEFAULT 0,
    link_filter_enabled BOOLEAN NOT NULL DEFAULT 0,
    caps_filter_enabled BOOLEAN NOT NULL DEFAULT 0,
    caps_threshold_percent INTEGER NOT NULL DEFAULT 70,
    caps_min_length INTEGER NOT NULL DEFAULT 10,
    log_channel_id INTEGER,
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ticket_configs (
    guild_id INTEGER PRIMARY KEY,
    category_id INTEGER,
    log_channel_id INTEGER,
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS member_inviters (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    inviter_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invite_stats (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joins INTEGER DEFAULT 0,
    leaves INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE
);
"""


CORE_SCHEMA: Final[str] = f"""
-- --------------------------------------------------------------------------
-- Bookkeeping
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- --------------------------------------------------------------------------
-- guild_settings: one row per guild, parent of every guild-scoped table.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id                     INTEGER PRIMARY KEY,
    prefix                       TEXT    NOT NULL DEFAULT '!',
    locale                       TEXT    NOT NULL DEFAULT 'en-US',
    timezone                     TEXT    NOT NULL DEFAULT 'UTC',

    mod_log_channel_id           INTEGER,
    audit_log_channel_id         INTEGER,
    message_log_channel_id       INTEGER,

    welcome_channel_id           INTEGER,
    welcome_message              TEXT,
    goodbye_channel_id           INTEGER,
    goodbye_message              TEXT,
    autorole_id                  INTEGER,
    mute_role_id                 INTEGER,

    ticket_category_id           INTEGER,
    ticket_log_channel_id        INTEGER,
    ticket_support_role_id       INTEGER,

    economy_enabled              INTEGER NOT NULL DEFAULT 0 CHECK (economy_enabled IN (0, 1)),
    economy_currency_symbol      TEXT    NOT NULL DEFAULT '$',
    economy_daily_amount         INTEGER NOT NULL DEFAULT 250 CHECK (economy_daily_amount >= 0),
    economy_work_amount          INTEGER NOT NULL DEFAULT 100 CHECK (economy_work_amount >= 0),

    leveling_enabled             INTEGER NOT NULL DEFAULT 0 CHECK (leveling_enabled IN (0, 1)),
    leveling_announce_channel_id INTEGER,
    leveling_xp_per_message      INTEGER NOT NULL DEFAULT 15 CHECK (leveling_xp_per_message >= 0),
    leveling_cooldown_seconds    INTEGER NOT NULL DEFAULT 60 CHECK (leveling_cooldown_seconds >= 0),
    leveling_stack_rewards       INTEGER NOT NULL DEFAULT 1 CHECK (leveling_stack_rewards IN (0, 1)),

    automod_enabled              INTEGER NOT NULL DEFAULT 0 CHECK (automod_enabled IN (0, 1)),
    dashboard_enabled            INTEGER NOT NULL DEFAULT 0 CHECK (dashboard_enabled IN (0, 1)),

    created_at                   TEXT    NOT NULL DEFAULT ({_NOW}),
    updated_at                   TEXT    NOT NULL DEFAULT ({_NOW})
);

CREATE TRIGGER IF NOT EXISTS trg_guild_settings_touch
AFTER UPDATE ON guild_settings
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE guild_settings SET updated_at = {_NOW} WHERE guild_id = NEW.guild_id;
END;

-- --------------------------------------------------------------------------
-- moderation_cases: append-only moderation history with per-guild case numbers.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS moderation_cases (
    case_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    case_number      INTEGER NOT NULL,
    action           TEXT    NOT NULL CHECK (action IN (
                         'note', 'warn', 'mute', 'unmute', 'timeout', 'untimeout',
                         'kick', 'ban', 'softban', 'unban', 'purge', 'lock', 'unlock'
                     )),
    target_id        INTEGER NOT NULL,
    target_tag       TEXT,
    moderator_id     INTEGER NOT NULL,
    reason           TEXT,
    evidence         TEXT,
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds > 0),
    expires_at       TEXT,
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    resolved_at      TEXT,
    resolved_by      INTEGER,
    created_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    UNIQUE (guild_id, case_number),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_moderation_cases_target
    ON moderation_cases (guild_id, target_id, created_at DESC);

-- Drives the expiry sweeper: find still-active, time-limited punishments.
CREATE INDEX IF NOT EXISTS idx_moderation_cases_expiry
    ON moderation_cases (active, expires_at)
    WHERE active = 1 AND expires_at IS NOT NULL;

-- --------------------------------------------------------------------------
-- automod_rules: several independent rules per guild, evaluated per message.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automod_rules (
    rule_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    rule_type        TEXT    NOT NULL CHECK (rule_type IN (
                         'spam', 'invite', 'link', 'caps', 'mention', 'word',
                         'attachment', 'emoji', 'newline', 'zalgo', 'raid'
                     )),
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    action           TEXT    NOT NULL DEFAULT 'delete' CHECK (action IN (
                         'log', 'delete', 'warn', 'timeout', 'kick', 'ban'
                     )),
    threshold        INTEGER NOT NULL DEFAULT 5 CHECK (threshold > 0),
    interval_seconds INTEGER NOT NULL DEFAULT 5 CHECK (interval_seconds > 0),
    duration_seconds INTEGER NOT NULL DEFAULT 300 CHECK (duration_seconds >= 0),
    strike_limit     INTEGER NOT NULL DEFAULT 3 CHECK (strike_limit > 0),
    -- Operator-supplied regex or word list. Always compiled with a size limit
    -- and a timeout guard by the AutoMod cog, never trusted blindly.
    pattern          TEXT,
    exempt_roles     TEXT    NOT NULL DEFAULT '[]',
    exempt_channels  TEXT    NOT NULL DEFAULT '[]',
    exempt_users     TEXT    NOT NULL DEFAULT '[]',
    created_by       INTEGER,
    created_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    updated_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    UNIQUE (guild_id, name),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_automod_rules_guild
    ON automod_rules (guild_id, enabled);

CREATE TRIGGER IF NOT EXISTS trg_automod_rules_touch
AFTER UPDATE ON automod_rules
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE automod_rules SET updated_at = {_NOW} WHERE rule_id = NEW.rule_id;
END;

-- --------------------------------------------------------------------------
-- economy_accounts: per-guild wallets. Balances are guarded by CHECK
-- constraints so no code path can drive an account negative.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS economy_accounts (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    balance      INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    bank         INTEGER NOT NULL DEFAULT 0 CHECK (bank >= 0),
    bank_limit   INTEGER NOT NULL DEFAULT 10000 CHECK (bank_limit >= 0),
    total_earned INTEGER NOT NULL DEFAULT 0 CHECK (total_earned >= 0),
    total_spent  INTEGER NOT NULL DEFAULT 0 CHECK (total_spent >= 0),
    daily_streak INTEGER NOT NULL DEFAULT 0 CHECK (daily_streak >= 0),
    last_daily_at TEXT,
    last_work_at  TEXT,
    inventory    TEXT    NOT NULL DEFAULT '[]',
    created_at   TEXT    NOT NULL DEFAULT ({_NOW}),
    updated_at   TEXT    NOT NULL DEFAULT ({_NOW}),
    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_economy_accounts_rich
    ON economy_accounts (guild_id, balance DESC);

CREATE TRIGGER IF NOT EXISTS trg_economy_accounts_touch
AFTER UPDATE ON economy_accounts
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE economy_accounts SET updated_at = {_NOW}
     WHERE guild_id = NEW.guild_id AND user_id = NEW.user_id;
END;

-- --------------------------------------------------------------------------
-- leveling_profiles / level_rewards
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leveling_profiles (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    xp              INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
    level           INTEGER NOT NULL DEFAULT 0 CHECK (level >= 0),
    total_messages  INTEGER NOT NULL DEFAULT 0 CHECK (total_messages >= 0),
    voice_minutes   INTEGER NOT NULL DEFAULT 0 CHECK (voice_minutes >= 0),
    last_message_at TEXT,
    last_xp_at      TEXT,
    created_at      TEXT    NOT NULL DEFAULT ({_NOW}),
    updated_at      TEXT    NOT NULL DEFAULT ({_NOW}),
    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_leveling_profiles_rank
    ON leveling_profiles (guild_id, xp DESC);

CREATE TRIGGER IF NOT EXISTS trg_leveling_profiles_touch
AFTER UPDATE ON leveling_profiles
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE leveling_profiles SET updated_at = {_NOW}
     WHERE guild_id = NEW.guild_id AND user_id = NEW.user_id;
END;

CREATE TABLE IF NOT EXISTS level_rewards (
    reward_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    level           INTEGER NOT NULL CHECK (level > 0),
    role_id         INTEGER NOT NULL,
    remove_previous INTEGER NOT NULL DEFAULT 0 CHECK (remove_previous IN (0, 1)),
    created_at      TEXT    NOT NULL DEFAULT ({_NOW}),
    UNIQUE (guild_id, level, role_id),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_level_rewards_guild
    ON level_rewards (guild_id, level);

-- --------------------------------------------------------------------------
-- giveaways: the giveaway itself plus one row per entrant, so entries can be
-- counted and de-duplicated in SQL instead of in memory.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS giveaways (
    giveaway_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    message_id       INTEGER NOT NULL UNIQUE,
    host_id          INTEGER NOT NULL,
    prize            TEXT    NOT NULL,
    description      TEXT,
    winner_count     INTEGER NOT NULL DEFAULT 1 CHECK (winner_count > 0),
    required_role_id INTEGER,
    required_level   INTEGER CHECK (required_level IS NULL OR required_level >= 0),
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'ended', 'cancelled')),
    ends_at          TEXT    NOT NULL,
    ended_at         TEXT,
    winners          TEXT    NOT NULL DEFAULT '[]',
    entry_count      INTEGER NOT NULL DEFAULT 0 CHECK (entry_count >= 0),
    created_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

-- Drives the scheduler: the next giveaway to end.
CREATE INDEX IF NOT EXISTS idx_giveaways_due
    ON giveaways (status, ends_at);

CREATE INDEX IF NOT EXISTS idx_giveaways_guild
    ON giveaways (guild_id, status);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    entries     INTEGER NOT NULL DEFAULT 1 CHECK (entries > 0),
    created_at  TEXT    NOT NULL DEFAULT ({_NOW}),
    UNIQUE (giveaway_id, user_id),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways (giveaway_id) ON DELETE CASCADE
);

-- --------------------------------------------------------------------------
-- tickets: superset of the original table, so the existing repository keeps
-- working while the new columns carry claim/close metadata.
-- No foreign key on guild_id: the legacy repository inserts tickets without
-- creating a parent row first, and losing tickets to a constraint error would
-- be worse than keeping a row whose guild has been deleted.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    ticket_number    INTEGER,
    channel_id       INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    subject          TEXT,
    status           TEXT    NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'claimed', 'closed')),
    claimed_by       INTEGER,
    panel_message_id INTEGER,
    transcript       TEXT,
    closed_by        INTEGER,
    close_reason     TEXT,
    created_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    closed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_guild_status
    ON tickets (guild_id, status);

CREATE INDEX IF NOT EXISTS idx_tickets_owner
    ON tickets (guild_id, user_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_channel
    ON tickets (channel_id);

-- --------------------------------------------------------------------------
-- custom_commands
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS custom_commands (
    command_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    name               TEXT    NOT NULL,
    content            TEXT    NOT NULL,
    description        TEXT,
    is_embed           INTEGER NOT NULL DEFAULT 0 CHECK (is_embed IN (0, 1)),
    embed_color        INTEGER,
    required_role_id   INTEGER,
    delete_invocation  INTEGER NOT NULL DEFAULT 0 CHECK (delete_invocation IN (0, 1)),
    uses               INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
    created_by         INTEGER,
    created_at         TEXT    NOT NULL DEFAULT ({_NOW}),
    updated_at         TEXT    NOT NULL DEFAULT ({_NOW}),
    UNIQUE (guild_id, name),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_custom_commands_guild
    ON custom_commands (guild_id);

CREATE TRIGGER IF NOT EXISTS trg_custom_commands_touch
AFTER UPDATE ON custom_commands
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE custom_commands SET updated_at = {_NOW} WHERE command_id = NEW.command_id;
END;

-- --------------------------------------------------------------------------
-- reaction_roles
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reaction_roles (
    entry_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    -- Unicode emoji, or the '<name:id>' form for custom emoji.
    emoji            TEXT    NOT NULL,
    role_id          INTEGER NOT NULL,
    mode             TEXT    NOT NULL DEFAULT 'toggle'
                     CHECK (mode IN ('toggle', 'add_only', 'remove_only', 'unique')),
    -- Rows sharing a group_key with mode='unique' are mutually exclusive.
    group_key        TEXT,
    required_role_id INTEGER,
    created_by       INTEGER,
    created_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    UNIQUE (message_id, emoji),
    FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reaction_roles_message
    ON reaction_roles (message_id);

CREATE INDEX IF NOT EXISTS idx_reaction_roles_guild
    ON reaction_roles (guild_id);

-- --------------------------------------------------------------------------
-- dashboard_sessions
--
-- Only *hashes* of session and refresh tokens are stored, so a database leak
-- cannot be replayed against the dashboard. The same applies to client IPs,
-- which are kept as a keyed hash for abuse detection rather than in clear.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    session_id         TEXT    PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    token_hash         TEXT    NOT NULL UNIQUE,
    refresh_token_hash TEXT    UNIQUE,
    scopes             TEXT    NOT NULL DEFAULT '',
    ip_hash            TEXT,
    user_agent         TEXT,
    created_at         TEXT    NOT NULL DEFAULT ({_NOW}),
    last_seen_at       TEXT    NOT NULL DEFAULT ({_NOW}),
    expires_at         TEXT    NOT NULL,
    revoked            INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    revoked_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_user
    ON dashboard_sessions (user_id, revoked);

CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expiry
    ON dashboard_sessions (expires_at);
"""


# The pool replays this on every boot. Legacy first so the old foreign keys
# always have their parent table available.
INITIAL_SCHEMA: Final[str] = LEGACY_SCHEMA + CORE_SCHEMA


# --------------------------------------------------------------------------
# Identifier allow-lists for the generic CRUD helpers.
# --------------------------------------------------------------------------
TABLE_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "schema_meta": frozenset({"key", "value"}),
    "guild_settings": frozenset(
        {
            "guild_id",
            "prefix",
            "locale",
            "timezone",
            "mod_log_channel_id",
            "audit_log_channel_id",
            "message_log_channel_id",
            "welcome_channel_id",
            "welcome_message",
            "goodbye_channel_id",
            "goodbye_message",
            "autorole_id",
            "mute_role_id",
            "ticket_category_id",
            "ticket_log_channel_id",
            "ticket_support_role_id",
            "economy_enabled",
            "economy_currency_symbol",
            "economy_daily_amount",
            "economy_work_amount",
            "leveling_enabled",
            "leveling_announce_channel_id",
            "leveling_xp_per_message",
            "leveling_cooldown_seconds",
            "leveling_stack_rewards",
            "automod_enabled",
            "dashboard_enabled",
            "created_at",
            "updated_at",
        }
    ),
    "moderation_cases": frozenset(
        {
            "case_id",
            "guild_id",
            "case_number",
            "action",
            "target_id",
            "target_tag",
            "moderator_id",
            "reason",
            "evidence",
            "duration_seconds",
            "expires_at",
            "active",
            "resolved_at",
            "resolved_by",
            "created_at",
        }
    ),
    "automod_rules": frozenset(
        {
            "rule_id",
            "guild_id",
            "name",
            "rule_type",
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
            "created_at",
            "updated_at",
        }
    ),
    "economy_accounts": frozenset(
        {
            "guild_id",
            "user_id",
            "balance",
            "bank",
            "bank_limit",
            "total_earned",
            "total_spent",
            "daily_streak",
            "last_daily_at",
            "last_work_at",
            "inventory",
            "created_at",
            "updated_at",
        }
    ),
    "leveling_profiles": frozenset(
        {
            "guild_id",
            "user_id",
            "xp",
            "level",
            "total_messages",
            "voice_minutes",
            "last_message_at",
            "last_xp_at",
            "created_at",
            "updated_at",
        }
    ),
    "level_rewards": frozenset(
        {"reward_id", "guild_id", "level", "role_id", "remove_previous", "created_at"}
    ),
    "giveaways": frozenset(
        {
            "giveaway_id",
            "guild_id",
            "channel_id",
            "message_id",
            "host_id",
            "prize",
            "description",
            "winner_count",
            "required_role_id",
            "required_level",
            "status",
            "ends_at",
            "ended_at",
            "winners",
            "entry_count",
            "created_at",
        }
    ),
    "giveaway_entries": frozenset(
        {"entry_id", "giveaway_id", "user_id", "entries", "created_at"}
    ),
    "tickets": frozenset(
        {
            "ticket_id",
            "guild_id",
            "ticket_number",
            "channel_id",
            "user_id",
            "subject",
            "status",
            "claimed_by",
            "panel_message_id",
            "transcript",
            "closed_by",
            "close_reason",
            "created_at",
            "closed_at",
        }
    ),
    "custom_commands": frozenset(
        {
            "command_id",
            "guild_id",
            "name",
            "content",
            "description",
            "is_embed",
            "embed_color",
            "required_role_id",
            "delete_invocation",
            "uses",
            "created_by",
            "created_at",
            "updated_at",
        }
    ),
    "reaction_roles": frozenset(
        {
            "entry_id",
            "guild_id",
            "channel_id",
            "message_id",
            "emoji",
            "role_id",
            "mode",
            "group_key",
            "required_role_id",
            "created_by",
            "created_at",
        }
    ),
    "dashboard_sessions": frozenset(
        {
            "session_id",
            "user_id",
            "token_hash",
            "refresh_token_hash",
            "scopes",
            "ip_hash",
            "user_agent",
            "created_at",
            "last_seen_at",
            "expires_at",
            "revoked",
            "revoked_at",
        }
    ),
}

# Default conflict target for upserts.
PRIMARY_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "schema_meta": ("key",),
    "guild_settings": ("guild_id",),
    "moderation_cases": ("case_id",),
    "automod_rules": ("rule_id",),
    "economy_accounts": ("guild_id", "user_id"),
    "leveling_profiles": ("guild_id", "user_id"),
    "level_rewards": ("reward_id",),
    "giveaways": ("giveaway_id",),
    "giveaway_entries": ("entry_id",),
    "tickets": ("ticket_id",),
    "custom_commands": ("command_id",),
    "reaction_roles": ("entry_id",),
    "dashboard_sessions": ("session_id",),
}

# Tables whose rows require a ``guild_settings`` parent row to exist first.
GUILD_SCOPED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "moderation_cases",
        "automod_rules",
        "economy_accounts",
        "leveling_profiles",
        "level_rewards",
        "giveaways",
        "tickets",
        "custom_commands",
        "reaction_roles",
    }
)

# Mirrors of the CHECK constraints above, so callers can validate input and
# return a friendly message instead of surfacing an IntegrityError.
MODERATION_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "note",
        "warn",
        "mute",
        "unmute",
        "timeout",
        "untimeout",
        "kick",
        "ban",
        "softban",
        "unban",
        "purge",
        "lock",
        "unlock",
    }
)
AUTOMOD_RULE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "spam",
        "invite",
        "link",
        "caps",
        "mention",
        "word",
        "attachment",
        "emoji",
        "newline",
        "zalgo",
        "raid",
    }
)
AUTOMOD_ACTIONS: Final[frozenset[str]] = frozenset(
    {"log", "delete", "warn", "timeout", "kick", "ban"}
)
TICKET_STATUSES: Final[frozenset[str]] = frozenset({"open", "claimed", "closed"})
GIVEAWAY_STATUSES: Final[frozenset[str]] = frozenset({"active", "ended", "cancelled"})
REACTION_ROLE_MODES: Final[frozenset[str]] = frozenset(
    {"toggle", "add_only", "remove_only", "unique"}
)

__all__ = [
    "SCHEMA_VERSION",
    "LEGACY_SCHEMA",
    "CORE_SCHEMA",
    "INITIAL_SCHEMA",
    "TABLE_COLUMNS",
    "PRIMARY_KEYS",
    "GUILD_SCOPED_TABLES",
    "MODERATION_ACTIONS",
    "AUTOMOD_RULE_TYPES",
    "AUTOMOD_ACTIONS",
    "TICKET_STATUSES",
    "GIVEAWAY_STATUSES",
    "REACTION_ROLE_MODES",
]
