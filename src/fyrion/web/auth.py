"""
Discord OAuth2 authorization-code flow for the dashboard.

What is deliberately *not* done here: Fyrion never stores the user's Discord
access or refresh token. The code is exchanged once to learn the user's id, the
access token is revoked immediately afterwards, and every later authorization
decision is made from the bot's own guild and member cache. That keeps the
blast radius of a database compromise limited to opaque session hashes.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import aiohttp

from fyrion.config import Config
from fyrion.web import security

log = logging.getLogger("fyrion.web.auth")

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
REVOKE_URL = f"{DISCORD_API}/oauth2/token/revoke"
CURRENT_USER_URL = f"{DISCORD_API}/users/@me"

# ``identify`` is enough: guild membership and permissions come from the bot.
OAUTH_SCOPES = "identify"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class OAuthError(RuntimeError):
    """Raised when Discord refuses or fails an OAuth exchange."""


def oauth_configured() -> bool:
    return bool(Config.DISCORD_CLIENT_ID and Config.DISCORD_CLIENT_SECRET)


def build_authorize_url(state: str) -> str:
    """Returns the Discord consent URL for a login attempt."""
    params = {
        "client_id": Config.DISCORD_CLIENT_ID or "",
        "redirect_uri": Config.dashboard_redirect_uri(),
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "none",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(session: aiohttp.ClientSession, code: str) -> dict[str, Any]:
    """Exchanges an authorization code for the authenticated user's profile.

    The access token is used once and then revoked. Failures are raised as
    :class:`OAuthError` with a generic message; the underlying Discord response
    is logged locally and never returned to the browser.
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
                raise OAuthError("Discord rejected the authorization code.")
            token_data = await response.json()
    except aiohttp.ClientError as exc:
        log.warning("OAuth token exchange transport error: %s", exc)
        raise OAuthError("Could not reach Discord to complete the login.") from exc

    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("Discord returned an unusable token response.")

    try:
        profile = await _fetch_current_user(session, access_token)
    finally:
        # Best effort: the token is no longer needed, so do not leave a live
        # credential floating around on Discord's side.
        await _revoke_token(session, access_token)

    return profile


async def _fetch_current_user(
    session: aiohttp.ClientSession, access_token: str
) -> dict[str, Any]:
    try:
        async with session.get(
            CURRENT_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status != 200:
                log.warning(
                    "Could not read the OAuth profile (HTTP %s).", response.status
                )
                raise OAuthError("Discord did not return your profile.")
            profile = await response.json()
    except aiohttp.ClientError as exc:
        log.warning("OAuth profile transport error: %s", exc)
        raise OAuthError("Could not reach Discord to complete the login.") from exc

    user_id = profile.get("id") if isinstance(profile, dict) else None
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise OAuthError("Discord returned an unusable profile.")
    return profile


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


def login_redirect() -> tuple[str, str]:
    """Returns ``(authorize_url, state)`` for a fresh login attempt."""
    state = security.create_state()
    return build_authorize_url(state), state


__all__ = [
    "OAuthError",
    "OAUTH_SCOPES",
    "oauth_configured",
    "build_authorize_url",
    "exchange_code",
    "login_redirect",
]
