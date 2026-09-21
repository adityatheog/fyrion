"""
``python -m dashboard``.

Runs the dashboard as its own process, without the gateway client. In that mode
Fyrion's live guild cache is unavailable, so channel and role pickers fall back
to the snapshot Discord returns for the signed-in user and stored settings are
read and written directly through the SQLite pool.

To run the dashboard alongside the bot in a single event loop (which is what
enables the live cache), use ``python -m fyrion`` with ``DASHBOARD_ENABLED=true``
instead.
"""

from __future__ import annotations

import logging
import sys


def main() -> int:
    from fyrion.config import Config, ConfigurationError
    from fyrion.logging.logger import setup_logging

    # This process exists to serve the dashboard, so the dashboard is enabled
    # regardless of the environment variable. Forcing it true makes
    # Config.validate() apply the dashboard-specific rules — a real signing key,
    # https + Secure cookies in production, and non-wildcard origins/hosts —
    # which it otherwise skips, leaving the API on an ephemeral key.
    Config.DASHBOARD_ENABLED = True  # type: ignore[misc]

    try:
        # The dashboard needs OAuth credentials and a signing key; failing here
        # is far better than serving an API that cannot authenticate anyone.
        Config.validate()
    except ConfigurationError as exc:
        sys.stderr.write(f"CONFIGURATION ERROR:\n{exc}\n")
        return 2

    setup_logging()
    log = logging.getLogger("dashboard")

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "uvicorn is not installed. Install the web dependencies with: "
            "pip install -r requirements.txt\n"
        )
        return 1

    from dashboard.app import create_app

    log.info(
        "Starting the standalone dashboard on %s:%s (redirect URI: %s)",
        Config.DASHBOARD_HOST,
        Config.DASHBOARD_PORT,
        f"{Config.DASHBOARD_BASE_URL}{Config.DASHBOARD_OAUTH_CALLBACK_PATH}",
    )

    uvicorn.run(
        create_app(),
        host=Config.DASHBOARD_HOST,
        port=Config.DASHBOARD_PORT,
        log_config=None,
        access_log=Config.DASHBOARD_ACCESS_LOG,
        server_header=False,
        proxy_headers=Config.DASHBOARD_TRUST_PROXY,
        forwarded_allow_ips=(
            Config.DASHBOARD_FORWARDED_ALLOW_IPS
            if Config.DASHBOARD_TRUST_PROXY
            else None
        ),
        timeout_graceful_shutdown=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
