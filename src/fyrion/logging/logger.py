"""
Structured logging configuration.

Handlers are attached to the *root* logger so that Fyrion, discord.py, uvicorn
and any third-party library all end up in the same stream and the same rotating
file. Two formats are supported:

* ``text`` - human readable, for a terminal or ``docker logs``.
* ``json`` - one JSON object per line, for log shippers that parse structured
  events. Extra keyword fields passed as ``logger.info(..., extra={...})`` are
  emitted under ``extra`` rather than being dropped.

The console handler follows ``LOG_LEVEL``; the rotating file handler always
records DEBUG, so a post-mortem has full detail even when the console is quiet.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

from fyrion.config import Config

# Attributes present on every LogRecord; anything else came from ``extra``.
_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_configured = False


class JsonFormatter(logging.Formatter):
    """Renders each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
            "process": record.process,
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extras:
            payload["extra"] = extras

        # default=str keeps non-serializable extras (datetimes, discord objects)
        # from turning a log call into an exception.
        return json.dumps(payload, default=str, ensure_ascii=False)


def _build_formatter() -> logging.Formatter:
    if Config.LOG_FORMAT == "json":
        return JsonFormatter()
    return logging.Formatter(fmt=TEXT_FORMAT, datefmt=DATE_FORMAT)


def _resolve_level(name: str, default: int = logging.INFO) -> int:
    level = logging.getLevelName(name.upper())
    return level if isinstance(level, int) else default


def _file_handler(formatter: logging.Formatter) -> logging.Handler | None:
    """Creates the rotating file handler, or None when the disk is unusable."""
    if not Config.LOG_TO_FILE:
        return None

    try:
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            filename=os.path.join(Config.LOG_DIR, Config.LOG_FILE_NAME),
            maxBytes=Config.LOG_MAX_BYTES,
            backupCount=Config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        # A read-only filesystem must not stop the bot from starting; console
        # logging still works.
        sys.stderr.write(f"WARNING: file logging disabled ({exc}).\n")
        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    return handler


def _tune_third_party_loggers(console_level: int) -> None:
    """Keeps library output useful instead of overwhelming."""
    discord_level = _resolve_level(Config.DISCORD_LOG_LEVEL)
    logging.getLogger("discord").setLevel(discord_level)
    # These two are extremely chatty at INFO and add little operational value.
    logging.getLogger("discord.http").setLevel(max(discord_level, logging.WARNING))
    logging.getLogger("discord.gateway").setLevel(max(discord_level, logging.WARNING))
    logging.getLogger("discord.client").setLevel(discord_level)

    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)

    # uvicorn installs its own handlers when given a log config; Fyrion passes
    # log_config=None, so clear anything left over and let records propagate to
    # the root handlers configured here.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        library_logger = logging.getLogger(name)
        library_logger.handlers.clear()
        library_logger.propagate = True

    logging.getLogger("uvicorn.error").setLevel(console_level)
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if Config.DASHBOARD_ACCESS_LOG else logging.WARNING
    )


def setup_logging(*, force: bool = False) -> logging.Logger:
    """Configures process-wide logging and returns the ``fyrion`` logger.

    Idempotent: calling it twice does not duplicate handlers. Pass
    ``force=True`` to rebuild the handlers (useful in tests).
    """
    global _configured

    root = logging.getLogger()
    if _configured and not force:
        return logging.getLogger("fyrion")

    console_level = _resolve_level(Config.LOG_LEVEL)
    formatter = _build_formatter()

    if force or _configured:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - best effort teardown
                pass

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = _file_handler(formatter)
    if file_handler is not None:
        root.addHandler(file_handler)

    # The root level must be the most verbose of all handlers, otherwise the
    # file handler never sees DEBUG records.
    root.setLevel(logging.DEBUG if file_handler is not None else console_level)

    logging.getLogger("fyrion").setLevel(logging.DEBUG)
    _tune_third_party_loggers(console_level)

    # Route warnings.warn() through logging instead of stderr.
    logging.captureWarnings(True)

    _configured = True
    return logging.getLogger("fyrion")


__all__ = ["setup_logging", "JsonFormatter", "TEXT_FORMAT", "DATE_FORMAT"]
