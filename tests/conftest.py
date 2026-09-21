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


def _make_mock_role(position: int) -> "MagicMock":
    """Builds a mock Role whose comparison operators use ``position``.

    Discord's ``Role`` orders by position, so the hierarchy helpers compare
    roles with ``<=``/``>``. Only the ordering is wired here; callers that need
    ``is_default``/``managed``/``permissions`` set them on the returned mock.
    """
    role = MagicMock(spec=discord.Role)
    role.__ge__ = lambda self, other: position >= other.position
    role.__gt__ = lambda self, other: position > other.position
    role.__le__ = lambda self, other: position <= other.position
    role.__lt__ = lambda self, other: position < other.position
    role.position = position
    return role


@pytest.fixture
def make_mock_role():
    """Factory for a standalone mock Role at a given hierarchy position."""
    return _make_mock_role


@pytest.fixture
def create_mock_member(mock_guild):
    """Factory fixture to create mock Members with specific role positions."""

    def _create(
        member_id: int,
        top_role_position: int,
        is_bot: bool = False,
        *,
        manage_roles: bool = False,
        administrator: bool = False,
    ):
        member = MagicMock(spec=discord.Member)
        member.id = member_id
        member.guild = mock_guild

        # Mock top_role with a position so hierarchy comparisons work.
        member.top_role = _make_mock_role(top_role_position)

        # Guild-level permissions consulted by the role-management helpers.
        member.guild_permissions.manage_roles = manage_roles
        member.guild_permissions.administrator = administrator

        if is_bot:
            mock_guild.me = member

        return member

    return _create
