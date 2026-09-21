"""
Shared fixtures for Fyrion's pytest suite.
"""

import os

os.environ.setdefault("DISCORD_TOKEN", "test_token_for_local_pytest_runs")
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("DATABASE_URL", ":memory:")
os.environ.setdefault("LOG_TO_FILE", "false")
os.environ.setdefault("DASHBOARD_ENABLED", "false")

from unittest.mock import MagicMock  # noqa: E402

import discord  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from fyrion.config import Config  # noqa: E402
from fyrion.database.manager import DatabasePool  # noqa: E402

# Force tests to use an in-memory SQLite database instead of a file.
Config.DATABASE_URL = ":memory:"


@pytest_asyncio.fixture
async def db_manager():
    """Provides a connected, initialized in-memory database pool."""
    manager = DatabasePool(db_url=":memory:", pool_size=1, busy_timeout_ms=1000)
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
    """Factory fixture to create mock Members with specific role positions."""

    def _create(member_id: int, top_role_position: int, is_bot: bool = False):
        member = MagicMock(spec=discord.Member)
        member.id = member_id
        member.guild = mock_guild

        # Mock top_role with a position so hierarchy comparisons work.
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
