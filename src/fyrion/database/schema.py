"""
Database schema definitions.
Using explicit IF NOT EXISTS to allow safe startup re-evaluations.
"""

INITIAL_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS whitelists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL, -- 'user', 'role', 'channel'
    FOREIGN KEY (guild_id) REFERENCES guild_configs (guild_id) ON DELETE CASCADE,
    UNIQUE(guild_id, entity_id, entity_type)
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
