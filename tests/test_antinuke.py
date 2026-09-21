"""
Tests for the AntiNuke detector and its repository.

The detector is driven directly through :meth:`AntiNuke.handle_action` with a
fake guild and a fake event stream, so no live gateway is needed. The database
is a real in-memory pool, so moderation cases and settings round-trip through
the actual schema and CRUD helpers.
"""

import time

import pytest
import pytest_asyncio

from fyrion.cogs.antinuke import AntiNuke
from fyrion.database.manager import DatabasePool
from fyrion.database.repositories.antinuke import AntiNukeRepository
from fyrion.utils.ratelimit import SlidingWindow

GUILD_ID = 4000
OWNER_ID = 1
BOT_ID = 2
ACTOR_ID = 555
TRUSTED_ID = 777


@pytest_asyncio.fixture
async def pool():
    instance = DatabasePool(db_url=":memory:", pool_size=1, busy_timeout_ms=1000)
    await instance.connect()
    yield instance
    await instance.close()


@pytest.fixture(autouse=True)
def _reset_generations():
    AntiNukeRepository.reset_generations()
    yield
    AntiNukeRepository.reset_generations()


class FakeRole:
    def __init__(self, position, *, default=False, managed=False):
        self.position = position
        self._default = default
        self.managed = managed

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position


class FakeMember:
    def __init__(self, member_id, top_position=1, roles=None):
        self.id = member_id
        self.top_role = FakeRole(top_position)
        self.roles = roles if roles is not None else []
        self.removed = None
        self.kicked = False
        self.guild = None

    async def remove_roles(self, *roles, reason=None):
        self.removed = list(roles)

    async def kick(self, reason=None):
        self.kicked = True

    def __str__(self):
        return f"Actor#{self.id}"


class FakePermissions:
    def __init__(self):
        self.ban_members = True
        self.kick_members = True
        self.manage_roles = True


class FakeGuild:
    def __init__(self, owner_id=OWNER_ID, bot_id=BOT_ID):
        self.id = GUILD_ID
        self.owner_id = owner_id
        self._members = {}
        me = FakeMember(bot_id, top_position=100)
        me.guild_permissions = FakePermissions()
        me.guild = self
        self.me = me
        self.bans = []

    def add_member(self, member):
        member.guild = self
        self._members[member.id] = member
        return member

    def get_member(self, member_id):
        return self._members.get(member_id)

    async def ban(self, target, reason=None, delete_message_seconds=0):
        self.bans.append(target.id)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeBot:
    def __init__(self, db, bot_id=BOT_ID):
        self.db = db
        self.user = FakeUser(bot_id)

    def get_cog(self, name):
        return None


def _make_cog(pool):
    """Builds an AntiNuke cog without starting its pruning loop."""
    cog = AntiNuke.__new__(AntiNuke)
    cog.bot = FakeBot(pool)
    cog.db = pool
    cog.repo = AntiNukeRepository(pool)
    cog._policies = {}
    cog._windows = {}
    cog._punished = {}
    return cog


async def _arm(pool, **overrides):
    repo = AntiNukeRepository(pool)
    values = {"enabled": True, "window_seconds": 30, "punishment": "ban"}
    values.update(overrides)
    await repo.save_settings(GUILD_ID, values)


# ---------------------------------------------------------------------------
# SlidingWindow
# ---------------------------------------------------------------------------


def test_sliding_window_counts_within_window():
    window = SlidingWindow()
    assert window.hit(0.0, 10.0) == 1
    assert window.hit(1.0, 10.0) == 2
    assert window.hit(2.0, 10.0) == 3
    # At now=11 the cutoff is 1.0, so 0.0 and 1.0 age out; 2.0 and 11.0 remain.
    assert window.hit(11.0, 10.0) == 2
    # At now=100 every earlier event is long gone.
    assert window.hit(100.0, 10.0) == 1


def test_sliding_window_idle_and_reset():
    window = SlidingWindow()
    window.hit(0.0, 10.0)
    assert not window.is_idle(5.0, 10.0)
    assert window.is_idle(20.0, 10.0)
    window.hit(20.0, 10.0)
    window.reset()
    assert window.is_idle(20.0, 10.0)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_round_trip_and_seed_defaults(pool):
    repo = AntiNukeRepository(pool)
    saved = await repo.save_settings(GUILD_ID, {"enabled": True})
    assert saved["enabled"] == 1
    # Defaults seeded on first write.
    assert saved["ban_threshold"] == 3
    assert saved["punishment"] == "strip_roles"

    await repo.set_threshold(GUILD_ID, "ban", 7)
    reread = await repo.get_settings(GUILD_ID)
    assert reread["ban_threshold"] == 7
    # Unrelated fields survive a partial update.
    assert reread["kick_threshold"] == 5


@pytest.mark.asyncio
async def test_settings_reject_bad_values(pool):
    repo = AntiNukeRepository(pool)
    with pytest.raises(ValueError):
        await repo.save_settings(GUILD_ID, {"punishment": "explode"})
    with pytest.raises(ValueError):
        await repo.set_threshold(GUILD_ID, "ban", 9999)
    with pytest.raises(ValueError):
        await repo.save_settings(GUILD_ID, {"nonexistent_field": 1})


@pytest.mark.asyncio
async def test_clear_threshold_stops_watching(pool):
    repo = AntiNukeRepository(pool)
    await repo.save_settings(GUILD_ID, {"enabled": True})
    row = await repo.clear_threshold(GUILD_ID, "ban")
    assert row["ban_threshold"] is None


@pytest.mark.asyncio
async def test_whitelist_add_remove(pool):
    repo = AntiNukeRepository(pool)
    assert await repo.add_whitelist(GUILD_ID, TRUSTED_ID) is True
    assert await repo.add_whitelist(GUILD_ID, TRUSTED_ID) is False  # duplicate
    assert TRUSTED_ID in await repo.get_whitelist(GUILD_ID)
    assert await repo.remove_whitelist(GUILD_ID, TRUSTED_ID) is True
    assert TRUSTED_ID not in await repo.get_whitelist(GUILD_ID)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_triggers_exactly_one_punishment_and_case(pool):
    await _arm(pool, punishment="ban", ban_threshold=3)
    cog = _make_cog(pool)
    guild = FakeGuild()

    results = []
    for _ in range(6):  # well past the threshold of 3
        results.append(await cog.handle_action(guild, ACTOR_ID, "ban"))

    # Exactly one call in the burst reported a punishment.
    assert results.count(True) == 1
    # And it fired on the third event, not before.
    assert results == [False, False, True, False, False, False]

    # Exactly one ban was issued.
    assert guild.bans == [ACTOR_ID]

    # Exactly one moderation case was recorded.
    count = await pool.count(
        "moderation_cases", {"guild_id": GUILD_ID, "target_id": ACTOR_ID}
    )
    assert count == 1


@pytest.mark.asyncio
async def test_below_threshold_does_nothing(pool):
    await _arm(pool, punishment="ban", ban_threshold=5)
    cog = _make_cog(pool)
    guild = FakeGuild()

    for _ in range(4):  # one short of the threshold
        assert await cog.handle_action(guild, ACTOR_ID, "ban") is False

    assert guild.bans == []
    assert await pool.count("moderation_cases", {"guild_id": GUILD_ID}) == 0


@pytest.mark.asyncio
async def test_unwatched_action_is_ignored(pool):
    # ban is watched; role_create threshold is left null (not watched).
    await _arm(pool, punishment="ban", ban_threshold=3)
    repo = AntiNukeRepository(pool)
    await repo.clear_threshold(GUILD_ID, "role_create")
    cog = _make_cog(pool)
    guild = FakeGuild()

    for _ in range(10):
        assert await cog.handle_action(guild, ACTOR_ID, "role_create") is False
    assert guild.bans == []


@pytest.mark.asyncio
async def test_owner_is_never_punished(pool):
    await _arm(pool, punishment="ban", ban_threshold=2)
    cog = _make_cog(pool)
    guild = FakeGuild(owner_id=OWNER_ID)

    for _ in range(5):
        assert await cog.handle_action(guild, OWNER_ID, "ban") is False
    assert guild.bans == []


@pytest.mark.asyncio
async def test_bot_is_never_punished(pool):
    await _arm(pool, punishment="ban", ban_threshold=2)
    cog = _make_cog(pool)
    guild = FakeGuild(bot_id=BOT_ID)

    for _ in range(5):
        assert await cog.handle_action(guild, BOT_ID, "ban") is False
    assert guild.bans == []


@pytest.mark.asyncio
async def test_whitelisted_actor_is_never_punished(pool):
    await _arm(pool, punishment="ban", ban_threshold=2)
    repo = AntiNukeRepository(pool)
    await repo.add_whitelist(GUILD_ID, TRUSTED_ID)
    cog = _make_cog(pool)
    guild = FakeGuild()

    for _ in range(5):
        assert await cog.handle_action(guild, TRUSTED_ID, "ban") is False
    assert guild.bans == []


@pytest.mark.asyncio
async def test_disabled_guild_takes_no_action(pool):
    await _arm(pool, punishment="ban", ban_threshold=2)
    repo = AntiNukeRepository(pool)
    await repo.set_enabled(GUILD_ID, False)
    cog = _make_cog(pool)
    guild = FakeGuild()

    for _ in range(5):
        assert await cog.handle_action(guild, ACTOR_ID, "ban") is False
    assert guild.bans == []


@pytest.mark.asyncio
async def test_strip_roles_removes_manageable_roles_only(pool):
    await _arm(pool, punishment="strip_roles", role_delete_threshold=2)
    cog = _make_cog(pool)
    guild = FakeGuild()

    everyone = FakeRole(0, default=True)
    managed = FakeRole(5, managed=True)
    normal = FakeRole(10)
    above_bot = FakeRole(200)  # higher than the bot's top role (100)
    member = FakeMember(
        ACTOR_ID, top_position=10, roles=[everyone, managed, normal, above_bot]
    )
    guild.add_member(member)

    for _ in range(2):
        await cog.handle_action(guild, ACTOR_ID, "role_delete")

    # Only the plain role below the bot was removed.
    assert member.removed == [normal]


@pytest.mark.asyncio
async def test_cooldown_prevents_restrike_within_window(pool):
    await _arm(pool, punishment="ban", ban_threshold=2)
    cog = _make_cog(pool)
    guild = FakeGuild()

    # First burst punishes once.
    for _ in range(2):
        await cog.handle_action(guild, ACTOR_ID, "ban")
    assert guild.bans == [ACTOR_ID]

    # A kick spree by the same actor immediately after is swallowed by the
    # per-actor cooldown, so no second punishment is enqueued.
    await _arm(pool, punishment="ban", ban_threshold=2, kick_threshold=2)
    cog.invalidate(GUILD_ID)
    for _ in range(3):
        await cog.handle_action(guild, ACTOR_ID, "kick")
    assert guild.bans == [ACTOR_ID]  # unchanged
