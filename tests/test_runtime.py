"""
Tests for the process supervisor, intents, cog discovery and configuration.

No network access and no real Discord client: the bot and dashboard are replaced
with stubs so the concurrency and shutdown logic can be exercised directly.
"""
import asyncio
import logging

import discord
import pytest

from fyrion import runtime
from fyrion.bot import build_intents, discover_extensions
from fyrion.config import Config, ConfigurationError
from fyrion.logging.logger import JsonFormatter, setup_logging


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------


def test_intents_are_explicit_and_minimal():
    intents = build_intents()

    assert intents.guilds is True
    assert intents.members is True
    assert intents.message_content is True
    assert intents.guild_messages is True
    assert intents.invites is True
    assert intents.moderation is True

    # Nothing Fyrion does not use should be requested.
    assert intents.presences is False
    assert intents.typing is False
    assert intents.dm_messages is False
    assert intents.voice_states is False


def test_intents_do_not_include_everything():
    assert build_intents().value != discord.Intents.all().value


# ---------------------------------------------------------------------------
# Cog discovery
# ---------------------------------------------------------------------------


def test_discover_extensions_finds_shipped_cogs():
    extensions = discover_extensions()

    assert extensions == sorted(extensions)
    assert "fyrion.cogs.moderation" in extensions
    assert "fyrion.cogs.automod" in extensions
    assert "fyrion.cogs.help" in extensions
    # Private helper modules must not be treated as extensions.
    assert all(
        not name.rsplit(".", 1)[-1].startswith("_") for name in extensions
    )


def test_discover_extensions_rejects_a_non_package():
    with pytest.raises((RuntimeError, ModuleNotFoundError)):
        discover_extensions("fyrion.config")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_validate_accepts_the_ci_environment():
    # conftest guarantees a usable token, so this must not raise.
    Config.validate()


def test_summary_redacts_secrets():
    summary = Config.summary()
    token = Config.DISCORD_TOKEN or ""
    assert token not in str(summary)
    assert summary["discord_token"].startswith("set (")


def test_dashboard_requires_credentials(monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "DISCORD_CLIENT_ID", None, raising=False)
    monkeypatch.setattr(Config, "DISCORD_CLIENT_SECRET", None, raising=False)
    monkeypatch.setattr(Config, "DASHBOARD_SECRET_KEY", None, raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        Config.validate()

    message = str(excinfo.value)
    assert "DISCORD_CLIENT_ID" in message
    assert "DASHBOARD_SECRET_KEY" in message


def test_dashboard_rejects_a_short_secret(monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "DISCORD_CLIENT_ID", "1234567890", raising=False)
    monkeypatch.setattr(Config, "DISCORD_CLIENT_SECRET", "secret-value", raising=False)
    monkeypatch.setattr(Config, "DASHBOARD_SECRET_KEY", "tooshort", raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        Config.validate()
    assert "at least 32 characters" in str(excinfo.value)


def test_dashboard_rejects_wildcard_cors(monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "DISCORD_CLIENT_ID", "1234567890", raising=False)
    monkeypatch.setattr(Config, "DISCORD_CLIENT_SECRET", "secret-value", raising=False)
    monkeypatch.setattr(Config, "DASHBOARD_SECRET_KEY", "x" * 48, raising=False)
    monkeypatch.setattr(
        Config, "DASHBOARD_ALLOWED_ORIGINS", ("*",), raising=False
    )

    with pytest.raises(ConfigurationError) as excinfo:
        Config.validate()
    assert "DASHBOARD_ALLOWED_ORIGINS" in str(excinfo.value)


def test_redirect_uri_is_absolute():
    assert Config.dashboard_redirect_uri().startswith(Config.DASHBOARD_BASE_URL)
    assert Config.dashboard_redirect_uri().endswith(
        Config.DASHBOARD_OAUTH_CALLBACK_PATH
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_json_formatter_emits_valid_json():
    import json

    record = logging.LogRecord(
        name="fyrion.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="guild %s ready",
        args=(42,),
        exc_info=None,
    )
    record.guild_id = 42

    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "fyrion.test"
    assert payload["message"] == "guild 42 ready"
    assert payload["extra"]["guild_id"] == 42


def test_setup_logging_is_idempotent():
    first = setup_logging()
    count = len(logging.getLogger().handlers)
    second = setup_logging()

    assert first is second
    assert len(logging.getLogger().handlers) == count


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class StubBot:
    """Minimal stand-in for the gateway client."""

    def __init__(self, *, run_time: float = 0.05, error: Exception | None = None):
        self.run_time = run_time
        self.error = error
        self.started = False
        self.closed = False
        self._stop = asyncio.Event()

    async def start(self, token):
        self.started = True
        if self.error is not None:
            raise self.error
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.run_time)
        except asyncio.TimeoutError:
            pass

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        self._stop.set()


class StubDashboard:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.served = False
        self.stopped = False
        self._stop = asyncio.Event()

    async def serve(self):
        self.served = True
        if self.error is not None:
            raise self.error
        await self._stop.wait()

    async def stop(self):
        self.stopped = True
        self._stop.set()


@pytest.mark.asyncio
async def test_supervise_runs_both_components_and_drains(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_TOKEN", "token", raising=False)

    bot = StubBot(run_time=0.05)
    dashboard = StubDashboard()

    exit_code = await runtime.supervise(bot, dashboard)

    assert exit_code == runtime.EXIT_OK
    assert bot.started is True
    assert bot.closed is True
    # The gateway exiting must also take the HTTP server down.
    assert dashboard.served is True
    assert dashboard.stopped is True


@pytest.mark.asyncio
async def test_supervise_reports_a_bot_failure(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_TOKEN", "token", raising=False)

    bot = StubBot(error=RuntimeError("gateway exploded"))
    dashboard = StubDashboard()

    exit_code = await runtime.supervise(bot, dashboard)

    assert exit_code == runtime.EXIT_FAILURE
    assert dashboard.stopped is True


@pytest.mark.asyncio
async def test_supervise_reports_a_dashboard_failure(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_TOKEN", "token", raising=False)

    bot = StubBot(run_time=5.0)
    dashboard = StubDashboard(error=OSError("address already in use"))

    exit_code = await runtime.supervise(bot, dashboard)

    assert exit_code == runtime.EXIT_FAILURE
    # A dead dashboard must not leave an orphaned gateway session behind.
    assert bot.closed is True


@pytest.mark.asyncio
async def test_supervise_runs_without_a_dashboard(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_TOKEN", "token", raising=False)

    bot = StubBot(run_time=0.02)
    assert await runtime.supervise(bot, None) == runtime.EXIT_OK
    assert bot.closed is True


@pytest.mark.asyncio
async def test_supervise_requires_a_token(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_TOKEN", None, raising=False)

    with pytest.raises(ConfigurationError):
        await runtime.supervise(StubBot(), None)


@pytest.mark.asyncio
async def test_signal_handler_installation_is_tolerant():
    # Must not raise on platforms without add_signal_handler support.
    runtime.install_signal_handlers(asyncio.get_running_loop(), asyncio.Event())


def test_build_dashboard_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_ENABLED", False, raising=False)
    assert runtime.build_dashboard(StubBot()) is None


def test_main_reports_a_configuration_error(monkeypatch, capsys):
    def _fail():
        raise ConfigurationError("  - DISCORD_TOKEN is missing.")

    monkeypatch.setattr(Config, "validate", staticmethod(_fail))

    assert runtime.main() == runtime.EXIT_CONFIG
    assert "CONFIGURATION ERROR" in capsys.readouterr().err
