"""
``python -m fyrion``.

Thin wrapper around :func:`fyrion.runtime.main`, which is the single
implementation of the startup and shutdown sequence.
"""
from __future__ import annotations

from fyrion.runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
