"""
Fyrion dashboard application.

What this module serves
-----------------------
* three server-rendered Jinja2 pages: ``/`` (landing), ``/dashboard`` (server
  picker) and ``/manage/{guild_id}`` (settings editor);
* the Discord OAuth2 authorization-code handshake: ``/login``, ``/callback``
  and ``/logout``;
* a JSON API: ``/api/guilds``, ``/api/guilds/{guild_id}`` (GET and PATCH) and
  ``/api/bot/stats``.

Security model
--------------
* **Sessions are cookies signed by Starlette's ``SessionMiddleware``.** The
  cookie is HttpOnly, ``SameSite=Lax`` (so it survives the top-level redirect
  back from Discord) and ``Secure`` whenever ``DASHBOARD_BASE_URL`` is https.
* **No Discord token is ever stored.** The authorization code is exchanged once
  to read the user's profile and guild list, the access token is revoked
  immediately afterwards, and only a snapshot of that data is kept in the
  session. A leaked session cookie therefore cannot be replayed against
  Discord's API.
* **Authorization is decided server side.** Managing a guild requires
  ``Manage Server`` (or ``Administrator``) in the snapshot Discord returned
  *and*, when the gateway client is available, in Fyrion's live view of the
  member. Nothing the browser sends can influence that decision.
* **CSRF.** The OAuth ``state`` is a random value held in the session and
  compared with ``hmac.compare_digest``. Mutating API calls require the
  session's CSRF token in an ``X-CSRF-Token`` header, which a cross-site form
  post cannot set.
* **Strict input validation.** ``PATCH`` bodies are validated by
  :class:`fyrion.web.models.GuildSettingsUpdate` (``extra="forbid"``), every
  referenced channel and role must exist in that guild, and the database layer
  validates the column names again before building SQL. Values are always bound
  as parameters.
* **No internals in responses.** Unhandled failures return a short reference id;
  the traceback stays in the log. Filesystem paths are never exposed.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Deque, Mapping, Sequence
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fyrion.config import Config
from fyrion.web.models import CHANNEL_FIELDS, ROLE_FIELDS, GuildSettingsUpdate

log = logging.getLogger("dashboard.app")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Discord OAuth2
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
REVOKE_URL = f"{DISCORD_API}/oauth2/token/revoke"
CURRENT_USER_URL = f"{DISCORD_API}/users/@me"
CURRENT_GUILDS_URL = f"{DISCORD_API}/users/@me/guilds"
CDN_BASE = "https://cdn.discordapp.com"

# ``identify`` names the user, ``guilds`` lists the servers they are in. Nothing
# else is requested: everything Fyrion actually changes goes through the bot's
# own permissions, not the user's token.
OAUTH_SCOPES = "identify guilds"

DEFAULT_CALLBACK_PATH = "/callback"

PERMISSION_ADMINISTRATOR = 1 << 3
PERMISSION_MANAGE_GUILD = 1 << 5

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Discord's profile and guild-list payloads are small; anything larger is a bug.
MAX_RESPONSE_BYTES = 512 * 1024

# ---------------------------------------------------------------------------
# Session keys
# ---------------------------------------------------------------------------

SESSION_USER = "user"
SESSION_GUILDS = "guilds"
SESSION_ISSUED_AT = "issued_at"
SESSION_CSRF = "csrf"
SESSION_STATE = "oauth_state"
SESSION_NEXT = "post_login_path"

# ---------------------------------------------------------------------------
# Response hardening
# ---------------------------------------------------------------------------

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' https://cdn.discordapp.com data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}

# Keys from ``DatabasePool.stats()`` that are safe to expose. ``db_url`` is a
# filesystem path and is deliberately omitted.
SAFE_DB_STAT_KEYS = frozenset(
    {
        "connected",
        "pool_size",
        "available_connections",
        "in_memory",
        "schema_version",
        "journal_mode",
        "page_count",
        "page_size",
        "freelist_count",
        "size_bytes",
    }
)


class OAuthError(RuntimeError):
    """Raised when Discord refuses or fails an OAuth exchange.

    The message is user facing, so it never contains a URL, a payload excerpt or
    any other internal detail.
    """


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class SlidingWindowLimiter:
    """Small in-process sliding-window limiter.

    Enough for a single-process self-hosted dashboard: it blunts credential
    stuffing and accidental request storms. Deployments behind a shared edge
    should also rate limit there.
    """

    def __init__(self, limit: int, window: int, *, max_keys: int = 10_000) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window))
        self.max_keys = max_keys
        self._hits: dict[str, Deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.get(key)
        if bucket is None:
            if len(self._hits) >= self.max_keys:
                # Unbounded growth is itself a denial-of-service vector.
                self._evict(now)
            bucket = deque()
            self._hits[key] = bucket

        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self.limit:
            return False

        bucket.append(now)
        return True

    def _evict(self, now: float) -> None:
        cutoff = now - self.window
        stale = [
            key
            for key, bucket in self._hits.items()
            if not bucket or bucket[-1] <= cutoff
        ]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= self.max_keys:
            self._hits.clear()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def callback_path() -> str:
    """Returns the path the OAuth callback is served on."""
    raw = (Config.DASHBOARD_OAUTH_CALLBACK_PATH or DEFAULT_CALLBACK_PATH).strip()
    if not raw:
        raw = DEFAULT_CALLBACK_PATH
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw


def redirect_uri() -> str:
    """The exact redirect URI that must be registered with Discord."""
    return f"{Config.DASHBOARD_BASE_URL}{callback_path()}"


def oauth_configured() -> bool:
    return bool(Config.DISCORD_CLIENT_ID and Config.DISCORD_CLIENT_SECRET)


def session_secret() -> str:
    """Returns the session signing key.

    Without a configured key an ephemeral one is generated so development stays
    usable, and the warning makes it obvious that sessions will not survive a
    restart. ``Config.validate()`` refuses to enable the dashboard without a
    key, so production never reaches this branch.
    """
    configured = Config.DASHBOARD_SECRET_KEY
    if configured:
        return configured

    log.warning(
        "DASHBOARD_SECRET_KEY is not set; using an ephemeral signing key. Every "
        "session will be invalidated when this process restarts."
    )
    return secrets.token_urlsafe(48)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def can_manage(permissions: int, owner: bool) -> bool:
    """Returns True when a permission bitfield grants server management."""
    if owner:
        return True
    return bool(
        permissions & PERMISSION_ADMINISTRATOR or permissions & PERMISSION_MANAGE_GUILD
    )


def safe_next_path(raw: Any) -> str | None:
    """Validates a post-login redirect target.

    Only same-origin absolute paths are accepted, so ``?next=`` cannot be used
    as an open redirect.
    """
    if not raw or not isinstance(raw, str):
        return None
    if len(raw) > 300:
        return None
    if not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or "\n" in raw or "\r" in raw:
        return None
    return raw


def guild_icon_url(guild_id: Any, icon_hash: Any) -> str | None:
    if not icon_hash:
        return None
    suffix = "gif" if str(icon_hash).startswith("a_") else "png"
    return f"{CDN_BASE}/icons/{guild_id}/{icon_hash}.{suffix}?size=128"


def user_avatar_url(user_id: Any, avatar_hash: Any) -> str | None:
    if not avatar_hash:
        return None
    suffix = "gif" if str(avatar_hash).startswith("a_") else "png"
    return f"{CDN_BASE}/avatars/{user_id}/{avatar_hash}.{suffix}?size=128"


def json_attribute(payload: Any) -> str:
    """Serializes data for embedding in a ``data-`` attribute.

    Jinja's autoescaping handles the HTML quoting; the angle-bracket escaping
    here is belt and braces so the value can never terminate the element.
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def serialize_ids(row: Mapping[str, Any]) -> dict[str, Any]:
    """Renders snowflakes as strings so JavaScript cannot lose precision."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, int) and not isinstance(value, bool) and key.endswith("_id"):
            result[key] = str(value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


def get_bot(request: Request) -> Any | None:
    return getattr(request.app.state, "bot", None)


def get_db(request: Request) -> Any:
    db = getattr(request.app.state, "db", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The database is not available yet. Please try again shortly.",
        )
    return db


def get_http(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http", None)
    if client is None:  # pragma: no cover - lifespan always sets this
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The HTTP client is unavailable."
        )
    return client


def client_ip(request: Request) -> str:
    if Config.DASHBOARD_TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # The left-most entry is the original client.
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request, scope: str) -> None:
    limiter: SlidingWindowLimiter | None = getattr(
        request.app.state, f"limiter_{scope}", None
    )
    if limiter is None:
        return
    if not limiter.allow(client_ip(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Please slow down.",
            headers={"Retry-After": str(limiter.window)},
        )


def session_user(request: Request) -> dict[str, Any] | None:
    """Returns the signed-in user, or ``None``."""
    user = request.session.get(SESSION_USER)
    if not isinstance(user, dict) or not user.get("id"):
        return None

    issued_at = request.session.get(SESSION_ISSUED_AT)
    if isinstance(issued_at, (int, float)):
        if time.time() - float(issued_at) > Config.DASHBOARD_SESSION_TTL_SECONDS:
            request.session.clear()
            return None
    return user


def require_user(request: Request) -> dict[str, Any]:
    user = session_user(request)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Authentication is required."
        )
    return user


def require_csrf(request: Request) -> None:
    """Validates the double-submit CSRF token on a mutating request."""
    expected = request.session.get(SESSION_CSRF)
    supplied = request.headers.get("x-csrf-token", "")
    if (
        not isinstance(expected, str)
        or not supplied
        or not hmac.compare_digest(expected, supplied)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The request could not be verified. Reload the page and try again.",
        )


def session_guilds(request: Request) -> list[dict[str, Any]]:
    entries = request.session.get(SESSION_GUILDS)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def manageable_entry(request: Request, guild_id: int) -> dict[str, Any] | None:
    """Returns the session snapshot for a guild the user may manage."""
    for entry in session_guilds(request):
        if as_int(entry.get("id"), -1) != guild_id:
            continue
        if can_manage(as_int(entry.get("permissions")), bool(entry.get("owner"))):
            return entry
        return None
    return None


# ---------------------------------------------------------------------------
# Discord OAuth exchange
# ---------------------------------------------------------------------------


async def _post_form(
    client: httpx.AsyncClient, url: str, payload: Mapping[str, str]
) -> httpx.Response:
    return await client.post(
        url,
        data=dict(payload),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


async def exchange_code(
    client: httpx.AsyncClient, code: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Exchanges an authorization code for the user's profile and guild list.

    The access token is used once and then revoked, so Fyrion never holds a live
    Discord credential for a dashboard user.
    """
    payload = {
        "client_id": Config.DISCORD_CLIENT_ID or "",
        "client_secret": Config.DISCORD_CLIENT_SECRET or "",
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
    }

    try:
        response = await _post_form(client, TOKEN_URL, payload)
    except httpx.HTTPError as exc:
        log.warning("OAuth token exchange transport error: %s", exc)
        raise OAuthError("Could not reach Discord to complete the sign-in.") from None

    if response.status_code != 200:
        log.warning(
            "OAuth token exchange failed (HTTP %s): %s",
            response.status_code,
            response.text[:500],
        )
        raise OAuthError("Discord rejected the authorization code.")

    try:
        token_data = response.json()
    except ValueError:
        raise OAuthError("Discord returned an unreadable token response.") from None

    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("Discord returned an unusable token response.")

    try:
        profile = await _fetch_json(client, CURRENT_USER_URL, access_token)
        guilds = await _fetch_json(client, CURRENT_GUILDS_URL, access_token)
    finally:
        await _revoke_token(client, access_token)

    if not isinstance(profile, dict):
        raise OAuthError("Discord returned an unusable profile.")

    user_id = profile.get("id")
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise OAuthError("Discord returned an unusable profile.")

    if not isinstance(guilds, list):
        guilds = []

    return profile, [entry for entry in guilds if isinstance(entry, dict)]


async def _fetch_json(
    client: httpx.AsyncClient, url: str, access_token: str
) -> Any:
    try:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
    except httpx.HTTPError as exc:
        log.warning("OAuth read of %s failed: %s", url, exc)
        raise OAuthError("Could not reach Discord to complete the sign-in.") from None

    if response.status_code == 429:
        raise OAuthError(
            "Discord is rate limiting this instance. Please try again in a minute."
        )
    if response.status_code != 200:
        log.warning("OAuth read of %s returned HTTP %s.", url, response.status_code)
        raise OAuthError("Discord did not return your account details.")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise OAuthError("Discord returned an unexpectedly large response.")

    try:
        return response.json()
    except ValueError:
        raise OAuthError("Discord returned an unreadable response.") from None


async def _revoke_token(client: httpx.AsyncClient, access_token: str) -> None:
    payload = {
        "client_id": Config.DISCORD_CLIENT_ID or "",
        "client_secret": Config.DISCORD_CLIENT_SECRET or "",
        "token": access_token,
        "token_type_hint": "access_token",
    }
    try:
        response = await _post_form(client, REVOKE_URL, payload)
        if response.status_code >= 400:
            log.debug("Token revocation returned HTTP %s.", response.status_code)
    except httpx.HTTPError as exc:
        # Revocation is hygiene, not a hard requirement for a working sign-in.
        log.debug("Token revocation failed: %s", exc)


# ---------------------------------------------------------------------------
# Guild resolution
# ---------------------------------------------------------------------------


async def resolve_member(guild: Any, user_id: int) -> tuple[Any | None, bool]:
    """Returns ``(member, lookup_failed)`` for a guild member.

    ``chunk_guilds_at_startup`` is disabled, so the member cache is usually cold
    and a REST fetch is normally required. A transport failure is reported
    separately from "not a member", because the two must be treated differently:
    the first falls back to the OAuth snapshot, the second is a hard refusal.
    """
    import discord

    member = guild.get_member(user_id)
    if member is not None:
        return member, False

    try:
        return await guild.fetch_member(user_id), False
    except discord.NotFound:
        return None, False
    except discord.Forbidden:
        return None, True
    except discord.HTTPException as exc:
        log.warning(
            "Could not fetch member %s in guild %s: %s", user_id, guild.id, exc
        )
        return None, True


class GuildAccess:
    """An authorized (guild, user) pair."""

    __slots__ = ("guild_id", "guild", "member", "entry", "live")

    def __init__(
        self,
        guild_id: int,
        guild: Any | None,
        member: Any | None,
        entry: Mapping[str, Any],
    ) -> None:
        self.guild_id = guild_id
        self.guild = guild
        self.member = member
        self.entry = dict(entry)
        # True when the gateway client can see this guild, which is what makes
        # the channel and role pickers available.
        self.live = guild is not None

    @property
    def name(self) -> str:
        if self.guild is not None:
            return str(self.guild.name)
        return str(self.entry.get("name") or "Unknown server")

    @property
    def icon_url(self) -> str | None:
        if self.guild is not None and getattr(self.guild, "icon", None) is not None:
            return str(self.guild.icon.url)
        return guild_icon_url(self.guild_id, self.entry.get("icon"))


async def authorize_guild(request: Request, guild_id: int) -> GuildAccess:
    """Authorizes the signed-in user for one guild.

    A guild the user cannot manage is reported as ``404`` rather than ``403`` so
    the API does not confirm whether an arbitrary snowflake exists.
    """
    require_user(request)

    entry = manageable_entry(request, guild_id)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That server is not available to you. It must be a server you can "
            "manage; sign in again if you have just been given the permission.",
        )

    bot = get_bot(request)
    guild = bot.get_guild(guild_id) if bot is not None else None
    member = None

    if guild is not None:
        user_id = as_int(request.session[SESSION_USER]["id"])
        member, lookup_failed = await resolve_member(guild, user_id)

        if member is None and not lookup_failed:
            # Fyrion can see the guild and Discord says the user is not in it.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "You are not a member of that server."
            )

        if member is not None:
            permissions = member.guild_permissions
            if not (permissions.administrator or permissions.manage_guild):
                # Defence in depth: the live permission wins over the snapshot,
                # which may be minutes old.
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "The Manage Server permission is required for that server.",
                )

    return GuildAccess(guild_id, guild, member, entry)


def describe_guild(entry: Mapping[str, Any], bot: Any | None) -> dict[str, Any]:
    """Renders one guild for the picker and the API."""
    guild_id = as_int(entry.get("id"))
    guild = bot.get_guild(guild_id) if bot is not None else None
    permissions = as_int(entry.get("permissions"))

    icon_url = None
    if guild is not None and getattr(guild, "icon", None) is not None:
        icon_url = str(guild.icon.url)
    else:
        icon_url = guild_icon_url(guild_id, entry.get("icon"))

    return {
        "id": str(guild_id),
        "name": str(guild.name) if guild is not None else str(entry.get("name") or "Unknown server"),
        "icon_url": icon_url,
        "owner": bool(entry.get("owner")),
        "administrator": bool(permissions & PERMISSION_ADMINISTRATOR),
        "bot_present": guild is not None,
        "member_count": getattr(guild, "member_count", None) if guild is not None else None,
        "shard_id": getattr(guild, "shard_id", None) if guild is not None else None,
    }


def manageable_guilds(request: Request) -> list[dict[str, Any]]:
    bot = get_bot(request)
    guilds = [
        describe_guild(entry, bot)
        for entry in session_guilds(request)
        if can_manage(as_int(entry.get("permissions")), bool(entry.get("owner")))
    ]
    # Servers Fyrion is already in come first, then alphabetically.
    guilds.sort(key=lambda item: (not item["bot_present"], item["name"].lower()))
    return guilds


def describe_channels(guild: Any | None) -> dict[str, list[dict[str, Any]]]:
    """Returns the guild's channels, grouped by kind."""
    if guild is None:
        return {"text": [], "voice": [], "categories": []}

    import discord

    me = guild.me

    def describe(channel: Any) -> dict[str, Any]:
        writable: bool | None = None
        if me is not None and isinstance(channel, discord.TextChannel):
            permissions = channel.permissions_for(me)
            writable = bool(permissions.send_messages and permissions.embed_links)
        return {
            "id": str(channel.id),
            "name": str(channel.name),
            "type": str(channel.type),
            "position": int(getattr(channel, "position", 0)),
            "category": str(channel.category.name) if getattr(channel, "category", None) else None,
            "writable": writable,
        }

    return {
        "text": [describe(channel) for channel in guild.text_channels],
        "voice": [describe(channel) for channel in guild.voice_channels],
        "categories": [describe(channel) for channel in guild.categories],
    }


def describe_roles(guild: Any | None) -> list[dict[str, Any]]:
    """Returns the guild's roles, highest first."""
    if guild is None:
        return []

    me = guild.me
    roles: list[dict[str, Any]] = []
    for role in sorted(guild.roles, key=lambda item: item.position, reverse=True):
        assignable = bool(
            me is not None
            and not role.managed
            and not role.is_default()
            and me.top_role > role
        )
        roles.append(
            {
                "id": str(role.id),
                "name": str(role.name),
                "position": int(role.position),
                "color": str(role.color),
                "managed": bool(role.managed),
                "is_default": bool(role.is_default()),
                "administrator": bool(role.permissions.administrator),
                # Tells the UI which roles are usable for autorole and mute.
                "assignable": assignable,
            }
        )
    return roles


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

pages = APIRouter(include_in_schema=False)
auth = APIRouter(tags=["auth"])
api = APIRouter(prefix="/api", tags=["api"])


def templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request": request,
        "user": session_user(request),
        "version": Config.VERSION,
        "environment": Config.ENVIRONMENT,
        "bot_online": bool(get_bot(request) is not None),
        "oauth_ready": oauth_configured(),
    }
    context.update(extra)
    return context


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    query = urlencode({"next": target})
    return RedirectResponse(
        f"/login?{query}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@pages.get("/", name="index")
async def index(request: Request) -> Response:
    """Landing page. Public: it contains nothing that is not already public."""
    if session_user(request) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return templates(request).TemplateResponse(
        "index.html", page_context(request, title="Fyrion")
    )


@pages.get("/dashboard", name="dashboard_page")
async def dashboard_page(request: Request) -> Response:
    if session_user(request) is None:
        return login_redirect(request)

    guilds = manageable_guilds(request)
    return templates(request).TemplateResponse(
        "dashboard.html",
        page_context(
            request,
            title="Your servers",
            guilds=guilds,
            invite_url=invite_url(),
            managed_count=sum(1 for guild in guilds if guild["bot_present"]),
        ),
    )


@pages.get("/manage/{guild_id}", name="manage_page")
async def manage_page(guild_id: int, request: Request) -> Response:
    if session_user(request) is None:
        return login_redirect(request)

    access = await authorize_guild(request, guild_id)
    db = get_db(request)
    settings = serialize_ids(await db.get_guild_settings(access.guild_id))

    channels = describe_channels(access.guild)
    roles = describe_roles(access.guild)

    bootstrap = {
        "guild_id": str(access.guild_id),
        "csrf": request.session.get(SESSION_CSRF, ""),
        "settings": settings,
        "live": access.live,
    }

    return templates(request).TemplateResponse(
        "manage.html",
        page_context(
            request,
            title=f"Manage {access.name}",
            guild={
                "id": str(access.guild_id),
                "name": access.name,
                "icon_url": access.icon_url,
                "live": access.live,
                "member_count": getattr(access.guild, "member_count", None),
            },
            settings=settings,
            text_channels=channels["text"],
            categories=channels["categories"],
            roles=roles,
            bootstrap_json=json_attribute(bootstrap),
        ),
    )


def invite_url() -> str | None:
    """Returns the bot invite URL when the client id is configured."""
    client_id = Config.DISCORD_CLIENT_ID
    if not client_id:
        return None
    query = urlencode(
        {
            "client_id": client_id,
            "scope": "bot applications.commands",
            # Manage Server, Manage Roles, Manage Channels, Kick, Ban, Manage
            # Messages, Moderate Members, Embed Links, Attach Files, Read
            # History, Add Reactions.
            "permissions": "1101659730518",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


@pages.get("/health", name="health")
async def health(request: Request) -> JSONResponse:
    """Liveness probe. Intentionally minimal: no counts, no configuration."""
    bot = get_bot(request)
    db = getattr(request.app.state, "db", None)

    bot_ready = bool(bot is not None and bot.is_ready())
    db_ready = bool(db is not None and getattr(db, "is_connected", False))
    healthy = db_ready and (bot is None or bot_ready)

    return JSONResponse(
        {
            "status": "ok" if healthy else "starting",
            "database": db_ready,
            "gateway": bot_ready if bot is not None else None,
        },
        status_code=(
            status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
    )


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


@auth.get("/login", name="login", include_in_schema=False)
async def login(request: Request, next: str | None = None) -> Response:
    """Starts the Discord OAuth handshake."""
    enforce_rate_limit(request, "auth")

    if not oauth_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Discord sign-in is not configured on this instance. Set "
            "DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET.",
        )

    if session_user(request) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    state = secrets.token_urlsafe(32)
    request.session[SESSION_STATE] = state

    target = safe_next_path(next)
    if target:
        request.session[SESSION_NEXT] = target
    else:
        request.session.pop(SESSION_NEXT, None)

    query = urlencode(
        {
            "client_id": Config.DISCORD_CLIENT_ID or "",
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "state": state,
            "prompt": "none",
        }
    )
    return RedirectResponse(
        f"{AUTHORIZE_URL}?{query}", status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )


async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Completes the handshake and populates the session."""
    enforce_rate_limit(request, "auth")

    expected_state = request.session.pop(SESSION_STATE, None)

    if error:
        log.info("OAuth callback returned an error: %s", str(error)[:100])
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The Discord sign-in was declined."
        )
    if not code or not state:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The sign-in response was incomplete."
        )
    if not isinstance(expected_state, str) or not hmac.compare_digest(
        expected_state, state
    ):
        # Either the session expired between /login and /callback, or this is a
        # forged callback. Both are refused.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in attempt expired or could not be verified. Please try "
            "again.",
        )

    try:
        profile, guilds = await exchange_code(get_http(request), code)
    except OAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    user_id = str(profile["id"])
    username = str(profile.get("global_name") or profile.get("username") or "Unknown")

    # Only the fields the dashboard actually renders are kept; the raw payload is
    # discarded so the cookie stays small.
    snapshot: list[dict[str, Any]] = []
    for entry in guilds:
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.isdigit():
            continue
        permissions = as_int(entry.get("permissions"))
        if not can_manage(permissions, bool(entry.get("owner"))):
            # Servers the user cannot manage are dropped entirely: the dashboard
            # has nothing to show for them.
            continue
        snapshot.append(
            {
                "id": entry_id,
                "name": str(entry.get("name") or "Unknown server")[:100],
                "icon": entry.get("icon"),
                "owner": bool(entry.get("owner")),
                "permissions": permissions,
            }
        )

    request.session.clear()
    request.session[SESSION_USER] = {
        "id": user_id,
        "username": username[:100],
        "avatar_url": user_avatar_url(user_id, profile.get("avatar")),
    }
    request.session[SESSION_GUILDS] = snapshot
    request.session[SESSION_ISSUED_AT] = int(time.time())
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)

    log.info(
        "Dashboard sign-in for user %s (%d manageable server(s)).",
        user_id,
        len(snapshot),
    )

    destination = safe_next_path(request.session.pop(SESSION_NEXT, None)) or "/dashboard"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@auth.api_route(
    "/logout", methods=["GET", "POST"], name="logout", include_in_schema=False
)
async def logout(request: Request) -> Response:
    """Clears the session and returns to the landing page."""
    user = session_user(request)
    request.session.clear()
    if user is not None:
        log.info("Dashboard sign-out for user %s.", user.get("id"))
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@api.get("/me")
async def api_me(request: Request) -> dict[str, Any]:
    """Returns the caller's identity and session metadata."""
    enforce_rate_limit(request, "api")
    user = require_user(request)

    issued_at = request.session.get(SESSION_ISSUED_AT)
    expires_at = (
        int(issued_at) + Config.DASHBOARD_SESSION_TTL_SECONDS
        if isinstance(issued_at, (int, float))
        else None
    )

    return {
        "user": user,
        "session": {
            "issued_at": issued_at,
            "expires_at": expires_at,
            "scopes": OAUTH_SCOPES,
        },
        "csrf": request.session.get(SESSION_CSRF, ""),
    }


@api.get("/guilds")
async def api_guilds(request: Request) -> dict[str, Any]:
    """Lists the servers the caller may configure."""
    enforce_rate_limit(request, "api")
    require_user(request)

    guilds = manageable_guilds(request)
    return {
        "guilds": guilds,
        "total": len(guilds),
        "with_bot": sum(1 for guild in guilds if guild["bot_present"]),
        "invite_url": invite_url(),
    }


@api.get("/guilds/{guild_id}")
async def api_guild(guild_id: int, request: Request) -> dict[str, Any]:
    """Returns one server's settings, channels and roles."""
    enforce_rate_limit(request, "api")
    access = await authorize_guild(request, guild_id)

    db = get_db(request)
    settings = serialize_ids(await db.get_guild_settings(access.guild_id))
    channels = describe_channels(access.guild)

    guild = access.guild
    counts = (
        {
            "roles": len(guild.roles),
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "categories": len(guild.categories),
        }
        if guild is not None
        else {}
    )

    return {
        "guild": {
            "id": str(access.guild_id),
            "name": access.name,
            "icon_url": access.icon_url,
            "owner": bool(access.entry.get("owner")),
            "bot_present": access.live,
            "member_count": getattr(guild, "member_count", None) if guild else None,
            "created_at": guild.created_at.isoformat() if guild is not None else None,
        },
        "counts": counts,
        "settings": settings,
        "channels": channels,
        "roles": describe_roles(guild),
    }


@api.patch("/guilds/{guild_id}")
async def api_update_guild(
    guild_id: int, payload: GuildSettingsUpdate, request: Request
) -> dict[str, Any]:
    """Applies a partial settings update.

    Referenced channels and roles must exist in *this* guild, so a request can
    never point one server's configuration at another server's objects. The
    database layer validates the column names again before building SQL, and
    every value is bound as a parameter.
    """
    enforce_rate_limit(request, "api")
    require_csrf(request)
    access = await authorize_guild(request, guild_id)

    values = payload.to_columns()
    if not values:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No settings were supplied."
        )

    guild = access.guild
    if guild is not None:
        for field, value in values.items():
            if value is None:
                continue
            if field in CHANNEL_FIELDS:
                if guild.get_channel(int(value)) is None:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"{field} does not reference a channel in this server.",
                    )
            elif field in ROLE_FIELDS:
                role = guild.get_role(int(value))
                if role is None:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"{field} does not reference a role in this server.",
                    )
                if role.is_default():
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"{field} cannot be the @everyone role.",
                    )
                me = guild.me
                if me is not None and me.top_role <= role:
                    # Storing a role Fyrion cannot assign would fail silently
                    # every time the feature ran.
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"{field} is at or above Fyrion's highest role, so it "
                        "could not be assigned.",
                    )

    db = get_db(request)
    await db.update_guild_settings(access.guild_id, **values)
    settings = serialize_ids(await db.get_guild_settings(access.guild_id))

    log.info(
        "Guild %s settings updated by user %s: %s",
        access.guild_id,
        request.session[SESSION_USER]["id"],
        ", ".join(sorted(values)),
    )

    return {"updated": sorted(values), "settings": settings}


@api.get("/bot/stats")
async def api_bot_stats(request: Request) -> dict[str, Any]:
    """Instance-level counters. Filesystem paths are never exposed."""
    enforce_rate_limit(request, "api")
    require_user(request)

    bot = get_bot(request)
    db = getattr(request.app.state, "db", None)

    database: dict[str, Any] = {}
    if db is not None and getattr(db, "is_connected", False):
        try:
            raw = await db.stats()
        except Exception:
            log.exception("Could not read the database statistics.")
            raw = {}
        database = {
            key: value for key, value in raw.items() if key in SAFE_DB_STAT_KEYS
        }

    payload: dict[str, Any] = {
        "version": Config.VERSION,
        "environment": Config.ENVIRONMENT,
        "online": bot is not None,
        "ready": bool(bot is not None and bot.is_ready()),
        "database": database,
    }

    if bot is None:
        payload.update(
            {
                "guilds": None,
                "users": None,
                "shards": None,
                "latency_ms": None,
                "cogs": [],
                "commands": None,
                "boot_time": None,
                "note": (
                    "The dashboard is running without the gateway client, so "
                    "live server data is unavailable."
                ),
            }
        )
        return payload

    latency = bot.latency
    boot_time = getattr(bot, "boot_time", None)

    payload.update(
        {
            "guilds": len(bot.guilds),
            "users": sum(
                guild.member_count or 0
                for guild in bot.guilds
                if guild.member_count is not None
            ),
            "shards": bot.shard_count or 1,
            # discord.py reports nan until the first heartbeat ack arrives.
            "latency_ms": round(latency * 1000) if latency == latency else None,
            "cogs": sorted(bot.cogs),
            "commands": len(bot.tree.get_commands()),
            "boot_time": boot_time.isoformat() if boot_time is not None else None,
        }
    )
    return payload


# ---------------------------------------------------------------------------
# Middleware and error handlers
# ---------------------------------------------------------------------------


def _install_middleware(app: FastAPI) -> None:
    trusted = list(Config.DASHBOARD_TRUSTED_HOSTS)
    if trusted:
        # Blocks Host-header attacks and DNS rebinding against a local bind.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted)

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(),
        session_cookie="fyrion_session",
        max_age=Config.DASHBOARD_SESSION_TTL_SECONDS,
        # Lax rather than Strict, so the cookie still accompanies the top-level
        # redirect back from Discord.
        same_site="lax",
        https_only=Config.DASHBOARD_COOKIE_SECURE,
        domain=Config.DASHBOARD_COOKIE_DOMAIN,
    )

    origins = list(Config.DASHBOARD_ALLOWED_ORIGINS)
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        # Explicit origins only: the API authenticates with a cookie, so '*' is
        # rejected by Config.validate() and would be refused by browsers anyway.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token"],
            max_age=600,
        )

    @app.middleware("http")
    async def _harden(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if Config.DASHBOARD_COOKIE_SECURE:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # Authenticated payloads must never be cached by an intermediary.
        if request.url.path.startswith(("/api", "/login", "/logout", "/manage")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."

        # An unauthenticated page request is a sign-in prompt, not an error.
        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and not _wants_json(request)
        ):
            return login_redirect(request)

        if _wants_json(request):
            return JSONResponse(
                {"error": detail},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )

        return app.state.templates.TemplateResponse(
            "index.html",
            page_context(
                request,
                title="Something went wrong",
                error={"status": exc.status_code, "message": detail},
            ),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Field names and messages only: the submitted input is not echoed back.
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            {"error": "Validation failed.", "details": details},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        # A short reference lets a user quote the failure without exposing any
        # internals (SQL text, paths, tracebacks) to the browser.
        reference = uuid.uuid4().hex[:8]
        log.error(
            "Unhandled dashboard error on %s %s (reference %s)",
            request.method,
            request.url.path,
            reference,
            exc_info=exc,
        )

        if _wants_json(request):
            return JSONResponse(
                {"error": "An internal error occurred.", "reference": reference},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return app.state.templates.TemplateResponse(
            "index.html",
            page_context(
                request,
                title="Something went wrong",
                error={
                    "status": 500,
                    "message": (
                        "An internal error occurred. Quote reference "
                        f"{reference} when reporting it."
                    ),
                },
            ),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(bot: Any | None = None, *, db: Any | None = None) -> FastAPI:
    """Builds the dashboard application.

    Args:
        bot: a running gateway client. When supplied, the dashboard reads the
            live guild cache and the bot's own database pool, which is what
            makes the channel and role pickers work.
        db: an explicit database pool. Defaults to ``bot.db`` when a bot is
            given, otherwise a pool is opened for the dashboard's own lifetime.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One pooled HTTP client for the OAuth exchanges, created inside the
        # running loop as httpx expects.
        app.state.http = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
            headers={
                "User-Agent": f"Fyrion/{Config.VERSION} (dashboard)",
                "Accept": "application/json",
            },
        )

        app.state.owns_db = False
        if app.state.db is None:
            from fyrion.database.manager import DatabasePool

            pool = DatabasePool()
            await pool.connect()
            app.state.db = pool
            app.state.owns_db = True
            log.info("Dashboard opened its own database pool.")

        log.info(
            "Dashboard ready at %s (redirect URI: %s, gateway client: %s)",
            Config.DASHBOARD_BASE_URL,
            redirect_uri(),
            "attached" if app.state.bot is not None else "detached",
        )

        try:
            yield
        finally:
            await app.state.http.aclose()
            if app.state.owns_db and app.state.db is not None:
                await app.state.db.close()

    docs_enabled = not Config.is_production()

    app = FastAPI(
        title="Fyrion Dashboard",
        version=Config.VERSION,
        description="Configuration UI and API for the Fyrion Discord bot.",
        lifespan=lifespan,
        # The schema describes an authenticated admin surface, so it is not
        # published in production.
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    app.state.bot = bot
    app.state.db = db if db is not None else getattr(bot, "db", None)
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.limiter_api = SlidingWindowLimiter(
        Config.DASHBOARD_RATE_LIMIT, Config.DASHBOARD_RATE_LIMIT_WINDOW
    )
    app.state.limiter_auth = SlidingWindowLimiter(
        Config.DASHBOARD_AUTH_RATE_LIMIT, Config.DASHBOARD_RATE_LIMIT_WINDOW
    )

    _install_middleware(app)
    _install_exception_handlers(app)

    if STATIC_DIR.is_dir():
        app.mount(
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )
    else:  # pragma: no cover - only when the package is installed incompletely
        log.warning("Static asset directory %s is missing.", STATIC_DIR)

    app.include_router(pages)
    app.include_router(auth)
    app.include_router(api)

    # ``/callback`` is always available; when DASHBOARD_OAUTH_CALLBACK_PATH names
    # a different path, that one is registered too so an existing Developer
    # Portal redirect keeps working.
    app.add_api_route(
        DEFAULT_CALLBACK_PATH,
        oauth_callback,
        methods=["GET"],
        name="oauth_callback",
        include_in_schema=False,
    )
    configured = callback_path()
    if configured != DEFAULT_CALLBACK_PATH:
        app.add_api_route(
            configured,
            oauth_callback,
            methods=["GET"],
            name="oauth_callback_configured",
            include_in_schema=False,
        )

    return app


__all__ = [
    "create_app",
    "GuildAccess",
    "OAuthError",
    "SlidingWindowLimiter",
    "authorize_guild",
    "can_manage",
    "callback_path",
    "exchange_code",
    "invite_url",
    "oauth_configured",
    "redirect_uri",
    "safe_next_path",
]
