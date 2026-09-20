"""
FastAPI application for the Fyrion dashboard.

Security posture
----------------
* Every endpoint under ``/api`` requires an authenticated session cookie. The
  only unauthenticated routes are ``/`` (static metadata), ``/health`` and the
  OAuth handshake itself.
* Authorization is not taken from the browser: guild access requires that the
  session's user is a member of the guild *and* holds ``Manage Server`` (or
  ``Administrator``) there, checked against the bot's own view of Discord.
* Sessions are opaque tokens stored as keyed hashes; cookies are HttpOnly and,
  outside local development, Secure.
* Responses carry hardening headers, the ``Host`` header is validated, CORS is
  origin-restricted (never ``*`` with credentials), and unhandled exceptions
  return a reference id instead of internals.
* Writes are validated by a strict pydantic model and by the database layer's
  identifier allow-list, and every value is bound as a SQL parameter.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Deque

import aiohttp
import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fyrion.config import Config
from fyrion.database.manager import iso_from_now
from fyrion.web import auth, security
from fyrion.web.models import CHANNEL_FIELDS, ROLE_FIELDS, GuildSettingsUpdate

log = logging.getLogger("fyrion.web.app")

# Keys from DatabasePool.stats() that are safe to expose. ``db_url`` is a
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

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class SlidingWindowLimiter:
    """Small in-process sliding-window limiter.

    Good enough for a single-process self-hosted dashboard: it blunts credential
    stuffing and accidental request storms. Deployments behind a shared edge
    should also rate limit there.
    """

    def __init__(self, limit: int, window: int, *, max_keys: int = 10_000) -> None:
        self.limit = max(1, limit)
        self.window = max(1, window)
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
        if len(self._hits) < self.max_keys:
            return
        # Every bucket is still active. Drop the least-recently-used entries
        # rather than clearing the whole table: a full clear would reset every
        # caller's counters (including a flooder's) under the exact load the
        # limiter exists to handle. Evict a batch so this need not run again on
        # the very next request.
        overflow = len(self._hits) - self.max_keys + 1
        to_drop = max(overflow, self.max_keys // 10)
        oldest = sorted(self._hits, key=lambda key: self._hits[key][-1])[:to_drop]
        for key in oldest:
            del self._hits[key]


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _bot(request: Request) -> commands.Bot:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:  # pragma: no cover - lifespan always sets this
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Bot is not available."
        )
    return bot


def _pool(request: Request) -> Any:
    db = getattr(_bot(request), "db", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Database is not available yet."
        )
    return db


def _client_ip(request: Request) -> str | None:
    # When DASHBOARD_TRUST_PROXY is set, uvicorn is configured with
    # proxy_headers + forwarded_allow_ips, so it has already parsed
    # X-Forwarded-For against the trusted hop list and put the real client IP in
    # request.client.host. Reading the raw header here would instead trust the
    # left-most (client-supplied) entry, which a caller can rotate per request
    # to defeat the auth/login rate limiter.
    return request.client.host if request.client else None


def _enforce_rate_limit(request: Request, scope: str) -> None:
    limiter: SlidingWindowLimiter | None = getattr(
        request.app.state, f"limiter_{scope}", None
    )
    if limiter is None:
        return
    key = _client_ip(request) or "unknown"
    if not limiter.allow(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Please slow down.",
            headers={"Retry-After": str(limiter.window)},
        )


async def require_session(request: Request) -> dict[str, Any]:
    """Resolves and refreshes the caller's session, or rejects the request."""
    _enforce_rate_limit(request, "api")

    token = request.cookies.get(security.SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Authentication required."
        )

    pool = _pool(request)
    session = await pool.get_active_session(security.hash_token(token))
    if session is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again."
        )

    await pool.touch_session(session["token_hash"])
    return session


async def require_csrf(request: Request) -> None:
    """Rejects a mutating request that does not carry the session CSRF token.

    Defence in depth on top of the ``SameSite=Lax`` session cookie: the client
    reads its token from an authenticated ``/api`` response (see ``/api/me``)
    and echoes it in the ``X-CSRF-Token`` header. The token is a keyed HMAC of
    the session token, so it is bound to the session and cannot be forged
    without reading the HttpOnly cookie. GET routes are never guarded; only the
    mutating endpoints depend on this.
    """
    token = request.cookies.get(security.SESSION_COOKIE)
    supplied = request.headers.get("x-csrf-token")
    if not security.verify_csrf(token, supplied):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Missing or invalid CSRF token.",
        )


async def _resolve_member(
    guild: discord.Guild, user_id: int, *, allow_fetch: bool = True
) -> discord.Member | None:
    """Returns a guild member from cache, falling back to a REST fetch.

    ``chunk_guilds_at_startup`` is disabled, so the member cache is populated
    lazily; the dashboard's caller is usually not in it.
    """
    member = guild.get_member(user_id)
    if member is not None:
        return member
    if not allow_fetch:
        return None

    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden):
        return None
    except discord.HTTPException as exc:
        log.warning(
            "Could not fetch member %s in guild %s: %s", user_id, guild.id, exc
        )
        return None


def _can_manage(member: discord.Member) -> bool:
    permissions = member.guild_permissions
    return bool(permissions.administrator or permissions.manage_guild)


class GuildContext:
    """An authorized (guild, session) pair."""

    __slots__ = ("guild", "session", "member")

    def __init__(
        self,
        guild: discord.Guild,
        member: discord.Member,
        session: dict[str, Any],
    ) -> None:
        self.guild = guild
        self.member = member
        self.session = session


async def require_guild_manager(
    guild_id: int,
    request: Request,
    session: dict[str, Any] = Depends(require_session),
) -> GuildContext:
    """Authorizes the caller for a specific guild.

    Server-side check by design: the client cannot influence the outcome, and a
    404 is returned for guilds Fyrion is not in so the API does not confirm
    whether an arbitrary snowflake exists.
    """
    bot = _bot(request)
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Fyrion is not a member of that server."
        )

    member = await _resolve_member(guild, int(session["user_id"]))
    if member is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You are not a member of that server."
        )
    if not _can_manage(member):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The Manage Server permission is required for that server.",
        )

    return GuildContext(guild, member, session)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_ids(row: dict[str, Any]) -> dict[str, Any]:
    """Renders snowflakes as strings so JavaScript cannot lose precision."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if value is not None and isinstance(value, int) and key.endswith("_id"):
            result[key] = str(value)
        else:
            result[key] = value
    return result


def _guild_summary(guild: discord.Guild, member: discord.Member) -> dict[str, Any]:
    return {
        "id": str(guild.id),
        "name": guild.name,
        "icon_url": guild.icon.url if guild.icon else None,
        "member_count": guild.member_count,
        "owner": guild.owner_id == member.id,
        "administrator": member.guild_permissions.administrator,
    }


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

meta_router = APIRouter(tags=["meta"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])
api_router = APIRouter(prefix="/api", tags=["api"])


@meta_router.get("/")
async def index() -> dict[str, Any]:
    """Public metadata. Contains nothing that is not already public."""
    return {
        "name": "Fyrion",
        "version": Config.VERSION,
        "login": "/auth/login",
        "health": "/health",
    }


@meta_router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness probe. Intentionally minimal: no counts, no configuration."""
    bot = _bot(request)
    ready = bot.is_ready()
    return JSONResponse(
        {"status": "ok" if ready else "starting", "ready": ready},
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@auth_router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Starts the Discord OAuth handshake."""
    _enforce_rate_limit(request, "auth")

    if not auth.oauth_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OAuth is not configured on this instance.",
        )

    url, state = auth.login_redirect()
    response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    security.set_state_cookie(response, state)
    return response


@auth_router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Completes the handshake and issues a session cookie."""
    _enforce_rate_limit(request, "auth")

    if error:
        log.info("OAuth callback returned an error: %s", error)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The authorization request was declined."
        )
    if not code or not state:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Missing authorization code or state."
        )

    # Double submit: the signed state must match the HttpOnly cookie exactly.
    if not security.verify_state(state, request.cookies.get(security.STATE_COOKIE)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The login attempt expired or could not be verified. Try again.",
        )

    http: aiohttp.ClientSession = request.app.state.http
    try:
        profile = await auth.exchange_code(http, code)
    except auth.OAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    user_id = int(profile["id"])
    token = security.generate_token()
    ttl = Config.DASHBOARD_SESSION_TTL_SECONDS

    pool = _pool(request)
    await pool.create_dashboard_session(
        session_id=uuid.uuid4().hex,
        user_id=user_id,
        token_hash=security.hash_token(token),
        expires_at=iso_from_now(ttl),
        scopes=auth.OAUTH_SCOPES,
        ip_hash=security.hash_ip(_client_ip(request)),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )

    log.info("Dashboard session created for user %s.", user_id)

    response = RedirectResponse(
        Config.DASHBOARD_BASE_URL or "/", status_code=status.HTTP_303_SEE_OTHER
    )
    security.set_session_cookie(response, token, ttl)
    security.clear_state_cookie(response)
    return response


@auth_router.post("/logout")
async def logout(request:Request) -> JSONResponse:
    """Revokes the current session and clears the cookie."""
    token = request.cookies.get(security.SESSION_COOKIE)
    response = JSONResponse({"status": "signed_out"})

    if token:
        pool = _pool(request)
        await pool.revoke_session(security.hash_token(token))

    security.clear_session_cookie(response)
    return response


@auth_router.post("/logout-all")
async def logout_all(
    request: Request,
    session: dict[str, Any] = Depends(require_session),
    _csrf: None = Depends(require_csrf),
) -> JSONResponse:
    """Revokes every session belonging to the caller (all devices)."""
    pool = _pool(request)
    revoked = await pool.revoke_user_sessions(int(session["user_id"]))

    response = JSONResponse({"status": "signed_out", "revoked": revoked})
    security.clear_session_cookie(response)
    return response


@api_router.get("/me")
async def whoami(
    request: Request, session: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """Returns the caller's identity and the servers they may configure."""
    bot = _bot(request)
    user_id = int(session["user_id"])

    manageable: list[dict[str, Any]] = []
    for guild in bot.guilds:
        # Cache only: fetching a member for every guild would issue one REST
        # call per server on a page load.
        member = await _resolve_member(guild, user_id, allow_fetch=False)
        if member is not None and _can_manage(member):
            manageable.append(_guild_summary(guild, member))

    manageable.sort(key=lambda item: item["name"].lower())

    user = bot.get_user(user_id)
    # The caller already holds the (HttpOnly) session cookie; handing back the
    # CSRF token derived from it lets the page echo it in X-CSRF-Token on
    # mutating requests without ever exposing the cookie itself to JavaScript.
    cookie_token = request.cookies.get(security.SESSION_COOKIE)
    return {
        "user": {
            "id": str(user_id),
            "name": str(user) if user is not None else None,
            "avatar_url": user.display_avatar.url if user is not None else None,
        },
        "session": {
            "created_at": session.get("created_at"),
            "expires_at": session.get("expires_at"),
            "scopes": session.get("scopes"),
            "csrf_token": (
                security.csrf_token(cookie_token) if cookie_token else None
            ),
        },
        "guilds": manageable,
    }


@api_router.get("/stats")
async def stats(
    request: Request, session: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """Instance-level counters. Filesystem paths are never exposed."""
    bot = _bot(request)
    pool = _pool(request)

    raw = await pool.stats()
    database = {key: value for key, value in raw.items() if key in _SAFE_DB_STAT_KEYS}

    latency = bot.latency
    return {
        "version": Config.VERSION,
        "environment": Config.ENVIRONMENT,
        "ready": bot.is_ready(),
        "shards": bot.shard_count or 1,
        "guilds": len(bot.guilds),
        "latency_ms": round(latency * 1000) if latency == latency else None,
        "boot_time": getattr(bot, "boot_time", None),
        "cogs": sorted(bot.cogs),
        "database": database,
    }


@api_router.get("/guilds/{guild_id}")
async def guild_overview(
    context: GuildContext = Depends(require_guild_manager),
) -> dict[str, Any]:
    guild = context.guild
    return {
        "guild": _guild_summary(guild, context.member),
        "counts": {
            "roles": len(guild.roles),
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "categories": len(guild.categories),
        },
        "created_at": guild.created_at.isoformat(),
    }


@api_router.get("/guilds/{guild_id}/settings")
async def get_settings(
    request: Request, context: GuildContext = Depends(require_guild_manager)
) -> dict[str, Any]:
    pool = _pool(request)
    settings = await pool.get_guild_settings(context.guild.id)
    return _serialize_ids(settings)


@api_router.patch("/guilds/{guild_id}/settings")
async def patch_settings(
    payload: GuildSettingsUpdate,
    request: Request,
    context: GuildContext = Depends(require_guild_manager),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Applies a partial settings update.

    Referenced channels and roles must exist in *this* guild, so a request
    cannot point one server's configuration at another server's objects. The
    database layer validates the column names again before building SQL.
    """
    values = payload.to_columns()
    if not values:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No settings were supplied."
        )

    guild = context.guild
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
                # Storing a role the bot cannot assign would fail silently later.
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{field} is above Fyrion's highest role, so it could not "
                    "be assigned.",
                )

    pool = _pool(request)
    await pool.update_guild_settings(guild.id, **values)
    settings = await pool.get_guild_settings(guild.id)

    log.info(
        "Guild %s settings updated by user %s: %s",
        guild.id,
        context.session["user_id"],
        ", ".join(sorted(values)),
    )
    return _serialize_ids(settings)


@api_router.get("/guilds/{guild_id}/channels")
async def guild_channels(
    context: GuildContext = Depends(require_guild_manager),
) -> dict[str, Any]:
    guild = context.guild
    me = guild.me

    def describe(channel: discord.abc.GuildChannel) -> dict[str, Any]:
        writable = None
        if me is not None and isinstance(channel, discord.TextChannel):
            permissions = channel.permissions_for(me)
            writable = bool(permissions.send_messages and permissions.embed_links)
        return {
            "id": str(channel.id),
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category_id": str(channel.category_id) if channel.category_id else None,
            "writable": writable,
        }

    return {
        "text": [describe(channel) for channel in guild.text_channels],
        "voice": [describe(channel) for channel in guild.voice_channels],
        "categories": [describe(channel) for channel in guild.categories],
    }


@api_router.get("/guilds/{guild_id}/roles")
async def guild_roles(
    context: GuildContext = Depends(require_guild_manager),
) -> list[dict[str, Any]]:
    guild = context.guild
    me = guild.me

    roles = []
    for role in sorted(guild.roles, key=lambda item: item.position, reverse=True):
        roles.append(
            {
                "id": str(role.id),
                "name": role.name,
                "position": role.position,
                "color": str(role.color),
                "managed": role.managed,
                "is_default": role.is_default(),
                "administrator": role.permissions.administrator,
                # Tells the UI which roles are usable for autorole/mute.
                "assignable": bool(
                    me is not None
                    and not role.managed
                    and not role.is_default()
                    and me.top_role > role
                ),
            }
        )
    return roles


@api_router.get("/guilds/{guild_id}/cases")
async def guild_cases(
    request: Request,
    context: GuildContext = Depends(require_guild_manager),
    target_id: int | None = Query(default=None, ge=1),
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Moderation history for the guild, newest first."""
    pool = _pool(request)

    if target_id is not None:
        rows = await pool.get_member_cases(
            context.guild.id, target_id, active_only=active_only, limit=limit
        )
    else:
        where: dict[str, Any] = {"guild_id": context.guild.id}
        if active_only:
            where["active"] = 1
        rows = await pool.fetch_many(
            "moderation_cases", where, order_by="case_number DESC", limit=limit
        )

    return [_serialize_ids(row) for row in rows]


@api_router.get("/guilds/{guild_id}/tickets")
async def guild_tickets(
    request: Request,
    context: GuildContext = Depends(require_guild_manager),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    from fyrion.database.schema import TICKET_STATUSES

    where: dict[str, Any] = {"guild_id": context.guild.id}
    if status_filter is not None:
        if status_filter not in TICKET_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"status must be one of {sorted(TICKET_STATUSES)}.",
            )
        where["status"] = status_filter

    pool = _pool(request)
    rows = await pool.fetch_many(
        "tickets",
        where,
        # Transcripts can be large and are not needed for a list view.
        columns=[
            "ticket_id",
            "guild_id",
            "ticket_number",
            "channel_id",
            "user_id",
            "subject",
            "status",
            "claimed_by",
            "closed_by",
            "close_reason",
            "created_at",
            "closed_at",
        ],
        order_by="ticket_id DESC",
        limit=limit,
    )
    return [_serialize_ids(row) for row in rows]


@api_router.get("/guilds/{guild_id}/leaderboard/levels")
async def levels_leaderboard(
    request: Request,
    context: GuildContext = Depends(require_guild_manager),
    limit: int = Query(default=10, ge=1, le=100),
) -> list[dict[str, Any]]:
    pool = _pool(request)
    rows = await pool.leveling_leaderboard(context.guild.id, limit)
    return [_serialize_ids(row) for row in rows]


@api_router.get("/guilds/{guild_id}/leaderboard/economy")
async def economy_leaderboard(
    request: Request,
    context: GuildContext = Depends(require_guild_manager),
    limit: int = Query(default=10, ge=1, le=100),
) -> list[dict[str, Any]]:
    pool = _pool(request)
    rows = await pool.economy_leaderboard(context.guild.id, limit)
    return [_serialize_ids(row) for row in rows]


@api_router.get("/guilds/{guild_id}/automod")
async def automod_rules(
    request: Request,
    context: GuildContext = Depends(require_guild_manager),
    enabled_only: bool = Query(default=False),
) -> list[dict[str, Any]]:
    pool = _pool(request)
    rows = await pool.get_automod_rules(context.guild.id, enabled_only=enabled_only)
    return [_serialize_ids(row) for row in rows]


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def _install_middleware(app: FastAPI) -> None:
    trusted = list(Config.DASHBOARD_TRUSTED_HOSTS)
    if trusted:
        # Blocks Host-header attacks and DNS rebinding against a local bind.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted)

    origins = list(Config.DASHBOARD_ALLOWED_ORIGINS)
    if origins:
        # Explicit origins only: cookies are credentials, so '*' is rejected by
        # Config.validate() and would be refused by browsers anyway.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
            max_age=600,
        )

    @app.middleware("http")
    async def _harden(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if Config.DASHBOARD_COOKIE_SECURE:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # Authenticated payloads must never be cached by an intermediary.
        if request.url.path.startswith(("/api", "/auth")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Field names and messages only: the raw input is not echoed back.
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
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
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
        return JSONResponse(
            {
                "error": "An internal error occurred.",
                "reference": reference,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def create_app(bot: commands.Bot) -> FastAPI:
    """Builds the dashboard application bound to a running bot instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One shared aiohttp session for the OAuth exchanges, created inside the
        # running loop as aiohttp requires.
        app.state.http = aiohttp.ClientSession(
            headers={"User-Agent": f"Fyrion/{Config.VERSION} (dashboard)"}
        )
        log.info(
            "Dashboard ready at %s (redirect URI: %s)",
            Config.DASHBOARD_BASE_URL,
            Config.dashboard_redirect_uri(),
        )
        try:
            yield
        finally:
            await app.state.http.close()

    docs_enabled = not Config.is_production()

    app = FastAPI(
        title="Fyrion Dashboard",
        version=Config.VERSION,
        description="Configuration API for the Fyrion Discord bot.",
        lifespan=lifespan,
        # The schema describes an authenticated admin surface, so it is not
        # published in production.
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    app.state.bot = bot
    app.state.limiter_api = SlidingWindowLimiter(
        Config.DASHBOARD_RATE_LIMIT, Config.DASHBOARD_RATE_LIMIT_WINDOW
    )
    app.state.limiter_auth = SlidingWindowLimiter(
        Config.DASHBOARD_AUTH_RATE_LIMIT, Config.DASHBOARD_RATE_LIMIT_WINDOW
    )

    _install_middleware(app)
    _install_exception_handlers(app)

    app.include_router(meta_router)
    app.include_router(auth_router)
    app.include_router(api_router)

    return app


__all__ = [
    "create_app",
    "SlidingWindowLimiter",
    "GuildContext",
    "require_session",
    "require_guild_manager",
]
