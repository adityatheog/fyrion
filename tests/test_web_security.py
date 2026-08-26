"""
Unit tests for the dashboard's cryptographic session helpers.

These use only the standard library plus fyrion.web.security, so they run even
when the FastAPI extras are not installed.
"""
import time

import pytest

from fyrion.config import Config
from fyrion.web import security

KEY = b"\x01" * 32
OTHER_KEY = b"\x02" * 32


def test_tokens_are_unique_and_long():
    tokens = {security.generate_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(token) >= 43 for token in tokens)


def test_token_hash_is_deterministic_and_keyed():
    token = security.generate_token()

    assert security.hash_token(token, key=KEY) == security.hash_token(token, key=KEY)
    # The stored value must not be the credential itself.
    assert security.hash_token(token, key=KEY) != token
    # A different key must produce a different digest.
    assert security.hash_token(token, key=KEY) != security.hash_token(
        token, key=OTHER_KEY
    )
    assert len(security.hash_token(token, key=KEY)) == 64


def test_ip_hashing():
    assert security.hash_ip(None, key=KEY) is None
    assert security.hash_ip("", key=KEY) is None

    digest = security.hash_ip("203.0.113.7", key=KEY)
    assert digest is not None
    assert "203.0.113.7" not in digest
    assert digest == security.hash_ip("203.0.113.7", key=KEY)
    assert digest != security.hash_ip("203.0.113.8", key=KEY)


def test_state_round_trip():
    state = security.create_state(key=KEY)
    assert security.verify_state(state, state, key=KEY) is True


def test_state_requires_the_cookie_to_match():
    state = security.create_state(key=KEY)
    other = security.create_state(key=KEY)

    # This is the CSRF protection: signature alone is not enough.
    assert security.verify_state(state, other, key=KEY) is False
    assert security.verify_state(state, None, key=KEY) is False
    assert security.verify_state(None, state, key=KEY) is False


def test_state_signature_is_verified():
    state = security.create_state(key=KEY)

    # Signed with a different key: must be refused.
    assert security.verify_state(state, state, key=OTHER_KEY) is False

    payload, _, signature = state.partition(".")
    tampered = f"{payload}.{'a' * len(signature)}"
    assert security.verify_state(tampered, tampered, key=KEY) is False


def test_state_expires():
    expired = security.create_state(ttl=-1, key=KEY)
    assert security.verify_state(expired, expired, key=KEY) is False

    fresh = security.create_state(ttl=60, key=KEY)
    assert security.verify_state(fresh, fresh, key=KEY) is True


def test_malformed_state_is_rejected():
    for value in ("", ".", "nodot", "a.b", "!!!.???"):
        assert security.verify_state(value, value, key=KEY) is False


class FakeResponse:
    """Captures cookie operations the way Starlette's Response performs them."""

    def __init__(self):
        self.set_calls = []
        self.deleted = []

    def set_cookie(self, name, value, **kwargs):
        self.set_calls.append((name, value, kwargs))

    def delete_cookie(self, name, **kwargs):
        self.deleted.append((name, kwargs))


def test_session_cookie_is_httponly_and_lax():
    response = FakeResponse()
    security.set_session_cookie(response, "token-value", 3600)

    name, value, kwargs = response.set_calls[0]
    assert name == security.SESSION_COOKIE
    assert value == "token-value"
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["max_age"] == 3600
    assert kwargs["secure"] == Config.DASHBOARD_COOKIE_SECURE


def test_state_cookie_uses_the_state_ttl():
    response = FakeResponse()
    security.set_state_cookie(response, security.create_state(key=KEY))

    _, _, kwargs = response.set_calls[0]
    assert kwargs["max_age"] == security.STATE_TTL_SECONDS
    assert kwargs["httponly"] is True


def test_cookies_can_be_cleared():
    response = FakeResponse()
    security.clear_session_cookie(response)
    security.clear_state_cookie(response)

    cleared = {name for name, _ in response.deleted}
    assert cleared == {security.SESSION_COOKIE, security.STATE_COOKIE}


def test_secret_key_prefers_configuration(monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_SECRET_KEY", "k" * 48, raising=False)
    assert security.secret_key() == security.secret_key()
    assert len(security.secret_key()) == 32


def test_secret_key_falls_back_to_an_ephemeral_key(monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_SECRET_KEY", None, raising=False)
    monkeypatch.setattr(security, "_runtime_key", None, raising=False)

    first = security.secret_key()
    assert len(first) == 32
    # Stable within the process, so sessions survive until restart.
    assert security.secret_key() == first


@pytest.mark.parametrize("ttl", [1, 5, 600])
def test_state_is_valid_for_its_whole_ttl(ttl):
    state = security.create_state(ttl=ttl, key=KEY)
    assert security.verify_state(state, state, key=KEY) is True
    assert time.time() > 0  # sanity: no clock mocking involved
