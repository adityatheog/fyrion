"""
Configuration and environment management.

Values are read once, at import time, from the process environment (and from a
local ``.env`` file when present). Malformed values never raise during import:
they fall back to the documented default and record an issue that
:meth:`Config.validate` reports, so the process fails fast with one readable
message instead of a stack trace from an unrelated module.

Secrets are never logged. :meth:`Config.summary` returns a redacted view that is
safe to write to the log on startup.
"""
from __future__ import annotations

import os
from typing import Any, Final
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

try:  # pragma: no cover - depends on how the package was installed
    from importlib.metadata import PackageNotFoundError, version as _package_version

    try:
        _VERSION = _package_version("fyrion")
    except PackageNotFoundError:
        _VERSION = "0.0.0-dev"
except ImportError:  # pragma: no cover - Python < 3.8 only
    _VERSION = "0.0.0-dev"

_TRUE = frozenset({"1", "true", "yes", "y", "on", "enable", "enabled"})
_FALSE = frozenset({"0", "false", "no", "n", "off", "disable", "disabled"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LOG_FORMATS = frozenset({"text", "json"})
_ENVIRONMENTS = frozenset({"development", "testing", "production"})

# Values shipped in .env.example that must never reach production.
PLACEHOLDER_SECRETS: Final[frozenset[str]] = frozenset(
    {
        "your_bot_token_here",
        "your_token_here",
        "changeme",
        "change_me",
        "replace_me",
        "todo",
    }
)

# Collected while the class body below executes; surfaced by validate().
_ISSUES: list[str] = []


class ConfigurationError(RuntimeError):
    """Raised when the environment cannot produce a runnable configuration."""


def _raw(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _as_bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    _ISSUES.append(f"{name} must be a boolean (true/false); got {raw!r}.")
    return default


def _as_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _ISSUES.append(f"{name} must be an integer; got {raw!r}.")
        return default
    if minimum is not None and value < minimum:
        _ISSUES.append(f"{name} must be >= {minimum}; got {value}.")
        return default
    if maximum is not None and value > maximum:
        _ISSUES.append(f"{name} must be <= {maximum}; got {value}.")
        return default
    return value


def _as_optional_int(name: str, *, minimum: int | None = None) -> int | None:
    raw = _raw(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        _ISSUES.append(f"{name} must be an integer when set; got {raw!r}.")
        return None
    if minimum is not None and value < minimum:
        _ISSUES.append(f"{name} must be >= {minimum}; got {value}.")
        return None
    return value


def _as_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _ISSUES.append(f"{name} must be a number; got {raw!r}.")
        return default
    if minimum is not None and value < minimum:
        _ISSUES.append(f"{name} must be >= {minimum}; got {value}.")
        return default
    return value


def _as_tuple(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _raw(name)
    if raw is None:
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or default


def _default_trusted_hosts(base_url: str, host: str) -> tuple[str, ...]:
    """Derives a conservative Host allow-list from the configured base URL."""
    hosts = {"localhost", "127.0.0.1"}
    parsed = urlsplit(base_url)
    if parsed.hostname:
        hosts.add(parsed.hostname)
    if host and host not in {"0.0.0.0", "::"}:
        hosts.add(host)
    return tuple(sorted(hosts))


class Config:
    """Immutable view of the runtime configuration."""

    VERSION: Final[str] = _VERSION

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------
    ENVIRONMENT: Final[str] = (_raw("ENVIRONMENT", "development") or "development").lower()
    DISCORD_TOKEN: Final[str | None] = _raw("DISCORD_TOKEN")

    SHARD_COUNT: Final[int | None] = _as_optional_int("SHARD_COUNT", minimum=1)
    MESSAGE_CACHE_SIZE: Final[int] = _as_int(
        "MESSAGE_CACHE_SIZE", 1000, minimum=0, maximum=50_000
    )
    ACTIVITY_NAME: Final[str] = _raw("ACTIVITY_NAME", "/help") or "/help"
    SYNC_COMMANDS_ON_STARTUP: Final[bool] = _as_bool("SYNC_COMMANDS_ON_STARTUP", True)

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    DATABASE_URL: Final[str] = _raw("DATABASE_URL", "fyrion.db") or "fyrion.db"
    DATABASE_POOL_SIZE: Final[int] = _as_int(
        "DATABASE_POOL_SIZE", 5, minimum=1, maximum=64
    )
    DATABASE_BUSY_TIMEOUT_MS: Final[int] = _as_int(
        "DATABASE_BUSY_TIMEOUT_MS", 5000, minimum=0, maximum=120_000
    )
    DATABASE_MAINTENANCE_INTERVAL_SECONDS: Final[float] = _as_float(
        "DATABASE_MAINTENANCE_INTERVAL_SECONDS", 3600.0, minimum=60.0
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: Final[str] = (_raw("LOG_LEVEL", "INFO") or "INFO").upper()
    LOG_FORMAT: Final[str] = (_raw("LOG_FORMAT", "text") or "text").lower()
    LOG_TO_FILE: Final[bool] = _as_bool("LOG_TO_FILE", True)
    LOG_DIR: Final[str] = _raw("LOG_DIR", "logs") or "logs"
    LOG_FILE_NAME: Final[str] = _raw("LOG_FILE_NAME", "fyrion.log") or "fyrion.log"
    LOG_MAX_BYTES: Final[int] = _as_int(
        "LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=64 * 1024
    )
    LOG_BACKUP_COUNT: Final[int] = _as_int("LOG_BACKUP_COUNT", 5, minimum=0, maximum=100)
    DISCORD_LOG_LEVEL: Final[str] = (
        _raw("DISCORD_LOG_LEVEL", "INFO") or "INFO"
    ).upper()

    # ------------------------------------------------------------------
    # Web dashboard
    #
    # Disabled by default: an HTTP surface should be an explicit decision, and
    # enabling it requires OAuth credentials so the API is never reachable
    # without authentication (see validate()).
    # ------------------------------------------------------------------
    DASHBOARD_ENABLED: Final[bool] = _as_bool("DASHBOARD_ENABLED", False)
    DASHBOARD_HOST: Final[str] = _raw("DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1"
    DASHBOARD_PORT: Final[int] = _as_int(
        "DASHBOARD_PORT", 8080, minimum=1, maximum=65535
    )
    DASHBOARD_BASE_URL: Final[str] = (
        _raw("DASHBOARD_BASE_URL", f"http://127.0.0.1:{DASHBOARD_PORT}")
        or f"http://127.0.0.1:{DASHBOARD_PORT}"
    ).rstrip("/")
    DASHBOARD_OAUTH_CALLBACK_PATH: Final[str] = (
        _raw("DASHBOARD_OAUTH_CALLBACK_PATH", "/auth/callback")
        or "/auth/callback"
    )
    DASHBOARD_SECRET_KEY: Final[str | None] = _raw("DASHBOARD_SECRET_KEY")
    DISCORD_CLIENT_ID: Final[str | None] = _raw("DISCORD_CLIENT_ID")
    DISCORD_CLIENT_SECRET: Final[str | None] = _raw("DISCORD_CLIENT_SECRET")
    DASHBOARD_SESSION_TTL_SECONDS: Final[int] = _as_int(
        "DASHBOARD_SESSION_TTL_SECONDS", 12 * 3600, minimum=300, maximum=30 * 86400
    )
    DASHBOARD_ALLOWED_ORIGINS: Final[tuple[str, ...]] = _as_tuple(
        "DASHBOARD_ALLOWED_ORIGINS"
    )
    DASHBOARD_TRUSTED_HOSTS: Final[tuple[str, ...]] = _as_tuple(
        "DASHBOARD_TRUSTED_HOSTS",
        _default_trusted_hosts(DASHBOARD_BASE_URL, DASHBOARD_HOST),
    )
    DASHBOARD_COOKIE_SECURE: Final[bool] = _as_bool(
        "DASHBOARD_COOKIE_SECURE", DASHBOARD_BASE_URL.startswith("https://")
    )
    DASHBOARD_COOKIE_DOMAIN: Final[str | None] = _raw("DASHBOARD_COOKIE_DOMAIN")
    DASHBOARD_ACCESS_LOG: Final[bool] = _as_bool("DASHBOARD_ACCESS_LOG", False)
    DASHBOARD_TRUST_PROXY: Final[bool] = _as_bool("DASHBOARD_TRUST_PROXY", False)
    DASHBOARD_FORWARDED_ALLOW_IPS: Final[str | None] = _raw(
        "DASHBOARD_FORWARDED_ALLOW_IPS"
    )
    DASHBOARD_RATE_LIMIT: Final[int] = _as_int(
        "DASHBOARD_RATE_LIMIT", 60, minimum=1, maximum=10_000
    )
    DASHBOARD_RATE_LIMIT_WINDOW: Final[int] = _as_int(
        "DASHBOARD_RATE_LIMIT_WINDOW", 60, minimum=1, maximum=3600
    )
    DASHBOARD_AUTH_RATE_LIMIT: Final[int] = _as_int(
        "DASHBOARD_AUTH_RATE_LIMIT", 10, minimum=1, maximum=1000
    )
    DASHBOARD_MEMBER_LOOKUP_LIMIT: Final[int] = _as_int(
        "DASHBOARD_MEMBER_LOOKUP_LIMIT", 50, minimum=1, maximum=1000
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def is_production(cls) -> bool:
        return cls.ENVIRONMENT == "production"

    @classmethod
    def dashboard_redirect_uri(cls) -> str:
        """The exact redirect URI that must be registered with Discord."""
        path = cls.DASHBOARD_OAUTH_CALLBACK_PATH
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{cls.DASHBOARD_BASE_URL}{path}"

    @classmethod
    def validate(cls) -> None:
        """Raises :class:`ConfigurationError` when the process cannot run."""
        issues: list[str] = list(_ISSUES)

        token = cls.DISCORD_TOKEN
        if not token:
            issues.append("DISCORD_TOKEN is missing. Set it in the environment or .env.")
        else:
            if token.lower() in PLACEHOLDER_SECRETS:
                issues.append(
                    "DISCORD_TOKEN is still the placeholder value from .env.example."
                )
            if any(char.isspace() for char in token):
                issues.append("DISCORD_TOKEN contains whitespace; check for a typo.")

        if cls.LOG_LEVEL not in _LOG_LEVELS:
            issues.append(
                f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}; got {cls.LOG_LEVEL!r}."
            )
        if cls.DISCORD_LOG_LEVEL not in _LOG_LEVELS:
            issues.append(
                "DISCORD_LOG_LEVEL must be one of "
                f"{sorted(_LOG_LEVELS)}; got {cls.DISCORD_LOG_LEVEL!r}."
            )
        if cls.LOG_FORMAT not in _LOG_FORMATS:
            issues.append(
                f"LOG_FORMAT must be 'text' or 'json'; got {cls.LOG_FORMAT!r}."
            )
        if cls.ENVIRONMENT not in _ENVIRONMENTS:
            issues.append(
                f"ENVIRONMENT must be one of {sorted(_ENVIRONMENTS)}; "
                f"got {cls.ENVIRONMENT!r}."
            )

        if cls.DASHBOARD_ENABLED:
            issues.extend(cls._validate_dashboard())

        if issues:
            raise ConfigurationError(
                "\n".join(f"  - {issue}" for issue in issues)
            )

    @classmethod
    def _validate_dashboard(cls) -> list[str]:
        """Dashboard-specific rules.

        The credential requirements are deliberately hard failures: without
        them the HTTP API would have no way to authenticate anyone, and a
        network-reachable unauthenticated admin API is not an acceptable
        default.
        """
        issues: list[str] = []

        if not cls.DISCORD_CLIENT_ID or not cls.DISCORD_CLIENT_SECRET:
            issues.append(
                "DASHBOARD_ENABLED requires DISCORD_CLIENT_ID and "
                "DISCORD_CLIENT_SECRET; the dashboard refuses to run without "
                "an authentication provider."
            )
        if cls.DISCORD_CLIENT_SECRET and (
            cls.DISCORD_CLIENT_SECRET.lower() in PLACEHOLDER_SECRETS
        ):
            issues.append("DISCORD_CLIENT_SECRET is still a placeholder value.")

        secret = cls.DASHBOARD_SECRET_KEY
        if not secret:
            issues.append(
                "DASHBOARD_ENABLED requires DASHBOARD_SECRET_KEY (at least 32 "
                "characters) to sign session and OAuth state values. Generate "
                'one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        else:
            if len(secret) < 32:
                issues.append("DASHBOARD_SECRET_KEY must be at least 32 characters.")
            if secret.lower() in PLACEHOLDER_SECRETS:
                issues.append("DASHBOARD_SECRET_KEY is still a placeholder value.")

        parsed = urlsplit(cls.DASHBOARD_BASE_URL)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            issues.append(
                "DASHBOARD_BASE_URL must be an absolute http(s) URL, for example "
                "https://dashboard.example.com."
            )
        elif cls.is_production() and parsed.scheme != "https":
            issues.append(
                "DASHBOARD_BASE_URL must use https in production so session "
                "cookies can be marked Secure."
            )

        if cls.is_production() and not cls.DASHBOARD_COOKIE_SECURE:
            issues.append(
                "DASHBOARD_COOKIE_SECURE cannot be disabled in production."
            )

        if "*" in cls.DASHBOARD_ALLOWED_ORIGINS:
            issues.append(
                "DASHBOARD_ALLOWED_ORIGINS cannot be '*': the API uses cookie "
                "credentials, so origins must be listed explicitly."
            )

        if cls.is_production() and "*" in cls.DASHBOARD_TRUSTED_HOSTS:
            issues.append(
                "DASHBOARD_TRUSTED_HOSTS cannot be '*' in production; list the "
                "hostnames the dashboard is served under."
            )

        return issues

    @classmethod
    def summary(cls) -> dict[str, Any]:
        """Returns a redacted snapshot suitable for logging."""

        def redact(value: str | None) -> str:
            if not value:
                return "unset"
            return f"set ({len(value)} chars)"

        return {
            "version": cls.VERSION,
            "environment": cls.ENVIRONMENT,
            "discord_token": redact(cls.DISCORD_TOKEN),
            "shard_count": cls.SHARD_COUNT or "auto",
            "sync_commands_on_startup": cls.SYNC_COMMANDS_ON_STARTUP,
            "database_url": cls.DATABASE_URL,
            "database_pool_size": cls.DATABASE_POOL_SIZE,
            "log_level": cls.LOG_LEVEL,
            "log_format": cls.LOG_FORMAT,
            "log_to_file": cls.LOG_TO_FILE,
            "dashboard_enabled": cls.DASHBOARD_ENABLED,
            "dashboard_bind": f"{cls.DASHBOARD_HOST}:{cls.DASHBOARD_PORT}",
            "dashboard_base_url": cls.DASHBOARD_BASE_URL,
            "dashboard_cookie_secure": cls.DASHBOARD_COOKIE_SECURE,
            "dashboard_secret_key": redact(cls.DASHBOARD_SECRET_KEY),
            "discord_client_id": cls.DISCORD_CLIENT_ID or "unset",
            "discord_client_secret": redact(cls.DISCORD_CLIENT_SECRET),
        }


__all__ = ["Config", "ConfigurationError", "PLACEHOLDER_SECRETS"]
