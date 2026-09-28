from __future__

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from dashboard import app as dashboard_app
from dashboard.app import (
    GuildAccess,
    SESSION_CSRF,
    SESSION_GUILDS,
    SESSION_USER,
)
from fyrion.web.models import GuildSettingsUpdate


class FakeRole:
    def __init__(self, *, position: int, default: bool = False):
        self.position = position
        self._default = default
        self.permissions = SimpleNamespace(administrator=False)

    def is_default(self):
        return self._default

    def __le__(self, other):
        return self.position <= other.position


async def _seed_settings(db_manager, guild_id: int, **values):
    await db_manager.ensure_guild(guild_id)
    await db_manager.update_guild_settings(guild_id, **values)


def _request(db_manager, guild_id: int, *, csrf: str = "csrf", user_id: str = "42"):
    session = {
        SESSION_USER: {"id": user_id},
        SESSION_GUILDS: [
            {"id": str(guild_id), "name": "Test Guild", "owner": False, "permissions": 8}
        ],
        SESSION_CSRF: csrf,
    }
    return SimpleNamespace(
        session=session,
        headers={"x-csrf-token": csrf},
        app=SimpleNamespace(state=SimpleNamespace(db=db_manager, bot=None)),
    )


@pytest.mark.asyncio
async def test_detached_channel_update_is_rejected(db_manager):
    guild_id = 1001
    await _seed_settings(db_manager, guild_id, mod_log_channel_id=55)
    request = _request(db_manager, guild_id)

    with pytest.raises(HTTPException) as excinfo:
        await dashboard_app.api_update_guild(
            guild_id,
            GuildSettingsUpdate(mod_log_channel_id=123),
            request,
        )

    assert excinfo.value.status_code == 400
    settings = await db_manager.get_guild_settings(guild_id)
    assert settings["mod_log_channel_id"] == 55


@pytest.mark.asyncio
async def test_detached_role_update_is_rejected(db_manager):
    guild_id = 1002
    await _seed_settings(db_manager, guild_id, mute_role_id=10)
    request = _request(db_manager, guild_id)

    with pytest.raises(HTTPException) as excinfo:
        await dashboard_app.api_update_guild(
            guild_id,
            GuildSettingsUpdate(mute_role_id=77),
            request,
        )

    assert excinfo.value.status_code == 400
    settings = await db_manager.get_guild_settings(guild_id)
    assert settings["mute_role_id"] == 10


@pytest.mark.asyncio
async def test_detached_scalar_update_still_succeeds(db_manager):
    guild_id = 1003
    await _seed_settings(db_manager, guild_id, economy_daily_amount=3)
    request = _request(db_manager, guild_id)

    result = await dashboard_app.api_update_guild(
        guild_id,
        GuildSettingsUpdate(economy_daily_amount=25),
        request,
    )

    assert result["updated"] == ["economy_daily_amount"]
    settings = await db_manager.get_guild_settings(guild_id)
    assert settings["economy_daily_amount"] == 25


@pytest.mark.asyncio
async def test_live_guild_channel_validation_accepts_valid_and_rejects_invalid(db_manager):
    guild_id = 1004
    await _seed_settings(db_manager, guild_id, mod_log_channel_id=11)
    request = _request(db_manager, guild_id)

    valid_channel = object()
    guild = MagicMock()
    guild.get_channel.side_effect = lambda channel_id: valid_channel if channel_id == 123 else None
    entry = {"id": str(guild_id), "name": "Live Guild", "owner": False, "permissions": 8}
    guild_access = GuildAccess(guild_id, guild, None, entry)

    with patch.object(dashboard_app, "authorize_guild", AsyncMock(return_value=guild_access)):
        result = await dashboard_app.api_update_guild(
            guild_id,
            GuildSettingsUpdate(mod_log_channel_id=123),
            request,
        )
        assert result["settings"]["mod_log_channel_id"] == 123

    with pytest.raises(HTTPException) as excinfo:
        with patch.object(dashboard_app, "authorize_guild", AsyncMock(return_value=guild_access)):
            await dashboard_app.api_update_guild(
                guild_id,
                GuildSettingsUpdate(mod_log_channel_id=999),
                request,
            )
    assert excinfo.value.status_code == 400
    settings = await db_manager.get_guild_settings(guild_id)
    assert settings["mod_log_channel_id"] == 123


@pytest.mark.asyncio
async def test_live_guild_role_validation_rejects_missing_and_protected_roles(db_manager):
    guild_id = 1005
    await _seed_settings(db_manager, guild_id, autorole_id=7)
    request = _request(db_manager, guild_id)

    guild = MagicMock()
    guild.me = MagicMock()
    guild.me.top_role = FakeRole(position=10)
    valid_role = FakeRole(position=5)
    default_role = FakeRole(position=1, default=True)
    hierarchy_role = FakeRole(position=12)
    guild.get_role.side_effect = lambda role_id: {
        7: valid_role,
        99: default_role,
        100: hierarchy_role,
    }.get(role_id)

    entry = {"id": str(guild_id), "name": "Live Guild", "owner": False, "permissions": 8}
    guild_access = GuildAccess(guild_id, guild, None, entry)

    with patch.object(dashboard_app, "authorize_guild", AsyncMock(return_value=guild_access)):
        result = await dashboard_app.api_update_guild(
            guild_id,
            GuildSettingsUpdate(autorole_id=7),
            request,
        )
        assert result["settings"]["autorole_id"] == 7

    with pytest.raises(HTTPException) as excinfo:
        with patch.object(dashboard_app, "authorize_guild", AsyncMock(return_value=guild_access)):
            await dashboard_app.api_update_guild(
                guild_id,
                GuildSettingsUpdate(autorole_id=999),
                request,
            )
    assert excinfo.value.status_code == 400

    with pytest.raises(HTTPException):
        with patch.object(dashboard_app, "authorize_guild", AsyncMock(return_value=guild_access)):
            await dashboard_app.api_update_guild(
                guild_id,
                GuildSettingsUpdate(autorole_id=99),
                request,
            )

    with pytest.raises(HTTPException):
        with patch.object(dashboard_app, "authorize_guild", AsyncMock(return_value=guild_access)):
            await dashboard_app.api_update_guild(
                guild_id,
                GuildSettingsUpdate(autorole_id=100),
                request,
            )

    settings = await db_manager.get_guild_settings(guild_id)
    assert settings["autorole_id"] == 7


@pytest.mark.asyncio
async def test_no_partial_update_when_object_field_is_invalid(db_manager):
    guild_id = 1006
    await _seed_settings(db_manager, guild_id, mod_log_channel_id=1, economy_daily_amount=9)
    request = _request(db_manager, guild_id)

    with pytest.raises(HTTPException) as excinfo:
        await dashboard_app.api_update_guild(
            guild_id,
            GuildSettingsUpdate(mod_log_channel_id=999, economy_daily_amount=42),
            request,
        )

    assert excinfo.value.status_code == 400
    settings = await db_manager.get_guild_settings(guild_id)
    assert settings["mod_log_channel_id"] == 1
    assert settings["economy_daily_amount"] == 9
