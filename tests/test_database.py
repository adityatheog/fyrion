"""
Integration tests for the database repositories and guild isolation.
"""
import pytest
from fyrion.database.repositories.guild_config import GuildConfigRepository
from fyrion.database.repositories.warnings import WarningsRepository

@pytest.mark.asyncio
async def test_guild_config_auto_creation(db_manager):
    repo = GuildConfigRepository(db_manager)
    
    # Fetching a config for an unknown guild should automatically create it
    config = await repo.get_config(12345)
    
    assert config is not None
    assert config["guild_id"] == 12345
    assert config["anti_link_enabled"] == 0 # Default value

@pytest.mark.asyncio
async def test_guild_config_update(db_manager):
    repo = GuildConfigRepository(db_manager)
    
    # Update anti-link
    await repo.update_config(12345, "anti_link_enabled", 1)
    config = await repo.get_config(12345)
    
    assert config["anti_link_enabled"] == 1

@pytest.mark.asyncio
async def test_guild_config_invalid_column(db_manager):
    repo = GuildConfigRepository(db_manager)
    
    # Attempting to update a non-whitelisted column should raise ValueError (SQL Injection protection)
    with pytest.raises(ValueError):
        await repo.update_config(12345, "DROP TABLE guild_configs", "value")

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
