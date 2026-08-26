"""
Cryptographic helpers for the dashboard session layer.

Design rules:

* The database never stores a usable credential. Session tokens are kept as a
  keyed HMAC-SHA256 digest, so a database leak cannot be replayed as a login.
  The same applies to client IPs, which are stored as a keyed hash for abuse
  detection rather than in clear.
* OAuth ``state`` is signed *and* mirrored in an HttpOnly cookie, so the
  callback validates both the signature (integrity, expiry) and the double
  submit (CSRF).
* Every comparison of secret material uses :func:`hmac.compare_digest`.

This module deliberately imports nothing from FastAPI: it is pure stdlib so it
can be unit tested without the web extras installed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

from fyrion.config import Config

log = logging.getLogger("fyrion.web.security")

SESSION_COOKIE = "fyrion_session"
STATE_COOKIE = "fyrion_oauth_state"

# OAuth round trips are short; a long-lived state is just a wider CSRF window.
STATE_TTL_SECONDS = 600
_TOKEN_BYTES = 48

_runtime_key: bytes | None = None


def secret_key() -> bytes:
    """Returns the HMAC key derived from ``DASHBOARD_SECRET_KEY``.

    When no key is configured a random one is generated for the lifetime of the
    process. That keeps development usable while making it obvious that
    sessions will not survive a restart; :meth:`Config.validate` refuses to
    start the dashboard without a key, so this path is only reached by tests
    and direct library use.
    """
    global _runtime_key

    configured = Config.DASHBOARD_SECRET_KEY
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()

    if _runtime_key is None:
        _runtime_key = secrets.token_bytes(32)
        log.warning(
            "DASHBOARD_SECRET_KEY is not set; using an ephemeral key. All "
            "sessions will be invalidated when this process restarts."
        )
    return _runtime_key


def _key(key: bytes | None) -> bytes:
    return key if key is not None else secret_key()


def generate_token() -> str:
    """Returns a fresh, high-entropy session token (never stored as-is)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str, *, key: bytes | None = None) -> str:
    """Returns the keyed digest of a token, for storage and lookup."""
    return hmac.new(_key(key), token.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_ip(ip: str | None, *, key: bytes | None = None) -> str | None:
    """Returns a keyed digest of a client IP, or None when it is unknown."""
    if not ip:
        return None
    return hmac.new(_key(key), ip.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# OAuth state
# ---------------------------------------------------------------------------


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload: bytes, key: bytes | None = None) -> str:
    return _b64encode(hmac.new(_key(key), payload, hashlib.sha256).digest())


def create_state(
    *, ttl: int = STATE_TTL_SECONDS, key: bytes | None = None
) -> str:
    """Returns a signed, expiring OAuth ``state`` value."""
    payload = json.dumps(
        {"n": secrets.token_urlsafe(16), "e": int(time.time()) + int(ttl)},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = _b64encode(payload)
    return f"{encoded}.{_sign(encoded.encode('ascii'), key)}"


def verify_state(
    state: str | None, cookie_state: str | None, *, key: bytes | None = None
) -> bool:
    """Validates the double submit, the signature and the expiry."""
    if not state or not cookie_state:
        return False
    if not hmac.compare_digest(state, cookie_state):
        return False

    encoded, _, signature = state.partition(".")
    if not encoded or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(encoded.encode("ascii"), key)):
        return False

    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return False

    expiry = payload.get("e") if isinstance(payload, dict) else None
    if not isinstance(expiry, (int, float)):
        return False
    return time.time() < float(expiry)


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def _cookie_kwargs(max_age: int) -> dict[str, Any]:
    return {
        "max_age": max_age,
        "expires": max_age,
        "path": "/",
        "domain": Config.DASHBOARD_COOKIE_DOMAIN,
        "httponly": True,  # never readable from JavaScript
        "secure": Config.DASHBOARD_COOKIE_SECURE,
        # Lax (not Strict) so the browser still sends the cookie on the
        # top-level redirect back from Discord.
        "samesite": "lax",
    }


def set_session_cookie(response: Any, token: str, max_age: int) -> None:
    response.set_cookie(SESSION_COOKIE, token, **_cookie_kwargs(max_age))


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", domain=Config.DASHBOARD_COOKIE_DOMAIN
    )


def set_state_cookie(response: Any, state: str) -> None:
    response.set_cookie(STATE_COOKIE, state, **_cookie_kwargs(STATE_TTL_SECONDS))


def clear_state_cookie(response: Any) -> None:
    response.delete_cookie(
        STATE_COOKIE, path="/", domain=Config.DASHBOARD_COOKIE_DOMAIN
    )


__all__ = [
    "SESSION_COOKIE",
    "STATE_COOKIE",
    "STATE_TTL_SECONDS",
    "secret_key",
    "generate_token",
    "hash_token",
    "hash_ip",
    "create_state",
    "verify_state",
    "set_session_cookie",
    "clear_session_cookie",
    "set_state_cookie",
    "clear_state_cookie",
]
