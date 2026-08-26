<div align="center">

# Fyrion

**Powerful tools for better Discord communities.**

[![CI](https://github.com/adityatheog/fyrion/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/adityatheog/fyrion/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776ab?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![discord.py](https://img.shields.io/badge/discord.py-2.4.0-5865F2?logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-2a6db2.svg)](https://mypy-lang.org/)
[![Docker ready](https://img.shields.io/badge/docker-ready-2496ed?logo=docker&logoColor=white)](Dockerfile)
[![Self-hostable](https://img.shields.io/badge/self--hostable-yes-brightgreen.svg)](#installation--self-hosting)

[Features](#features) &middot;
[Commands](#command-reference) &middot;
[Architecture](#architecture) &middot;
[Setup](#installation--self-hosting) &middot;
[Dashboard](#web-dashboard) &middot;
[Roadmap](#roadmap) &middot;
[Security](SECURITY.md)

</div>

---

Fyrion is a modern, secure, multi-guild Discord bot built with Python and
[discord.py](https://github.com/Rapptz/discord.py). It is designed from the
ground up for safe self-hosting: strict permission boundaries, hard data
isolation between servers, parameterised SQL everywhere, and an optional web
dashboard that refuses to start without an authentication provider.

Everything in the feature list below is implemented and shipped. There is no
"coming soon" section.

---

## Features

| Module | What it does |
| --- | --- |
| **Moderation** | Kick, ban (including by ID), unban, timeout, warn, warning history, purge, slowmode, channel lock. Discord's role hierarchy is enforced server-side on every action, for both the invoker and the bot. |
| **AutoMod** | Seven filters &mdash; spam (per-member token bucket with escalating strikes), invites, links, blocked words, mention floods, line floods and caps. Per-rule and guild-wide exemptions for users, roles and channels. |
| **Bulk deletion** | Eight purge commands: everything, one member, bots and webhooks, links and invites, attachments and images, text matches, embeds and stickers, and safe regular expressions. |
| **Tickets** | Persistent button panels with up to five topics, private per-member channels, staff claiming, participant add/remove, and plain-text transcripts archived on close. |
| **Leveling** | XP per message with cooldowns, a quadratic level curve, reward roles (stacking or ladder), reputation with a 12-hour cooldown, and rank and leaderboard commands. |
| **Giveaways** | Live countdown embeds, one entry per member enforced by a database constraint, role and level requirements, weighted draws, and rerolls that never reselect a previous winner. |
| **Reaction roles** | Toggle, add-only, remove-only and mutually exclusive group modes, with permission and hierarchy validation on every assignment. |
| **Server administration** | Prefix, log channels, welcome and leave messages, autorole, mute role with channel-overwrite sync, role and channel creation and deletion behind confirmation prompts, and an interactive embed builder. |
| **Invite tracking** | Delta-based in-memory invite cache tracking joins, leaves and net invites per inviter. |
| **Audit logging** | Message edits and deletions, bulk deletions, member joins and leaves, nickname and role changes, timeouts, bans, and channel and role lifecycle events. |
| **Utility** | AFK status with mention notices, ping with database latency, uptime, bot info, user info, server info, avatar, banner, role info, a sandboxed calculator and reaction polls. |
| **Fun** | 8-ball, coin flips, dice notation, quotes, memes and Urban Dictionary &mdash; all with bounded requests, host validation and NSFW gating. |
| **Web dashboard** | Discord OAuth2 sign-in, a server picker and a settings editor, plus a JSON API. Optional and disabled by default. |

### Security posture

These are properties of the implementation, not aspirations:

- **Permissions are never trusted from the client.** `default_permissions` only
  decides whether Discord *shows* a command; every command re-checks the
  invoker's effective permissions server-side.
- **Role hierarchy is enforced twice** &mdash; the invoker must outrank the target,
  and so must the bot. Roles granting `Administrator` can only be handled by an
  administrator, so Fyrion cannot be used as a privilege-escalation path.
- **All SQL is parameterised.** Table, column and sort identifiers are validated
  against a schema allow-list before a statement is built, because identifiers
  cannot be bound as parameters.
- **Guild data is isolated.** Every query is scoped by `guild_id`, and
  guild-scoped tables cascade from `guild_settings` so removing a guild leaves
  no orphaned rows.
- **Attacker-controlled text is inert.** Message content, nicknames, role names
  and API payloads are escaped, length-bounded and posted with mention parsing
  disabled. The only exceptions are messages that address exactly one member.
- **No stored Discord user tokens.** The dashboard exchanges the OAuth code
  once, reads the profile, then revokes the access token. Session tokens are
  stored as keyed HMAC digests, so a database leak cannot be replayed.
- **Operator regexes are refused, not risked.** Word filters are `re.escape`-d,
  and `/purge-match` rejects nested quantifiers, because Python's `re` has no
  match timeout.

---

## Command reference

### Moderation &mdash; `/mod`

| Command | Description | Required permission |
| --- | --- | --- |
| `/mod kick` | Kick a member. | Kick Members |
| `/mod ban` | Ban a user, including by ID if they already left. | Ban Members |
| `/mod unban` | Lift a ban. | Ban Members |
| `/mod timeout` | Time a member out for up to 28 days. | Moderate Members |
| `/mod untimeout` | Remove an active timeout. | Moderate Members |
| `/mod warn` | Issue a formal warning and DM the member. | Moderate Members |
| `/mod warnings` | List a member's warnings. | Moderate Members |
| `/mod delwarn` | Delete one warning by ID. | Moderate Members |
| `/mod clearwarns` | Clear every warning for a member. | Moderate Members |
| `/mod purge` | Bulk delete recent messages, optionally from one member. | Manage Messages |
| `/mod slowmode` | Set the channel slowmode delay. | Manage Channels |
| `/mod lock` / `/mod unlock` | Stop or restore `@everyone` posting. | Manage Channels |

### Bulk deletion

| Command | Description |
| --- | --- |
| `/purge` | Delete the most recent messages in a channel. |
| `/purge-user` | Delete messages from one member. |
| `/purge-bot` | Delete bot and, optionally, webhook messages. |
| `/purge-links` | Delete messages containing links or only invites. |
| `/purge-attachments` | Delete messages with files, or only with images. |
| `/purge-contains` | Delete messages containing a piece of text. |
| `/purge-embeds` | Delete messages with embeds or stickers. |
| `/purge-match` | Delete messages matching a safe regular expression. |

All purge commands require `Manage Messages` **and** `Read Message History` in
the target channel, skip pinned messages unless you opt in, bound how far back
they scan, and mirror the result to the moderation log.

### AutoMod

| Command | Description |
| --- | --- |
| `/automod-enable` | Arm or disarm the whole engine. |
| `/automod-status` | Show every filter, its settings and the whitelist. |
| `/automod-antispam` | Rate limit messages with a token bucket and strikes. |
| `/automod-antiinvite` | Block Discord invite links. |
| `/automod-antilink` | Block any URL. |
| `/automod-anticaps` | Remove messages that are mostly uppercase. |
| `/automod-antimention` | Limit mentions per message. |
| `/automod-antilines` | Limit lines per message. |
| `/automod-wordfilter add\|remove\|list` | Manage the blocked word list. |
| `/automod-whitelist-role` | Exempt a role from every filter. |
| `/automod-whitelist-channel` | Exempt a channel from every filter. |

Each filter chooses its own action: `log`, `delete`, `warn`, `timeout`, `kick`
or `ban`. The server owner and anyone with `Manage Messages` are always exempt.

### Tickets

| Command | Description |
| --- | --- |
| `/ticket-setup` | Configure the system and post the panel. |
| `/ticket-close` | Close the ticket, export a transcript, delete the channel. |
| `/ticket-claim` | Claim or release a ticket as staff. |
| `/ticket-add` / `/ticket-remove` | Grant or revoke one member's access. |
| `/ticket-list` | List the currently open tickets. |

### Leveling and reputation

| Command | Description |
| --- | --- |
| `/rank` | Show level, XP progress, rank and reputation. |
| `/leaderboard-levels` | Top members by experience. |
| `/rep` | Give a reputation point (12-hour cooldown). |
| `/level-toggle` | Enable leveling and tune XP, cooldown and stacking. |
| `/set-levelchannel` | Choose where level-ups are announced. |
| `/level-rewards add\|remove\|list` | Manage reward roles per level. |

### Giveaways

| Command | Description |
| --- | --- |
| `/giveaway-start` | Start a giveaway with a live countdown. |
| `/giveaway-end` | End a giveaway now and draw winners. |
| `/giveaway-reroll` | Draw replacements, excluding previous winners. |
| `/giveaway-list` | List giveaways and entry counts. |

### Administration

| Command | Description |
| --- | --- |
| `/set-prefix` | Prefix used by custom commands. |
| `/set-logchannel` | Audit log destination. |
| `/set-modlog` | Moderation and AutoMod log destination. |
| `/set-welcome` / `/set-leave` | Join and leave channels and templates. |
| `/set-autorole` | Role granted automatically on join. |
| `/set-muterole` | Mute role, optionally syncing channel denials. |
| `/role-add` / `/role-remove` | Grant or remove a role. |
| `/role-all` | Bulk add or remove a role across the server. |
| `/role-create` / `/role-delete` | Create a permissionless role, or delete one. |
| `/channel-create` / `/channel-delete` | Create or delete a channel. |
| `/embed-builder` | Build and preview a rich embed before posting. |
| `/reactionrole-add` / `/reactionrole-remove` | Manage reaction role mappings. |
| `/config welcome` / `/config autorole` | Legacy equivalents, still supported. |
| `/logs channel` / `/logs disable` / `/logs status` | Audit log configuration. |

Destructive administration commands require a button confirmation and are
mirrored to the moderation log.

### Utility

`/help` &middot; `/ping` &middot; `/uptime` &middot; `/botinfo` &middot;
`/userinfo` &middot; `/serverinfo` &middot; `/avatar` &middot; `/banner` &middot;
`/roleinfo` &middot; `/calculator` &middot; `/poll` &middot; `/afk` &middot;
`/invites`

### Fun

`/8ball` &middot; `/coinflip` &middot; `/roll` &middot; `/quote` &middot;
`/meme` &middot; `/urban`

---

## Architecture

One process, one event loop. The sharded gateway client and the optional HTTP
dashboard are started as tasks and awaited together, so a failure in either one
tears the process down cleanly instead of leaving a half-dead bot behind.

```
                        ┌─────────────────────────┐
                        │    Discord Gateway     │
                        │  + REST API + OAuth2   │
                        └───────────┬────────────┘
                                    │ websocket + https
╔══════════════════════════════╗═════════════════════════════╗
║                     fyrion.runtime  —  supervisor              ║
║  validate config → configure logging → gather(bot, dashboard)  ║
║  SIGINT / SIGTERM → stop_event → drain HTTP → close gateway    ║
╚══════════╦═════════════════════════════════╦══════════════╝
           │                                        │
 ┌─────────▼───────────┐            ┌────────────▼────────────┐
 │   fyrion.bot        │            │  dashboard (FastAPI)     │
 │   AutoShardedBot    │            │  uvicorn, same loop      │
 │                     │            │                          │
 │ explicit intents    │            │  /            landing    │
 │ persistent views    │            │  /login /callback OAuth2 │
 │ cog auto-discovery  │            │  /dashboard   picker     │
 │ command tree sync   │            │  /manage/{id} settings   │
 └─────────┬───────────┘            │  /api/*       JSON       │
           │                        └────────────┬────────────┘
           │  loads                              │ session cookie
 ┌─────────▼──────────────────────────┐          │ + CSRF header
 │                 cogs                        │          │
 │                                             │   ┌──────▼──────┐
 │ admin      automod    error_handler  fun    │   │ authorize   │
 │ giveaways  help       invites        level  │   │ Manage      │
 │ logging    moderation purge          tickets│   │ Server on   │
 │ utility    welcome                          │   │ that guild  │
 └─────────┬──────────────────────────┘   └─────────────┘
           │  via
 ┌─────────▼──────────────────────────┐
 │   utils/            views/           │
 │   permissions       tickets          │  hierarchy checks,
 │   modlog            confirm          │  persistent buttons,
 │   patterns          embeds           │  confirmation prompts
 │   tickets  colors  transcripts       │
 └─────────┬──────────────────────────┘
           │  repositories/ (guild_config, warnings, automod_rules,
           │                 tickets, invites, leveling, security)
 ┌─────────▼──────────────────────────────────────────────┐
 │            fyrion.database.manager.DatabasePool             │
 │                                                            │
 │  bounded aiosqlite pool   │  WAL journal, busy_timeout      │
 │  asyncio write lock       │  foreign_keys = ON             │
 │  identifier allow-list    │  incremental auto-vacuum       │
 │  parameter binding only   │  periodic maintenance task     │
 └────────────────────────────┬────────────────────────────┘
                             │
                    ┌───────▼────────┐
                    │  SQLite file    │  guild_settings (parent)
                    │  fyrion.db      │  moderation_cases
                    │  /data in Docker│  automod_rules, tickets
                    │                 │  leveling_profiles
                    │  ON DELETE      │  economy_accounts
                    │  CASCADE from   │  giveaways + entries
                    │  guild_settings │  reaction_roles
                    └────────────────┘  dashboard_sessions
```

### Request lifecycle of a slash command

```
interaction → discord.py app_commands
            → guild_only / default_permissions       (visibility only)
            → cog authorize(): re-check permissions  (the real gate)
            → utils.permissions: hierarchy check     (invoker AND bot)
            → repository: parameterised SQL          (identifier allow-list)
            → Discord REST action
            → utils.modlog: mirror to the log channel
            → reply with mentions disabled

any uncaught exception → cogs.error_handler
                       → expected  : short, actionable, INFO log
                       → unexpected: generic message + reference id,
                                      full traceback in the log only
```

### Project layout

```
fyrion/
├── src/fyrion/
│   ├── __main__.py            # python -m fyrion
│   ├── runtime.py             # supervisor: startup, gather, drain, exit codes
│   ├── bot.py                 # AutoShardedBot, intents, cog discovery
│   ├── config.py              # environment parsing and validation
│   ├── cogs/                  # feature modules, auto-discovered
│   ├── database/
│   │   ├── manager.py         # pooled DatabasePool + CRUD helpers
│   │   ├── schema.py          # idempotent DDL + identifier allow-lists
│   │   └── repositories/      # one module per feature area
│   ├── errors/                # command tree error handler
│   ├── logging/               # text and JSON formatters, rotation
│   ├── utils/                 # permissions, modlog, patterns, transcripts
│   ├── views/                 # persistent and ephemeral UI components
│   └── web/                   # bot-hosted dashboard (API only)
├── dashboard/                 # standalone dashboard (UI + API)
│   ├── app.py
│   ├── templates/
│   └── static/
├── tests/
├── main.py                    # python main.py, no install required
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Installation &amp; self-hosting

### Requirements

- Python 3.10 or newer (3.12 recommended), or Docker
- A Discord application with a bot user

### 1. Discord Developer Portal setup

Open the [Discord Developer Portal](https://discord.com/developers/applications)
and create or open your application.

Under **Bot**:

1. Enable **Server Members Intent** &mdash; required for autorole, welcome and
   leave messages, and invite tracking.
2. Enable **Message Content Intent** &mdash; required for AutoMod, leveling and AFK.
3. Reset and copy the token.

Both intents are privileged. Without them the gateway refuses the connection at
startup, and Fyrion will tell you so explicitly.

> **Never share your bot token or commit it to Git.** If it leaks, reset it in
> the portal immediately.

If you intend to use the dashboard, also open **OAuth2** and register your
redirect URI (`DASHBOARD_BASE_URL` + `DASHBOARD_OAUTH_CALLBACK_PATH`, for
example `http://127.0.0.1:8080/callback`), then copy the client ID and secret.

### 2. Clone the repository

```bash
git clone https://github.com/adityatheog/fyrion.git
cd fyrion
```

### 3. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```dotenv
DISCORD_TOKEN=your_real_token_here
```

Every other value has a documented default. Fyrion validates the whole file at
startup and reports all problems at once rather than failing halfway through
boot.

### 4a. Run with Docker (recommended)

```bash
docker compose up -d --build
docker compose logs -f fyrion
```

The database is persisted in the `fyrion-data` volume and logs in `fyrion-logs`.
The container runs as an unprivileged user with a read-only root filesystem and
`no-new-privileges`, and honours `SIGTERM` so `docker compose stop` drains
cleanly.

```bash
docker compose restart fyrion     # restart
docker compose down               # stop, keep the volumes
docker compose pull && \
  docker compose up -d --build    # update
```

Back up the database with:

```bash
docker compose exec fyrion \
  python -c "import sqlite3,shutil; \
    c=sqlite3.connect('/data/fyrion.db'); \
    b=sqlite3.connect('/data/backup.db'); \
    c.backup(b); b.close(); c.close()"
docker compose cp fyrion:/data/backup.db ./fyrion-backup.db
```

Using SQLite's online backup API rather than copying the file avoids capturing a
torn WAL.

### 4b. Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .
python -m fyrion
```

A fresh clone also runs without installing anything:

```bash
pip install -r requirements.txt
python main.py
```

On startup Fyrion applies its SQLite schema, registers persistent views, loads
every cog under `fyrion.cogs`, and synchronises the global command tree.

Exit codes: `0` clean shutdown, `1` a component failed, `2` the configuration is
unusable.

### 5. Invite the bot

Under **OAuth2 &gt; URL Generator**, select the `bot` and
`applications.commands` scopes, then grant the permissions the features you
intend to use require:

| Feature | Permissions |
| --- | --- |
| Moderation | Kick Members, Ban Members, Moderate Members, Manage Messages |
| AutoMod | Manage Messages, plus whichever action you configure |
| Tickets | Manage Channels, Manage Roles, Attach Files, Embed Links |
| Leveling &amp; reaction roles | Manage Roles |
| Logging | Send Messages, Embed Links, Read Message History |
| Invite tracking | Manage Server |

> **Discord's role hierarchy always applies.** Fyrion cannot moderate a member
> whose highest role is equal to or above its own, and cannot assign a role at
> or above its own. Position Fyrion's role appropriately in
> **Server Settings &gt; Roles**.

Avoid granting `Administrator`. Fyrion validates its own permissions before
every action and reports what is missing.

---

## Web dashboard

Optional and **disabled by default**, because an HTTP surface should be a
deliberate decision.

```dotenv
DASHBOARD_ENABLED=true
DASHBOARD_BASE_URL=https://dashboard.example.com
DISCORD_CLIENT_ID=your_application_id
DISCORD_CLIENT_SECRET=your_client_secret
DASHBOARD_SECRET_KEY=generate_a_long_random_value
```

Generate the signing key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Two deployment modes:

| Mode | Command | Notes |
| --- | --- | --- |
| Alongside the bot | `python -m fyrion` | One event loop. The live guild cache is available, so channel and role pickers work. |
| Standalone | `python -m dashboard` | No gateway client. Stored settings are still editable; pickers fall back to the OAuth snapshot. |

What the dashboard enforces:

- Every `/api` route requires an authenticated session **and** `Manage Server`
  on the target guild, checked against Fyrion's live view of the member.
- Session cookies are HttpOnly, `SameSite=Lax` and `Secure` on https. Mutating
  requests additionally require the session CSRF token in an `X-CSRF-Token`
  header.
- `PATCH` bodies are validated with `extra="forbid"`; referenced channels and
  roles must exist in that guild, and a role Fyrion could not assign is refused
  at configuration time rather than failing silently later.
- Hardened response headers, `Host` allow-listing, explicit CORS origins (never
  `*` with credentials) and per-IP rate limiting.
- Unhandled failures return a short reference id; the traceback stays in the
  log. Filesystem paths are never exposed.

Publishing the port exposes an authenticated admin API. Put TLS and a reverse
proxy in front of it, and set `DASHBOARD_TRUST_PROXY=true` only when that proxy
really does set `X-Forwarded-For`.

---

## Configuration

All configuration is read from the environment (and from `.env` when present).
See [`.env.example`](.env.example) for the fully annotated list.

| Group | Variables |
| --- | --- |
| Core | `DISCORD_TOKEN`, `ENVIRONMENT` |
| Gateway | `SHARD_COUNT`, `MESSAGE_CACHE_SIZE`, `ACTIVITY_NAME`, `SYNC_COMMANDS_ON_STARTUP` |
| Database | `DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_BUSY_TIMEOUT_MS`, `DATABASE_MAINTENANCE_INTERVAL_SECONDS` |
| Logging | `LOG_LEVEL`, `LOG_FORMAT`, `LOG_TO_FILE`, `LOG_DIR`, `LOG_FILE_NAME`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `DISCORD_LOG_LEVEL` |
| Dashboard | `DASHBOARD_ENABLED`, `DASHBOARD_HOST`, `DASHBOARD_PORT`, `DASHBOARD_BASE_URL`, `DASHBOARD_OAUTH_CALLBACK_PATH`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DASHBOARD_SECRET_KEY`, `DASHBOARD_SESSION_TTL_SECONDS`, `DASHBOARD_ALLOWED_ORIGINS`, `DASHBOARD_TRUSTED_HOSTS`, `DASHBOARD_COOKIE_SECURE`, `DASHBOARD_COOKIE_DOMAIN`, `DASHBOARD_ACCESS_LOG`, `DASHBOARD_TRUST_PROXY`, `DASHBOARD_FORWARDED_ALLOW_IPS`, `DASHBOARD_RATE_LIMIT`, `DASHBOARD_RATE_LIMIT_WINDOW`, `DASHBOARD_AUTH_RATE_LIMIT` |

Never commit a populated `.env`. Secrets are redacted from Fyrion's own startup
log output.

---

## Database

Fyrion uses SQLite through a bounded pool of `aiosqlite` connections.

- **WAL journal mode**, so readers never block the single writer.
- **`foreign_keys = ON` per connection**, which is what makes the schema's
  `ON DELETE CASCADE` guarantees real. SQLite defaults this to off.
- **`busy_timeout`**, so writers wait instead of raising "database is locked".
- **Incremental auto-vacuum** with a periodic `incremental_vacuum` and WAL
  checkpoint pass, so deleted rows return their pages without a blocking
  `VACUUM`.
- **A single asyncio write lock**, because SQLite permits one writer at a time.

The schema is idempotent and replayed on every boot, and `PRAGMA user_version`
records its version. Snowflakes are stored as 64-bit integers, timestamps as
ISO-8601 UTC strings, and booleans as `0`/`1`.

Core tables: `guild_settings` (the parent every guild-scoped table cascades
from), `moderation_cases`, `automod_rules`, `economy_accounts`,
`leveling_profiles`, `level_rewards`, `giveaways`, `giveaway_entries`,
`tickets`, `custom_commands`, `reaction_roles`, `dashboard_sessions`, plus the
legacy tables retained for compatibility.

---

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest                                 # tests
pytest tests/ -v                       # verbose
black src tests                        # format
flake8 src tests --max-line-length=120 # lint
mypy src --ignore-missing-imports      # type check
```

CI runs the same checks on Python 3.10, 3.11 and 3.12 for every push and pull
request against `main`.

Before opening a pull request, make sure that tests pass, no secrets are
committed, new functionality has tests where practical, and the code follows the
project's style and typing conventions. See
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Roadmap

Everything originally planned is implemented and shipped.

### Delivered

- [x] Secure moderation with role-hierarchy enforcement
- [x] Warning system with per-guild history
- [x] Button-based ticket system with persistent panels
- [x] Ticket transcripts, claiming and participant management
- [x] AutoMod: anti-link and anti-invite
- [x] AutoMod: spam and flood protection with escalating strikes
- [x] AutoMod: blocked words, caps, mention and line filters
- [x] AutoMod exemptions for users, roles and channels
- [x] Advanced bulk deletion, including safe regular expressions
- [x] Audit logging for messages, members, roles and channels
- [x] Welcome and leave messages with placeholders
- [x] Autorole and mute role with channel-overwrite sync
- [x] Invite tracking with joins, leaves and net counts
- [x] Leveling with XP cooldowns and a quadratic curve
- [x] Level reward roles, stacking or as a ladder
- [x] Reputation system
- [x] Giveaways with live countdowns, requirements and rerolls
- [x] Reaction roles with toggle, add-only, remove-only and unique modes
- [x] Interactive embed builder with preview before posting
- [x] Role and channel management behind confirmation prompts
- [x] Utility suite: info, diagnostics, calculator, polls, AFK
- [x] Fun commands with bounded outbound requests
- [x] Dynamic dropdown help menu
- [x] Pooled SQLite storage in WAL mode with enforced foreign keys
- [x] Structured logging with text and JSON formatters and rotation
- [x] Unified error handling with reference ids
- [x] Web dashboard with Discord OAuth2 and a settings editor
- [x] Docker and docker-compose deployment
- [x] CI across Python 3.10, 3.11 and 3.12

### Under consideration

These are candidates, not commitments. Open a
[feature request](https://github.com/adityatheog/fyrion/issues) if one matters
to you.

- [ ] PostgreSQL storage backend alongside SQLite
- [ ] Raid protection with join-rate heuristics
- [ ] Scheduled announcements and reminders
- [ ] Starboard
- [ ] Localisation of user-facing strings

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `CONFIGURATION ERROR: DISCORD_TOKEN is missing` | `.env` was not created, or the token was left as the placeholder. |
| Startup fails mentioning privileged intents | Enable Server Members and Message Content in the Developer Portal. |
| Slash commands do not appear | Global sync propagates within about an hour. Confirm `SYNC_COMMANDS_ON_STARTUP=true` and that the bot was invited with `applications.commands`. |
| "I cannot moderate a member with an equal or higher role" | Move Fyrion's role above the target's in Server Settings &gt; Roles. |
| Message edit and delete events are missing | Only cached messages raise those events. Raise `MESSAGE_CACHE_SIZE`. |
| `database is locked` | Raise `DATABASE_BUSY_TIMEOUT_MS`, or lower `DATABASE_POOL_SIZE`. |
| Dashboard refuses to start | It requires `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` and a 32-character `DASHBOARD_SECRET_KEY`. |
| OAuth callback rejected | The redirect URI must match the portal entry exactly, including scheme, port and path. |
| Autorole never applies | The role must not be managed by Discord and must sit below Fyrion's highest role. |

Full logs live in `logs/fyrion.log` (or the `fyrion-logs` volume under Docker)
and always record at `DEBUG`, whatever the console level is.

---

## Security

Security is a first-class requirement. If you discover a vulnerability, **do not
open a public issue** &mdash; follow [SECURITY.md](SECURITY.md) for private
reporting.

For self-hosters: keep the token private, never commit `.env`, keep Python and
dependencies updated, grant the minimum Discord permissions needed, protect the
host filesystem, and back up the database.

---

## Contributing

Contributions are welcome &mdash; bug reports, feature requests, documentation
improvements, fixes, tests and pull requests. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md) first.

---

## License

Fyrion is open-source software licensed under the MIT License. See
[LICENSE](LICENSE) for the complete text.

---

## Built with

- [discord.py](https://github.com/Rapptz/discord.py) &mdash; Discord API wrapper
- [aiosqlite](https://github.com/omnilib/aiosqlite) &mdash; asynchronous SQLite
- [FastAPI](https://github.com/fastapi/fastapi) &middot;
  [uvicorn](https://github.com/encode/uvicorn) &middot;
  [Pydantic](https://github.com/pydantic/pydantic) &mdash; the dashboard
- [httpx](https://github.com/encode/httpx) &mdash; outbound HTTP

---

## Disclaimer

Fyrion is provided as open-source software for legitimate Discord server
administration and community management. The maintainers are not responsible for
misuse, incorrect configuration, or damages resulting from self-hosting or
operating the bot.

<div align="center">

**Fyrion** &mdash; Powerful tools for better Discord communities.

[Source](https://github.com/adityatheog/fyrion) &middot;
[Issues](https://github.com/adityatheog/fyrion/issues) &middot;
[Security](SECURITY.md) &middot;
[License](LICENSE)

</div>
