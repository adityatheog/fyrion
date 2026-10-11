"""
Standalone Fyrion dashboard.

``python -m dashboard`` serves this application as its own process, without the
gateway client. It reuses Fyrion's SQLite pool and the strict
:class:`fyrion.web.models.GuildSettingsUpdate` schema, but it does **not** reuse
``fyrion.web``: that application is bound to a running gateway client and
authorizes guild access against the bot's live cache. This one has no client, so
it authorizes from the signed-in user's own OAuth grant instead.

Security posture
----------------
* Every page and every ``/api`` route requires a signed-in session. The only
  unauthenticated routes are the OAuth handshake itself.
* The session is a Starlette signed cookie (HttpOnly, ``SameSite=Lax``, and
  ``Secure`` whenever the dashboard is configured with an https base URL). The
  Discord access token is exchanged once, used to read the profile and the
  user's guild list, and then revoked; it is never stored anywhere.
* Guild authorization is decided server-side from Discord's own answer. The
  OAuth ``state`` is double-submitted through the session, and mutating requests
  must echo the session's CSRF token in ``X-CSRF-Token``.
* Because there is no live client, channel and role settings cannot be validated
  against the guild's real objects. They are *refused* rather than written
  blind: a detached dashboard edits text and numeric settings only.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, NamedTuple
from urllib.parse import urlencode

import aiohttp
import discord
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fyrion.config import Config
from fyrion.database.manager import DatabasePool
from fyrion.web.models import CHANNEL_FIELDS, ROLE_FIELDS, GuildSettingsUpdate

log = logging.getLogger("dashboard.app")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
REVOKE_URL = f"{DISCORD_API}/oauth2/token/revoke"
CURRENT_USER_URL = f"{DISCORD_API}/users/@me"
CURRENT_USER_GUILDS_URL = f"{DISCORD_API}/users/@me/guilds"

#: ``guilds`` is what makes the server picker possible without a live client.
#: It is read-only and far narrower than ``bot``.
OAUTH_SCOPES = "identify guilds"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Discord permission bits used for the server picker.
PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20

#: Permissions requested by the "Invite Fyrion" button. Deliberately a curated
#: set rather than Administrator: the dashboard should not be the thing that
#: hands the bot more power than its commands need.
INVITE_PERMISSIONS = (
    0x0000000000000400  # View Channels
    | 0x0000000000000800  # Send Messages
    | 0x0000000000002000  # Manage Messages
    | 0x0000000000001000  # Embed Links
    | 0x0000000000004000  # Read Message History
    | 0x0000000000000080  # Add Reactions
    | 0x0000000000000002  # Kick Members
    | 0x0000000000000004  # Ban Members
    | 0x0000000001000000  # Moderate Members (timeout)
    | 0x0000000010000000  # Manage Roles
    | 0x0000000000000010  # Manage Channels
    | 0x0000000000010000  # Manage Nicknames
    | 0x0000000000000020  # Manage Guild
    | 0x0000000020000000  # Manage Webhooks
)

# Session keys. Kept as module constants so the tests and the views cannot drift.
SESSION_USER = "user"
SESSION_GUILDS = "guilds"
SESSION_CSRF = "csrf"
SESSION_STATE = "oauth_state"

# ``DatabasePool.stats()`` keys that are safe to publish. ``db_url`` is a
# filesystem path and is deliberately omitted.
_SAFE_DB_STAT_KEYS = frozenset(
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

# The dashboard serves HTML, a stylesheet and two external scripts, so the
# Content-Security-Policy has to be wider than the JSON-only API's
# ``default-src 'none'``. It still forbids inline script, inline style, framing
# and any origin other than this one plus Discord's avatar CDN.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "img-src 'self' https://cdn.discordapp.com data:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Session / authorization
# ---------------------------------------------------------------------------


class GuildAccess(NamedTuple):
    """An authorized guild, plus whatever live objects are available for it.

    ``guild`` and ``member`` are ``None`` whenever the dashboard runs without
    the gateway client; ``entry`` is Discord's own answer from
    ``GET /users/@me/guilds`` and is always present.
    """

    guild_id: int
    guild: discord.Guild | None
    member: discord.Member | None
    entry: dict[str, Any]


def current_user(request: Request) -> dict[str, Any] | None:
    """Returns the signed-in user, or None."""
    user = request.session.get(SESSION_USER)
    return user if isinstance(user, dict) and user.get("id") else None


def ensure_csrf(request: Request) -> str:
    """Returns the session's CSRF token, minting one on first use."""
    token = request.session.get(SESSION_CSRF)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


def require_csrf(request: Request) -> None:
    """Rejects a mutating request that does not echo the session CSRF token."""
    expected = request.session.get(SESSION_CSRF)
    supplied = request.headers.get("x-csrf-token")
    if not isinstance(expected, str) or not supplied:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token."
        )
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token."
        )


def _as_int(value: object) -> int | None:
    """Returns ``value`` as an int, or None when it is absent or malformed.

    Discord sends ids as JSON numbers *or* strings depending on the endpoint, and
    a session minted before a change can hold neither, so every id read out of a
    session goes through this one narrowing point rather than a bare ``int()``.
    """
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_can_manage(entry: dict[str, Any]) -> bool:
    """Whether Discord says this user may configure the guild."""
    if entry.get("owner"):
        return True
    try:
        permissions = int(entry.get("permissions") or 0)
    except (TypeError, ValueError):
        return False
    return bool(permissions & PERM_ADMINISTRATOR or permissions & PERM_MANAGE_GUILD)


async def authorize_guild(request: Request, guild_id: int) -> GuildAccess:
    """Authorizes the caller for ``guild_id`` or raises.

    The decision comes from two server-side sources only: the signed session
    (which user, which guilds Discord reported) and, when the gateway client is
    attached, the client's own view. Nothing the browser sends beyond the guild
    id in the path influences the outcome.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Authentication required."
        )

    try:
        wanted = int(guild_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Unknown server."
        ) from None

    entry: dict[str, Any] | None = None
    for candidate in request.session.get(SESSION_GUILDS) or []:
        if not isinstance(candidate, dict):
            continue
        if _as_int(candidate.get("id")) == wanted:
            entry = candidate
            break

    if entry is None or not _entry_can_manage(entry):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You need the Manage Server permission in that server.",
        )

    bot = getattr(request.app.state, "bot", None)
    guild = bot.get_guild(wanted) if bot is not None else None

    member: discord.Member | None = None
    if guild is not None:
        member = guild.get_member(int(user["id"]))
        if member is None:
            member = await _fetch_member(guild, int(user["id"]))

    return GuildAccess(wanted, guild, member, entry)


async def _fetch_member(
    guild: discord.Guild, user_id: int
) -> discord.Member | None:
    """Fills a cold member cache entry, tolerating a refused or missing fetch."""
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden):
        return None
    except discord.HTTPException as exc:
        log.warning("Could not fetch member %s in guild %s: %s", user_id, guild.id, exc)
        return None


def _pool(request: Request) -> DatabasePool:
    db = getattr(request.app.state, "db", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Database is not available yet."
        )
    return db


# ---------------------------------------------------------------------------
# Settings updates
# ---------------------------------------------------------------------------


async def api_update_guild(
    guild_id: int,
    payload: GuildSettingsUpdate,
    request: Request,
) -> dict[str, Any]:
    """Applies a partial settings update for a guild.

    Referenced channels and roles are validated against the *live* guild when
    one is attached. Without a live client there is nothing to validate the
    snowflakes against, so an update that touches a channel or role field is
    refused instead of being written unchecked; text and numeric settings still
    apply, which is what the dashboard advertises when the client is absent.
    """
    require_csrf(request)
    access = await authorize_guild(request, guild_id)

    values = payload.to_columns()
    if not values:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No settings were supplied.")

    guild = access.guild

    if guild is None:
        object_fields = {
            field: value
            for field, value in values.items()
            if value is not None and (field in CHANNEL_FIELDS or field in ROLE_FIELDS)
        }
        if object_fields:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This server is not currently available for live validation. "
                "Channel and role settings cannot be updated until the server is "
                "reachable again.",
            )

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
                    # Storing a role the bot cannot assign would fail silently
                    # on the next join, so it is refused at configuration time.
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"{field} is above Fyrion's highest role, so it could "
                        "not be assigned.",
                    )

    pool = _pool(request)
    await pool.update_guild_settings(access.guild_id, **values)
    settings = await pool.get_guild_settings(access.guild_id)

    log.info(
        "Guild %s settings updated by user %s: %s",
        access.guild_id,
        request.session.get(SESSION_USER, {}).get("id", "?"),
        ", ".join(sorted(values)),
    )
    return {"updated": sorted(values), "settings": settings}


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def oauth_ready() -> bool:
    return bool(Config.DISCORD_CLIENT_ID and Config.DISCORD_CLIENT_SECRET)


def _authorize_url(state: str) -> str:
    params = {
        "client_id": Config.DISCORD_CLIENT_ID or "",
        "redirect_uri": Config.dashboard_redirect_uri(),
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "none",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def _exchange_code(
    session: aiohttp.ClientSession, code: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Trades an authorization code for the profile and the user's guild list.

    The access token is used for exactly these two reads and is then revoked in
    a ``finally`` block, so no live credential outlives the login.
    """
    payload = {
        "client_id": Config.DISCORD_CLIENT_ID or "",
        "client_secret": Config.DISCORD_CLIENT_SECRET or "",
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": Config.dashboard_redirect_uri(),
    }

    try:
        async with session.post(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status != 200:
                body = await response.text()
                log.warning(
                    "OAuth token exchange failed (HTTP %s): %s",
                    response.status,
                    body[:500],
                )
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Discord rejected the authorization code.",
                )
            token_data = await response.json()
    except aiohttp.ClientError as exc:
        log.warning("OAuth token exchange transport error: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not reach Discord to complete the login.",
        ) from exc

    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Discord returned an unusable token response."
        )

    try:
        profile = await _get_json(session, CURRENT_USER_URL, access_token)
        guilds = await _get_json(session, CURRENT_USER_GUILDS_URL, access_token)
    finally:
        await _revoke_token(session, access_token)

    user_id = profile.get("id") if isinstance(profile, dict) else None
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Discord returned an unusable profile."
        )
    if not isinstance(guilds, list):
        guilds = []

    return profile, [item for item in guilds if isinstance(item, dict)]


async def _get_json(
    session: aiohttp.ClientSession, url: str, access_token: str
) -> Any:
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status != 200:
                log.warning("Discord returned HTTP %s for %s.", response.status, url)
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Discord did not return the requested profile data.",
                )
            return await response.json()
    except aiohttp.ClientError as exc:
        log.warning("Transport error reading %s: %s", url, exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not reach Discord to complete the login.",
        ) from exc


async def _revoke_token(session: aiohttp.ClientSession, access_token: str) -> None:
    payload = {
        "client_id": Config.DISCORD_CLIENT_ID or "",
        "client_secret": Config.DISCORD_CLIENT_SECRET or "",
        "token": access_token,
        "token_type_hint": "access_token",
    }
    try:
        async with session.post(
            REVOKE_URL, data=payload, timeout=REQUEST_TIMEOUT
        ) as response:
            if response.status >= 400:
                log.debug("Token revocation returned HTTP %s.", response.status)
    except aiohttp.ClientError as exc:
        log.debug("Token revocation failed: %s", exc)


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    """Renders a page with the context every template's base layout expects."""
    user = current_user(request)
    payload: dict[str, Any] = {
        "user": user,
        "oauth_ready": oauth_ready(),
        "version": Config.VERSION,
        "environment": Config.ENVIRONMENT,
        "invite_url": _invite_url(),
        **context,
    }
    return templates.TemplateResponse(request, template, payload)


def _invite_url() -> str | None:
    if not Config.DISCORD_CLIENT_ID:
        return None
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": Config.DISCORD_CLIENT_ID,
            "scope": "bot applications.commands",
            "permissions": INVITE_PERMISSIONS,
        }
    )


def _icon_url(guild_id: Any, icon: Any) -> str | None:
    if not icon:
        return None
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon}.png?size=128"


def _guild_card(
    entry: dict[str, Any], *, bot_present: bool, shard_id: int | None
) -> dict[str, Any]:
    """Builds a server-picker card from Discord's OAuth guild entry."""
    guild_id = str(entry.get("id") or "")
    try:
        permissions = int(entry.get("permissions") or 0)
    except (TypeError, ValueError):
        permissions = 0
    owner = bool(entry.get("owner"))
    return {
        "id": guild_id,
        "name": str(entry.get("name") or "Unknown server"),
        "icon_url": _icon_url(guild_id, entry.get("icon")),
        "owner": owner,
        "administrator": bool(permissions & PERM_ADMINISTRATOR),
        "member_count": None,
        "bot_present": bot_present,
        "shard_id": shard_id,
    }


def _channel_options(channel: Any) -> dict[str, Any]:
    return {
        "id": str(channel.id),
        "name": channel.name,
        "writable": None,
    }


def _role_options(role: discord.Role, me: discord.Member | None) -> dict[str, Any]:
    return {
        "id": str(role.id),
        "name": role.name,
        "is_default": role.is_default(),
        "assignable": bool(
            me is not None
            and not role.managed
            and not role.is_default()
            and me.top_role > role
        ),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse, name="index")
    async def index(request: Request) -> HTMLResponse:
        return _render(request, "index.html")

    @app.get("/login", name="login")
    async def login(request: Request) -> RedirectResponse:
        if not oauth_ready():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OAuth is not configured on this instance.",
            )
        state = secrets.token_urlsafe(32)
        request.session[SESSION_STATE] = state
        # Always re-mint the CSRF token on a fresh login so a token captured
        # before sign-in cannot be replayed against the new session.
        request.session.pop(SESSION_CSRF, None)
        return RedirectResponse(
            _authorize_url(state), status_code=status.HTTP_307_TEMPORARY_REDIRECT
        )

    @app.get("/logout", name="logout_get", include_in_schema=False)
    async def logout_get() -> RedirectResponse:
        # The sign-out control is a POST form; a GET is only ever a stale
        # bookmark, so it is answered with the page rather than an error.
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout", name="logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    async def oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Completes the handshake and establishes the signed session."""
        if error:
            log.info("OAuth callback returned an error: %s", error)
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "The authorization request was declined."
            )
        if not code or not state:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Missing authorization code or state."
            )

        expected = request.session.get(SESSION_STATE)
        if not isinstance(expected, str) or not hmac.compare_digest(state, expected):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "The login attempt expired or could not be verified. Try again.",
            )

        http: aiohttp.ClientSession = request.app.state.http
        profile, guilds = await _exchange_code(http, code)

        request.session.clear()
        request.session[SESSION_USER] = {
            "id": str(profile["id"]),
            "username": str(profile.get("username") or "Discord user"),
            "avatar_url": _avatar_url(profile),
        }
        request.session[SESSION_GUILDS] = [
            entry for entry in guilds if _entry_can_manage(entry)
        ]
        ensure_csrf(request)

        log.info("Dashboard session created for user %s.", profile["id"])
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    app.add_api_route(
        Config.DASHBOARD_OAUTH_CALLBACK_PATH,
        oauth_callback,
        methods=["GET"],
        name="oauth_callback",
        include_in_schema=False,
    )

    @app.get("/dashboard", response_class=HTMLResponse, name="dashboard_page")
    async def dashboard_page(request: Request) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

        pool = _pool(request)
        bot = getattr(request.app.state, "bot", None)

        cards: list[dict[str, Any]] = []
        managed_count = 0
        for entry in request.session.get(SESSION_GUILDS) or []:
            if not isinstance(entry, dict):
                continue
            guild_id = _as_int(entry.get("id"))
            if guild_id is None:
                continue

            live = bot.get_guild(guild_id) if bot is not None else None
            if live is not None:
                present = True
                shard_id = getattr(live, "shard_id", None)
            else:
                # Without a client the only durable evidence that Fyrion has
                # been in a guild is the settings row created when it joined.
                present = await pool.exists("guild_settings", {"guild_id": guild_id})
                shard_id = None

            if present:
                managed_count += 1
            cards.append(_guild_card(entry, bot_present=present, shard_id=shard_id))

        cards.sort(key=lambda card: card["name"].lower())

        return _render(
            request,
            "dashboard.html",
            guilds=cards,
            managed_count=managed_count,
            bot_online=bot is not None,
        )

    @app.get("/manage/{guild_id}", response_class=HTMLResponse, name="manage_page")
    async def manage_page(request: Request, guild_id: int) -> HTMLResponse:
        access = await authorize_guild(request, guild_id)
        pool = _pool(request)

        settings = await pool.get_guild_settings(access.guild_id)

        guild_info: dict[str, Any] = {
            "id": str(access.guild_id),
            "name": str(access.entry.get("name") or "Unknown server"),
            "icon_url": _icon_url(access.guild_id, access.entry.get("icon")),
            "member_count": None,
            "live": access.guild is not None,
        }

        text_channels: list[dict[str, Any]] = []
        categories: list[dict[str, Any]] = []
        roles: list[dict[str, Any]] = []

        if access.guild is not None:
            me = access.guild.me
            guild_info["member_count"] = access.guild.member_count
            for channel in access.guild.text_channels:
                options = _channel_options(channel)
                if me is not None:
                    permissions = channel.permissions_for(me)
                    options["writable"] = bool(
                        permissions.send_messages and permissions.embed_links
                    )
                text_channels.append(options)
            categories = [
                {"id": str(category.id), "name": category.name}
                for category in access.guild.categories
            ]
            roles = [
                _role_options(role, me)
                for role in sorted(
                    access.guild.roles, key=lambda item: item.position, reverse=True
                )
            ]

        bootstrap = json.dumps(
            {"guild_id": str(access.guild_id), "csrf": ensure_csrf(request)}
        )

        return _render(
            request,
            "manage.html",
            guild=guild_info,
            settings=settings,
            text_channels=text_channels,
            categories=categories,
            roles=roles,
            bootstrap_json=bootstrap,
        )

    @app.get("/api/bot/stats")
    async def bot_stats(request: Request) -> JSONResponse:
        if current_user(request) is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Authentication required."
            )

        pool = _pool(request)
        bot = getattr(request.app.state, "bot", None)

        raw = await pool.stats()
        database = {
            key: value for key, value in raw.items() if key in _SAFE_DB_STAT_KEYS
        }

        if bot is None:
            # No gateway client: report what the database can actually prove
            # rather than inventing gateway numbers.
            return JSONResponse(
                {
                    "guilds": await pool.count("guild_settings"),
                    "users": None,
                    "shards": 1,
                    "latency_ms": None,
                    "commands": 0,
                    "cogs": [],
                    "note": (
                        "Running without the gateway client: counters come from "
                        "stored data only."
                    ),
                    "database": database,
                }
            )

        latency = bot.latency
        return JSONResponse(
            {
                "guilds": len(bot.guilds),
                "users": sum(
                    len(guild.members) for guild in bot.guilds if guild.chunked
                )
                or None,
                "shards": bot.shard_count or 1,
                "latency_ms": round(latency * 1000) if latency == latency else None,
                "commands": len(bot.tree.get_commands()),
                "cogs": sorted(bot.cogs),
                "note": None,
                "database": database,
            }
        )

    app.add_api_route(
        "/api/guilds/{guild_id}",
        api_update_guild,
        methods=["PATCH"],
        name="api_update_guild",
    )


def _avatar_url(profile: dict[str, Any]) -> str | None:
    user_id = profile.get("id")
    avatar = profile.get("avatar")
    if not user_id:
        return None
    if not avatar:
        # Discord's default avatar index is derived from the snowflake.
        try:
            index = (int(user_id) >> 22) % 6
        except (TypeError, ValueError):
            index = 0
        return f"https://cdn.discordapp.com/embed/avatars/{index}.png"
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def _wants_html(request: Request) -> bool:
    """Whether the caller is a browser rather than the dashboard's own JS."""
    if request.url.path.startswith("/api/"):
        return False
    accept = request.headers.get("accept") or ""
    return "text/html" in accept


def _apply_security_headers(request: Request, response: Response) -> Response:
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if Config.DASHBOARD_COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    if request.url.path.startswith(("/api", "/dashboard", "/manage")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _install_handlers(app: FastAPI) -> None:
    """Installs the error handlers and the hardening middleware.

    The hardening middleware catches unhandled exceptions itself, *before* they
    reach Starlette's outermost ``ServerErrorMiddleware``. That ordering is the
    point: a handler registered for ``Exception`` runs outside every user
    middleware, so a 500 produced there would be the one response that ships
    without the security headers. Catching one layer in keeps every response on
    the same path.
    """

    @app.middleware("http")
    async def _harden(request: Request, call_next: Any) -> Response:
        try:
            response = await call_next(request)
        except Exception as exc:
            reference = uuid.uuid4().hex[:8]
            log.error(
                "Unhandled dashboard error on %s %s (reference %s)",
                request.method,
                request.url.path,
                reference,
                exc_info=exc,
            )
            response = JSONResponse(
                {"error": "An internal error occurred.", "reference": reference},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return _apply_security_headers(request, response)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if _wants_html(request):
            return _render(
                request,
                "base.html",
                error={"status": exc.status_code, "message": str(exc.detail)},
            )
        return JSONResponse(
            {
                "error": str(exc.detail),
                "details": [{"field": None, "message": str(exc.detail)}],
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_payload(
        request: Request, exc: RequestValidationError
    ) -> Response:
        details: list[dict[str, Any]] = []
        for error in exc.errors():
            location = [
                str(part)
                for part in error.get("loc", ())
                if part not in {"body", "query", "path", "header"}
            ]
            details.append(
                {
                    "field": ".".join(location) or None,
                    "message": str(error.get("msg", "Invalid value.")),
                }
            )
        log.info(
            "Rejected invalid payload for %s %s: %s",
            request.method,
            request.url.path,
            details,
        )
        return JSONResponse(
            {
                "error": "The request was rejected because it was not valid.",
                "details": details,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


def create_app() -> FastAPI:
    """Builds the standalone dashboard application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.bot = None

        pool = DatabasePool(
            db_url=Config.DATABASE_URL,
            pool_size=Config.DATABASE_POOL_SIZE,
            busy_timeout_ms=Config.DATABASE_BUSY_TIMEOUT_MS,
        )
        await pool.connect()
        app.state.db = pool

        # aiohttp requires the session to be created inside a running loop.
        app.state.http = aiohttp.ClientSession(
            headers={"User-Agent": f"Fyrion/{Config.VERSION} (dashboard)"}
        )
        log.info(
            "Standalone dashboard ready at %s (redirect URI: %s)",
            Config.DASHBOARD_BASE_URL,
            Config.dashboard_redirect_uri(),
        )
        try:
            yield
        finally:
            await app.state.http.close()
            await pool.close()

    app = FastAPI(
        title="Fyrion Dashboard",
        version=Config.VERSION,
        description="Configuration dashboard for the Fyrion Discord bot.",
        lifespan=lifespan,
        docs_url="/docs" if not Config.is_production() else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not Config.is_production() else None,
    )

    if Config.DASHBOARD_TRUSTED_HOSTS:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=list(Config.DASHBOARD_TRUSTED_HOSTS)
        )

    secret_key = Config.DASHBOARD_SECRET_KEY
    if not secret_key:
        # ``python -m dashboard`` calls Config.validate() first, which refuses to
        # start without a key, so this is only reachable when create_app() is
        # driven directly. Production still fails closed rather than signing
        # sessions with a key that changes on every restart.
        if Config.is_production():
            raise RuntimeError(
                "DASHBOARD_SECRET_KEY is required in production; refusing to "
                "sign sessions with an ephemeral key."
            )
        secret_key = secrets.token_urlsafe(48)
        log.warning(
            "DASHBOARD_SECRET_KEY is unset, so an ephemeral session key was "
            "generated: every restart signs every user out. Set it before "
            "deploying."
        )

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="fyrion_dashboard",
        max_age=Config.DASHBOARD_SESSION_TTL_SECONDS,
        same_site="lax",
        https_only=Config.DASHBOARD_COOKIE_SECURE,
    )

    # Added last so it wraps every other middleware, including the trusted-host
    # guard: that way the security headers and the caught-exception 500 apply to
    # every response the application can produce.
    _install_handlers(app)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _register_routes(app)
    return app


__all__ = [
    "GuildAccess",
    "SESSION_CSRF",
    "SESSION_GUILDS",
    "SESSION_USER",
    "api_update_guild",
    "authorize_guild",
    "create_app",
    "require_csrf",
]
