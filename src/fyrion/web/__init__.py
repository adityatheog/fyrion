"""
Fyrion web dashboard.

The FastAPI application and the uvicorn wrapper are imported lazily so that
installations which never enable the dashboard do not need FastAPI or uvicorn
installed.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app", "DashboardServer"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from fyrion.web.app import create_app

        return create_app
    if name == "DashboardServer":
        from fyrion.web.server import DashboardServer

        return DashboardServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
