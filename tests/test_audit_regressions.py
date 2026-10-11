"""Regression tests for defects found and repaired during the audit.

Each test here pins a specific previously-wrong behaviour, so a future change
that reintroduces it fails loudly:

* ``fyrion.web.app._serialize_ids`` used to leave snowflake columns that do not
  end in ``_id`` (``resolved_by``, ``claimed_by``, ``closed_by``,
  ``created_by``, ``added_by``) as JSON numbers, which JavaScript rounds above
  2**53-1.
* ``WarningsRepository.clear_warnings`` used to count rows and then delete them,
  so its return value could disagree with what it actually removed; its sibling
  ``delete_warning`` looked the row up before deleting it.
* ``.env.example`` documented an OAuth callback path the application did not
  serve, so the documented Docker deployment redirected to a 404.
* The 500 produced for an unhandled exception used to be built by an ``Exception``
  handler, which runs outside all user middleware and so shipped without the
  security headers; the test asserts a 500 still carries them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fyrion.database.repositories.warnings import WarningsRepository
from fyrion.web.app import _is_snowflake_column, _serialize_ids

# Comfortably above 2**53-1, where IEEE-754 doubles stop representing integers
# exactly. Any real Discord snowflake is in this range.
SNOWFLAKE = 1234567890123456789


def test_is_snowflake_column_recognises_actor_columns():
    """The ``_id`` suffix is not the only marker of a snowflake column."""
    for key in (
        "user_id",
        "mod_log_channel_id",
        "resolved_by",
        "claimed_by",
        "closed_by",
        "created_by",
        "added_by",
    ):
        assert _is_snowflake_column(key), key


def test_is_snowflake_column_ignores_counters_and_text():
    for key in ("case_number", "duration_seconds", "uses", "reason", "status"):
        assert not _is_snowflake_column(key), key


def test_serialize_ids_stringifies_snowflakes_and_keeps_counters():
    row = {
        "case_id": SNOWFLAKE,
        "guild_id": SNOWFLAKE,
        "target_id": SNOWFLAKE,
        "resolved_by": SNOWFLAKE,
        "claimed_by": SNOWFLAKE,
        "closed_by": SNOWFLAKE,
        "created_by": SNOWFLAKE,
        "added_by": SNOWFLAKE,
        "case_number": 7,
        "duration_seconds": 3600,
        "uses": 0,
        "reason": "spam",
        "optional_role_id": None,
    }

    result = _serialize_ids(row)

    for key in (
        "case_id",
        "guild_id",
        "target_id",
        "resolved_by",
        "claimed_by",
        "closed_by",
        "created_by",
        "added_by",
    ):
        assert result[key] == str(SNOWFLAKE), key
        assert isinstance(result[key], str), key

    # Plain counters and text must survive untouched, or the dashboard would
    # compare stringified numbers.
    assert result["case_number"] == 7
    assert result["duration_seconds"] == 3600
    assert result["uses"] == 0
    assert result["reason"] == "spam"
    assert result["optional_role_id"] is None

    # The caller's row is not mutated in place.
    assert row["case_id"] == SNOWFLAKE


@pytest.mark.asyncio
async def test_clear_warnings_reports_the_rows_it_removed(db_manager):
    repository = WarningsRepository(db_manager)
    await repository.add_warning(100, 1, 999, "first")
    await repository.add_warning(100, 1, 999, "second")
    await repository.add_warning(100, 2, 999, "another member")
    await repository.add_warning(200, 1, 999, "another guild")

    assert await repository.count_warnings(100, 1) == 2
    assert await repository.clear_warnings(100, 1) == 2
    assert await repository.count_warnings(100, 1) == 0

    # A second clear removes nothing, so it must report zero rather than the
    # number of rows that used to be there.
    assert await repository.clear_warnings(100, 1) == 0

    # Unrelated members and guilds are untouched by the shared predicate.
    assert await repository.count_warnings(100, 2) == 1
    assert await repository.count_warnings(200, 1) == 1


@pytest.mark.asyncio
async def test_delete_warning_is_scoped_to_the_guild(db_manager):
    repository = WarningsRepository(db_manager)
    await repository.add_warning(300, 5, 999, "keep me")

    rows = await repository.get_warnings(300, 5)
    assert len(rows) == 1
    warning_id = int(rows[0]["id"])

    # A different guild must not be able to reach this row.
    assert await repository.delete_warning(301, warning_id) is False
    assert await repository.count_warnings(300, 5) == 1

    assert await repository.delete_warning(300, warning_id) is True
    assert await repository.count_warnings(300, 5) == 0

    # Deleting it again must report failure rather than claim success.
    assert await repository.delete_warning(300, warning_id) is False


def test_env_example_documents_the_default_callback_path():
    """The shipped example must agree with the application's own default.

    They drifted once already: ``.env.example`` said ``/callback`` while the
    code defaulted to ``/auth/callback``, so an operator following the example
    registered a redirect URI that the app never served.
    """
    example = Path(__file__).resolve().parents[1] / ".env.example"
    documented = [
        line.split("=", 1)[1].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("DASHBOARD_OAUTH_CALLBACK_PATH=")
    ]

    assert documented == ["/auth/callback"]


def test_unhandled_error_still_carries_the_security_headers():
    """A 500 from an unhandled exception must not bypass response hardening.

    Regression guard for the ``Exception`` handler that used to be registered on
    the application. Such a handler runs on Starlette's outermost
    ``ServerErrorMiddleware``, *outside* every user middleware, so its 500 was
    the one response that shipped without the security headers. The 500 is now
    built inside the ``_harden`` middleware instead, which wraps everything.

    Two details keep this honest:

    * ``raise_server_exceptions=False`` makes the test client return the 500 the
      middleware produced rather than re-raising the original error.
    * ``base_url="http://localhost"`` satisfies ``TrustedHostMiddleware``, whose
      allow-list is derived from the configured base URL and does not contain
      the client's default ``testserver`` host.
    """
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from dashboard.app import create_app

    app = create_app()

    @app.get("/__boom")
    async def _boom() -> None:  # pragma: no cover - only raises
        raise RuntimeError("deliberate failure for the hardening test")

    base_url = "http://localhost"
    client = TestClient(app, raise_server_exceptions=False, base_url=base_url)
    response = client.get("/__boom")

    assert response.status_code == 500
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"

    body = response.json()
    assert body["error"] == "An internal error occurred."
    assert body["reference"]


def test_shipped_documentation_does_not_advertise_a_dead_callback_path():
    """No operator-facing document may point at a callback route the app lacks.

    This drifted twice: ``.env.example`` and then ``README.md`` both told
    operators to register ``/callback`` while the application serves
    ``/auth/callback``, so a deployment following the docs was redirected to a
    404. ``.env.example`` is guarded by its own test; this one covers the README
    and re-checks the example.
    """
    root = Path(__file__).resolve().parents[1]

    for name in ("README.md", ".env.example"):
        text = (root / name).read_text(encoding="utf-8")
        assert "/auth/callback" in text, name
        assert "8080/callback" not in text, name
        assert "DASHBOARD_OAUTH_CALLBACK_PATH=/callback" not in text, name
