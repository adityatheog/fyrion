# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Fyrion container image.
#
# Two stages: the builder installs dependencies into a virtual environment, and
# the runtime copies only that environment plus the installed package. The
# resulting image carries no compiler toolchain and no pip cache.
#
# Fyrion is primarily an outbound gateway client. It opens no listening port
# unless DASHBOARD_ENABLED=true, in which case it binds DASHBOARD_PORT.
# ---------------------------------------------------------------------------

# --------------------------------------------------------------------- build
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build

RUN python -m venv "$VIRTUAL_ENV"

# Dependencies first, so editing source does not invalidate this layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install the package itself. --no-deps because requirements.txt already
# resolved and pinned the whole tree above.
COPY pyproject.toml README.md ./
COPY src ./src
COPY dashboard ./dashboard
RUN pip install --no-cache-dir --no-deps .

# ------------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Fyrion" \
      org.opencontainers.image.description="Powerful tools for better Discord communities." \
      org.opencontainers.image.source="https://github.com/adityatheog/fyrion" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Unprivileged runtime user. /data holds the SQLite database, /app/logs the
# rotating log files; both are mounted as named volumes by docker-compose.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin fyrion \
 && mkdir -p /data /app/logs \
 && chown -R fyrion:fyrion /data /app

COPY --from=builder --chown=fyrion:fyrion /opt/venv /opt/venv

# The dashboard serves Jinja2 templates and static assets from disk, so the
# package directory is copied even though it is also installed in the venv.
COPY --chown=fyrion:fyrion dashboard ./dashboard

USER fyrion

ENV DATABASE_URL=/data/fyrion.db \
    LOG_DIR=/app/logs \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO \
    ENVIRONMENT=production

# Liveness probe: open the SQLite database read-only and run a trivial query.
# This proves the /data volume is mounted and the database the bot writes to is
# reachable and not corrupt. `python -c` is used rather than curl so the image
# needs no extra packages. (The bot creates the database during startup; the
# start-period covers that window.) A read-only URI avoids the probe itself
# creating a stray empty database file.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD ["python", "-c", "import os, sqlite3; sqlite3.connect('file:' + os.environ.get('DATABASE_URL', '/data/fyrion.db') + '?mode=ro', uri=True).execute('SELECT 1')"]

# DISCORD_TOKEN must be supplied at runtime (env_file, secret store or
# `docker run -e`). It is deliberately never baked into the image.
STOPSIGNAL SIGTERM

CMD ["python", "-m", "fyrion"]
