"""
Integration tests for the database repositories and guild isolation.
"""
import pytest
from fyrion.database.repositories.guild_config import GuildConfigRepository
from fyrion.database.repositories.invites import InviteRepository
from fyrion.database.repositories.security import SecurityRepository
from fyrion.database.repositories.warnings import WarningsRepository

@pytest.mark.asyncio
async def test_guild_config_auto_creation(db_manager):
    repo = GuildConfigRepository(db_manager)

    # Fetching a config for an unknown guild should automatically create it
    config = await repo.get_config(12345)

    assert config is not None
    assert config["guild_id"] == 12345
    assert config["welcome_channel_id"] is None  # Default value

@pytest.mark.asyncio
async def test_guild_config_update(db_manager):
    repo = GuildConfigRepository(db_manager)

    # Update the welcome channel and read it back
    await repo.update_config(12345, "welcome_channel_id", 555)
    config = await repo.get_config(12345)

    assert config["welcome_channel_id"] == 555

@pytest.mark.asyncio
async def test_guild_config_invalid_column(db_manager):
    repo = GuildConfigRepository(db_manager)

    # Attempting to update a non-whitelisted key should raise ValueError (SQL Injection protection)
    with pytest.raises(ValueError):
        await repo.update_config(12345, "DROP TABLE guild_settings", "value")

@pytest.mark.asyncio
async def test_guild_config_settings_round_trip(db_manager):
    """Welcome, log and autorole settings survive a round trip through the
    migrated repository, which is now backed by ``guild_settings``.

    ``log_channel_id`` in particular must map onto the core
    ``mod_log_channel_id`` column and read back under its legacy key.
    """
    repo = GuildConfigRepository(db_manager)

    await repo.update_config(777, "welcome_channel_id", 1001)
    await repo.update_config(777, "log_channel_id", 2002)
    await repo.update_config(777, "autorole_id", 3003)

    config = await repo.get_config(777)
    assert config["welcome_channel_id"] == 1001
    assert config["log_channel_id"] == 2002
    assert config["autorole_id"] == 3003

    # The log channel is physically stored on the core column.
    settings = await db_manager.get_guild_settings(777)
    assert settings["mod_log_channel_id"] == 2002

    # Clearing a value round-trips as None, not as a stale cached read.
    await repo.update_config(777, "welcome_channel_id", None)
    config = await repo.get_config(777)
    assert config["welcome_channel_id"] is None

@pytest.mark.asyncio
async def test_guild_isolation_warnings(db_manager):
    repo = WarningsRepository(db_manager)
    
    # We must explicitly insert the guild_configs first due to our ON DELETE CASCADE Foreign Keys
    config_repo = GuildConfigRepository(db_manager)
    await config_repo.get_config(100) # Guild A
    await config_repo.get_config(200) # Guild B
    
    # Add a warning in Guild A
    await repo.add_warning(guild_id=100, user_id=111, moderator_id=999, reason="Spam")
    
    # The warning should exist in Guild A
    warnings_a = await repo.get_warnings(guild_id=100, user_id=111)
    assert len(warnings_a) == 1
    
    # The warning MUST NOT exist in Guild B for the same user
    warnings_b = await repo.get_warnings(guild_id=200, user_id=111)
    assert len(warnings_b) == 0


@pytest.mark.asyncio
async def test_whitelist_add_on_fresh_guild(db_manager):
    """A whitelist entry can be added for a guild that has never been seen.

    ``whitelists`` has a foreign key onto ``guild_configs``; the repository must
    create that parent row itself, since ``ensure_guild`` no longer does.
    """
    repo = SecurityRepository(db_manager)

    # Guild 4242 has no guild_configs row yet: this must not silently no-op.
    await repo.add_whitelist(4242, 8001, "role")

    rows = await repo.get_whitelists(4242)
    assert len(rows) == 1
    assert rows[0]["entity_id"] == 8001
    assert rows[0]["entity_type"] == "role"


@pytest.mark.asyncio
async def test_invite_join_on_fresh_guild(db_manager):
    """Invite tracking works for a guild with no pre-existing parent row.

    ``member_inviters`` and ``invite_stats`` both hold a foreign key onto
    ``guild_configs``; a missing parent would otherwise raise on the plain
    INSERT into ``invite_stats``.
    """
    repo = InviteRepository(db_manager)

    await repo.add_join(guild_id=5252, joined_user_id=1, inviter_id=42)

    stats = await repo.get_stats(5252, 42)
    assert stats["joins"] == 1
    assert stats["leaves"] == 0
    assert stats["net"] == 1


@pytest.mark.asyncio
async def test_audit_and_mod_log_are_independent(db_manager):
    """Setting the audit-log channel must not change the mod-log channel, and
    vice versa. They live on separate ``guild_settings`` columns.
    """
    # The audit-log surface (/logs, /set-logchannel) writes audit_log_channel_id.
    await db_manager.update_guild_settings(9001, audit_log_channel_id=111)
    # The mod-log surface (/set-modlog) writes mod_log_channel_id.
    await db_manager.update_guild_settings(9001, mod_log_channel_id=222)

    settings = await db_manager.get_guild_settings(9001)
    assert settings["audit_log_channel_id"] == 111
    assert settings["mod_log_channel_id"] == 222

    # Changing the audit-log channel leaves the mod log untouched.
    await db_manager.update_guild_settings(9001, audit_log_channel_id=333)
    settings = await db_manager.get_guild_settings(9001)
    assert settings["audit_log_channel_id"] == 333
    assert settings["mod_log_channel_id"] == 222

    # The migrated GuildConfigRepository still targets the mod log, so it never
    # clobbers the audit-log channel.
    config_repo = GuildConfigRepository(db_manager)
    await config_repo.update_config(9001, "log_channel_id", 444)
    settings = await db_manager.get_guild_settings(9001)
    assert settings["mod_log_channel_id"] == 444
    assert settings["audit_log_channel_id"] == 333
