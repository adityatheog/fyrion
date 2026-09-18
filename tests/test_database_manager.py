"""
Integration tests for the pooled database manager.

These run against an in-memory SQLite database, so they exercise the real
schema, the real PRAGMAs and the real SQL rather than mocks.
"""
import pytest
import pytest_asyncio

from fyrion.database.manager import (
    DatabasePool,
    InsufficientFundsError,
    MAX_FETCH_LIMIT,
    iso_from_now,
    utc_now_iso,
)
from fyrion.database.schema import SCHEMA_VERSION, TABLE_COLUMNS

GUILD_A = 100
GUILD_B = 200
USER = 555


@pytest_asyncio.fixture
async def pool():
    instance = DatabasePool(db_url=":memory:", pool_size=1, busy_timeout_ms=1000)
    await instance.connect()
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_schema_creates_every_declared_table(pool):
    rows = await pool.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    existing = {row["name"] for row in rows}
    for table in TABLE_COLUMNS:
        assert table in existing


@pytest.mark.asyncio
async def test_schema_version_recorded(pool):
    assert await pool.fetchval("PRAGMA user_version") == SCHEMA_VERSION
    stored = await pool.fetch_one("schema_meta", {"key": "schema_version"})
    assert stored is not None
    assert stored["value"] == str(SCHEMA_VERSION)


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(pool):
    assert await pool.fetchval("PRAGMA foreign_keys") == 1


@pytest.mark.asyncio
async def test_guild_settings_defaults(pool):
    settings = await pool.get_guild_settings(GUILD_A)
    assert settings["guild_id"] == GUILD_A
    assert settings["prefix"] == "!"
    assert settings["automod_enabled"] == 0

    await pool.update_guild_settings(GUILD_A, automod_enabled=1, prefix="?")
    updated = await pool.get_guild_settings(GUILD_A)
    assert updated["automod_enabled"] == 1
    assert updated["prefix"] == "?"


@pytest.mark.asyncio
async def test_unknown_table_and_column_are_rejected(pool):
    with pytest.raises(ValueError):
        await pool.fetch_one("guild_settings; DROP TABLE tickets", {})
    with pytest.raises(ValueError):
        await pool.update_guild_settings(GUILD_A, **{"prefix = 'x' --": 1})
    with pytest.raises(ValueError):
        await pool.fetch_many("guild_settings", order_by="guild_id; DROP TABLE x")


@pytest.mark.asyncio
async def test_unknown_table_rejected_on_no_column_no_where_paths(pool):
    """Table validation must run even when there is no column or filter to check."""
    with pytest.raises(ValueError):
        await pool.fetch_one("nonexistent_table", {})
    with pytest.raises(ValueError):
        await pool.fetch_many("nonexistent_table")
    with pytest.raises(ValueError):
        await pool.count("nonexistent_table")
    with pytest.raises(ValueError):
        await pool.exists("nonexistent_table", {})
    with pytest.raises(ValueError):
        await pool.delete("nonexistent_table", allow_full_table=True)


@pytest.mark.asyncio
async def test_update_and_delete_refuse_unfiltered_writes(pool):
    await pool.get_guild_settings(GUILD_A)
    with pytest.raises(ValueError):
        await pool.update("guild_settings", {"prefix": "x"})
    with pytest.raises(ValueError):
        await pool.delete("guild_settings")
    assert await pool.count("guild_settings") == 1


@pytest.mark.asyncio
async def test_case_numbers_are_sequential_per_guild(pool):
    first = await pool.create_moderation_case(
        guild_id=GUILD_A,
        action="warn",
        target_id=USER,
        moderator_id=1,
        reason="Spam",
    )
    second = await pool.create_moderation_case(
        guild_id=GUILD_A, action="kick", target_id=USER, moderator_id=1
    )
    other_guild = await pool.create_moderation_case(
        guild_id=GUILD_B, action="warn", target_id=USER, moderator_id=1
    )

    assert first["case_number"] == 1
    assert second["case_number"] == 2
    assert other_guild["case_number"] == 1

    # Guild isolation: guild B must not see guild A's history.
    assert len(await pool.get_member_cases(GUILD_A, USER)) == 2
    assert len(await pool.get_member_cases(GUILD_B, USER)) == 1


@pytest.mark.asyncio
async def test_invalid_moderation_action_rejected(pool):
    with pytest.raises(ValueError):
        await pool.create_moderation_case(
            guild_id=GUILD_A, action="vaporize", target_id=USER, moderator_id=1
        )


@pytest.mark.asyncio
async def test_expired_cases_and_resolution(pool):
    await pool.create_moderation_case(
        guild_id=GUILD_A,
        action="timeout",
        target_id=USER,
        moderator_id=1,
        expires_at=iso_from_now(-60),
    )

    due = await pool.get_expired_cases()
    assert len(due) == 1

    assert await pool.resolve_moderation_case(GUILD_A, due[0]["case_number"], 1)
    assert await pool.get_expired_cases() == []
    # Resolving twice is a no-op rather than an error.
    assert not await pool.resolve_moderation_case(GUILD_A, due[0]["case_number"], 1)


@pytest.mark.asyncio
async def test_deleting_a_guild_cascades(pool):
    await pool.create_moderation_case(
        guild_id=GUILD_A, action="warn", target_id=USER, moderator_id=1
    )
    await pool.get_economy_account(GUILD_A, USER)
    await pool.create_support_ticket(
        guild_id=GUILD_A, channel_id=9001, user_id=USER
    )

    await pool.delete_guild(GUILD_A)

    assert await pool.count("moderation_cases", {"guild_id": GUILD_A}) == 0
    assert await pool.count("economy_accounts", {"guild_id": GUILD_A}) == 0
    assert await pool.count("tickets", {"guild_id": GUILD_A}) == 0


@pytest.mark.asyncio
async def test_economy_credit_debit_and_overdraft(pool):
    assert await pool.adjust_balance(GUILD_A, USER, 500) == 500
    assert await pool.adjust_balance(GUILD_A, USER, -200) == 300

    with pytest.raises(InsufficientFundsError):
        await pool.adjust_balance(GUILD_A, USER, -1000)

    account = await pool.get_economy_account(GUILD_A, USER)
    assert account["balance"] == 300
    assert account["total_earned"] == 500
    assert account["total_spent"] == 200


@pytest.mark.asyncio
async def test_economy_transfer(pool):
    await pool.adjust_balance(GUILD_A, USER, 100)
    await pool.transfer_balance(GUILD_A, USER, 777, 60)

    sender = await pool.get_economy_account(GUILD_A, USER)
    recipient = await pool.get_economy_account(GUILD_A, 777)
    assert sender["balance"] == 40
    assert recipient["balance"] == 60

    with pytest.raises(InsufficientFundsError):
        await pool.transfer_balance(GUILD_A, USER, 777, 10_000)
    with pytest.raises(ValueError):
        await pool.transfer_balance(GUILD_A, USER, USER, 5)


@pytest.mark.asyncio
async def test_add_xp_upserts_and_accumulates(pool):
    first = await pool.add_xp(GUILD_A, USER, 15)
    second = await pool.add_xp(GUILD_A, USER, 10)

    assert first["xp"] == 15
    assert second["xp"] == 25
    assert second["total_messages"] == 2

    await pool.set_level(GUILD_A, USER, 3)
    profile = await pool.get_leveling_profile(GUILD_A, USER)
    assert profile["level"] == 3

    board = await pool.leveling_leaderboard(GUILD_A)
    assert board[0]["user_id"] == USER


@pytest.mark.asyncio
async def test_level_rewards_filtering(pool):
    await pool.ensure_guild(GUILD_A)
    for level, role_id in ((5, 11), (10, 12), (20, 13)):
        await pool.insert(
            "level_rewards",
            {"guild_id": GUILD_A, "level": level, "role_id": role_id},
        )

    earned = await pool.get_level_rewards(GUILD_A, up_to_level=10)
    assert [row["role_id"] for row in earned] == [11, 12]
    assert len(await pool.get_level_rewards(GUILD_A)) == 3


@pytest.mark.asyncio
async def test_giveaway_entries_are_deduplicated(pool):
    await pool.ensure_guild(GUILD_A)
    giveaway_id = await pool.insert(
        "giveaways",
        {
            "guild_id": GUILD_A,
            "channel_id": 1,
            "message_id": 2,
            "host_id": 3,
            "prize": "Nitro",
            "ends_at": iso_from_now(-5),
        },
    )

    assert await pool.add_giveaway_entry(giveaway_id, USER)
    assert not await pool.add_giveaway_entry(giveaway_id, USER)

    giveaway = await pool.fetch_one("giveaways", {"giveaway_id": giveaway_id})
    assert giveaway["entry_count"] == 1

    due = await pool.get_due_giveaways()
    assert [row["giveaway_id"] for row in due] == [giveaway_id]

    assert await pool.remove_giveaway_entry(giveaway_id, USER)
    giveaway = await pool.fetch_one("giveaways", {"giveaway_id": giveaway_id})
    assert giveaway["entry_count"] == 0


@pytest.mark.asyncio
async def test_ticket_lifecycle(pool):
    ticket = await pool.create_support_ticket(
        guild_id=GUILD_A, channel_id=4242, user_id=USER, subject="Help"
    )
    assert ticket["ticket_number"] == 1
    assert ticket["status"] == "open"

    assert await pool.get_open_ticket(GUILD_A, USER) is not None
    assert await pool.claim_ticket(4242, 999)
    assert await pool.close_support_ticket(4242, closed_by=999, close_reason="done")
    assert await pool.get_open_ticket(GUILD_A, USER) is None
    # Closing an already closed ticket reports failure instead of raising.
    assert not await pool.close_support_ticket(4242)


@pytest.mark.asyncio
async def test_custom_commands_lookup_is_case_insensitive(pool):
    await pool.ensure_guild(GUILD_A)
    command_id = await pool.insert(
        "custom_commands",
        {"guild_id": GUILD_A, "name": "rules", "content": "Read #rules"},
    )

    found = await pool.get_custom_command(GUILD_A, "RULES")
    assert found is not None
    assert found["command_id"] == command_id

    await pool.bump_custom_command_uses(command_id)
    refreshed = await pool.get_custom_command(GUILD_A, "rules")
    assert refreshed["uses"] == 1


@pytest.mark.asyncio
async def test_reaction_role_uniqueness_and_groups(pool):
    await pool.ensure_guild(GUILD_A)
    await pool.insert(
        "reaction_roles",
        {
            "guild_id": GUILD_A,
            "channel_id": 1,
            "message_id": 2,
            "emoji": "green",
            "role_id": 10,
            "mode": "unique",
            "group_key": "colors",
        },
    )
    await pool.insert(
        "reaction_roles",
        {
            "guild_id": GUILD_A,
            "channel_id": 1,
            "message_id": 2,
            "emoji": "blue",
            "role_id": 11,
            "mode": "unique",
            "group_key": "colors",
        },
    )

    mapping = await pool.get_reaction_role(2, "blue")
    assert mapping["role_id"] == 11
    assert len(await pool.get_reaction_role_group(GUILD_A, "colors")) == 2

    # The same emoji cannot map twice on one message.
    assert (
        await pool.insert(
            "reaction_roles",
            {
                "guild_id": GUILD_A,
                "channel_id": 1,
                "message_id": 2,
                "emoji": "blue",
                "role_id": 12,
            },
            on_conflict="ignore",
        )
        is None
    )


@pytest.mark.asyncio
async def test_dashboard_session_lifecycle(pool):
    await pool.create_dashboard_session(
        session_id="sid-1",
        user_id=USER,
        token_hash="hash-1",
        expires_at=iso_from_now(3600),
        scopes="identify guilds",
    )

    session = await pool.get_active_session("hash-1")
    assert session is not None
    assert session["user_id"] == USER

    assert await pool.touch_session("hash-1")
    assert await pool.revoke_session("hash-1")
    assert await pool.get_active_session("hash-1") is None


@pytest.mark.asyncio
async def test_expired_sessions_are_not_active_and_get_purged(pool):
    await pool.create_dashboard_session(
        session_id="sid-old",
        user_id=USER,
        token_hash="hash-old",
        expires_at=iso_from_now(-10),
    )
    assert await pool.get_active_session("hash-old") is None
    assert await pool.purge_expired_sessions() == 1
    assert await pool.count("dashboard_sessions") == 0


@pytest.mark.asyncio
async def test_upsert_and_increment_helpers(pool):
    await pool.upsert(
        "economy_accounts",
        {"guild_id": GUILD_A, "user_id": USER, "balance": 10},
    )
    await pool.upsert(
        "economy_accounts",
        {"guild_id": GUILD_A, "user_id": USER, "balance": 25},
    )
    account = await pool.get_economy_account(GUILD_A, USER)
    assert account["balance"] == 25

    await pool.increment(
        "economy_accounts", "bank", 40, {"guild_id": GUILD_A, "user_id": USER}
    )
    account = await pool.get_economy_account(GUILD_A, USER)
    assert account["bank"] == 40


@pytest.mark.asyncio
async def test_fetch_many_limits_are_validated(pool):
    await pool.ensure_guild(GUILD_A)
    with pytest.raises(ValueError):
        await pool.fetch_many("guild_settings", limit=0)
    with pytest.raises(ValueError):
        await pool.fetch_many("guild_settings", limit=10, offset=-1)


def test_absent_limit_is_capped_not_unbounded():
    """A fetch with no limit still emits LIMIT MAX_FETCH_LIMIT, so no caller can
    scan an entire table into memory."""
    assert DatabasePool._limit_clause(None, None) == f" LIMIT {MAX_FETCH_LIMIT}"
    assert DatabasePool._limit_clause(None, 5) == f" LIMIT {MAX_FETCH_LIMIT} OFFSET 5"
    assert DatabasePool._limit_clause(25, None) == " LIMIT 25"


@pytest.mark.asyncio
async def test_stats_and_ping(pool):
    assert await pool.ping() is True
    info = await pool.stats()
    assert info["connected"] is True
    assert info["pool_size"] == 1
    assert info["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_queries_fail_cleanly_after_close():
    instance = DatabasePool(db_url=":memory:", pool_size=1)
    await instance.connect()
    await instance.close()

    assert instance.is_connected is False
    assert await instance.ping() is False


def test_timestamp_helpers_format():
    now = utc_now_iso()
    assert now.endswith("Z")
    assert len(now) == 20
    assert iso_from_now(60) > now
