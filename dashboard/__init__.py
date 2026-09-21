"""
Fyrion web dashboard package.

The dashboard is a FastAPI application that renders a small Jinja2 UI and an
authenticated JSON API on top of Fyrion's SQLite storage and (when available)
the live gateway client.

The project uses a ``src`` layout. When the repository is used directly from a
clone, ``src`` is not on ``sys.path``; the bootstrap below makes ``fyrion``
importable in that situation so ``python -m dashboard`` works from a fresh
checkout. When Fyrion is pip-installed the already importable copy wins, because
the path is only added when the local ``src`` directory actually exists.

The application factory is imported lazily so that merely importing this package
does not require FastAPI, uvicorn or Jinja2 to be installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

__all__ = ["create_app"]


def _bootstrap_source_layout() -> None:
    src = Path(__file__).resolve().parent.parent / "src"
    if src.is_dir():
        candidate = str(src)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


_bootstrap_source_layout()


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from dashboard.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
