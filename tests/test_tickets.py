"""
Regression tests for the ticket panel storage layer.

The panel table is created by :meth:`TicketRepository.ensure_schema`, whose DDL
once contained a ``REFERENCESguild_settings`` typo that made the ``CREATE TABLE``
statement a hard SQLite parse error. That broke *every* ticket path, because
``get_config`` / ``save_panel`` / ``get_panel`` all call ``ensure_schema`` first.
These tests pin the DDL down and confirm the panel round-trips and cascades.
"""

import pytest

from fyrion.database.repositories.tickets import TicketRepository


@pytest.mark.asyncio
async def test_ensure_schema_creates_panel_table(db_manager):
    """ensure_schema must not raise and must create ticket_panels."""
    TicketRepository.reset_schema_flag()
    repo = TicketRepository(db_manager)

    # Would raise sqlite3.OperationalError on the fused-token DDL.
    await repo.ensure_schema()

    row = await db_manager.fetchrow(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'ticket_panels'"
    )
    assert row is not None


@pytest.mark.asyncio
async def test_panel_round_trips(db_manager):
    """A saved panel reads back with its stored presentation."""
    TicketRepository.reset_schema_flag()
    repo = TicketRepository(db_manager)
    guild_id = 424242

    assert await repo.get_panel(guild_id) is None

    await repo.save_panel(
        guild_id,
        channel_id=1000,
        message_id=2000,
        title="Support",
        description="Open a ticket below.",
        topics=[{"label": "Billing", "emoji": None}],
        created_by=3000,
    )

    panel = await repo.get_panel(guild_id)
    assert panel is not None
    assert panel["channel_id"] == 1000
    assert panel["message_id"] == 2000
    assert panel["title"] == "Support"


@pytest.mark.asyncio
async def test_panel_cascades_when_guild_deleted(db_manager):
    """The panel row cascades from guild_settings, so delete_guild clears it."""
    TicketRepository.reset_schema_flag()
    repo = TicketRepository(db_manager)
    guild_id = 515151

    await repo.save_panel(
        guild_id,
        channel_id=1,
        message_id=2,
        title="Support",
        description=None,
        topics=[],
        created_by=None,
    )
    assert await repo.get_panel(guild_id) is not None

    await db_manager.delete_guild(guild_id)
    assert await repo.get_panel(guild_id) is None
