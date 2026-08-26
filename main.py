"""
Fyrion process entry point.

Running the bot means running two long-lived components in one event loop:

1. the sharded Discord gateway client (``fyrion.bot.Fyrion``, an
   ``AutoShardedBot`` with explicit intents), and
2. the optional FastAPI web dashboard served by uvicorn.

Both are started as tasks and awaited with a single ``asyncio.gather`` in
``fyrion.runtime.supervise``, so a failure in either one tears the whole process
down cleanly instead of leaving a half-dead bot behind.

The supervisor itself lives in :mod:`fyrion.runtime` rather than in this file so
that ``python main.py`` (source checkout) and ``python -m fyrion`` (installed
package) execute exactly the same code path. Duplicating the startup sequence in
two places is how boot-order bugs are born.

Usage:
    python main.py            # source checkout, no install required
    python -m fyrion          # installed package, identical behaviour
"""
from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_source_layout() -> None:
    """Makes ``src/fyrion`` importable when Fyrion has not been pip-installed.

    The project uses a src layout. When the repository is cloned and run
    directly, ``src`` is not on ``sys.path``; prepending it here means a fresh
    clone runs without ``pip install -e .`` first. When the package *is*
    installed, the already-importable copy wins because we only add the path if
    the local one exists.
    """
    src = Path(__file__).resolve().parent / "src"
    if src.is_dir():
        candidate = str(src)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


def main() -> int:
    """Validates configuration, configures logging and supervises the process."""
    _bootstrap_source_layout()

    # Imported after the path bootstrap so a source checkout works unmodified.
    from fyrion.runtime import main as run_fyrion

    return run_fyrion()


if __name__ == "__main__":
    raise SystemExit(main())
