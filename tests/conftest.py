"""
Shared fixtures for Fyrion's pytest suite.
"""
import pytest
import pytest_asyncio
from unittest.mock import MagicMock
import discord

from fyrion.config import Config
from fyrion.database.connection import DatabaseManager

# Force tests to use an in-memory SQLite database instead of a file
Config.DATABASE_URL = ":memory:"

@pytest_asyncio.fixture
async def db_manager():
    """Provides a connected, initialized in-memory database manager."""
    manager = DatabaseManager()
    await manager.connect()
    yield manager
    await manager.close()

@pytest.fixture
def mock_guild():
    """Provides a standard mock Discord Guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 123456789
    guild.owner_id = 111111111
    return guild

@pytest.fixture
def create_mock_member(mock_guild):
    """Factory fixture to create mock Discord Members with specific role positions."""
    def _create(member_id: int, top_role_position: int, is_bot: bool = False):
        member = MagicMock(spec=discord.Member)
        member.id = member_id
        member.guild = mock_guild
        
        # Mock the top_role attribute with a specific position for hierarchy comparison
        mock_role = MagicMock(spec=discord.Role)
        mock_role.__ge__ = lambda self, other: top_role_position >= other.position
        mock_role.__gt__ = lambda self, other: top_role_position > other.position
        mock_role.__le__ = lambda self, other: top_role_position <= other.position
        mock_role.__lt__ = lambda self, other: top_role_position < other.position
        mock_role.position = top_role_position
        
        member.top_role = mock_role
        
        if is_bot:
            mock_guild.me = member
            
        return member
    return _create
