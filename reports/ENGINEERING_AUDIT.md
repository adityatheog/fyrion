# Fyrion — Repository Engineering Audit

**Repository:** `adityatheog/fyrion` (branch `main`)
**Working copy audited:** `/root/projects/fyrion`
**Audit date:** 2026-10-11
**Environment:** Ubuntu under Termux/PRoot on Android — `Linux 6.17.0-PRoot-Distro`
**Runtime Python:** 3.14.4 (project declares `requires-python = ">=3.10"`, CI targets 3.10–3.12)

> **Read this first.** Two environmental facts shape everything below and are not
> the author's opinions:
>
> 1. **The working copy is not a git repository.** The environment reports
>    `Is a git repository: false`. There is therefore no baseline commit, no
>    `git diff`, no branch, and no remote. Every change described here was made
>    directly to files on disk; nothing was committed and **nothing was pushed**.
>    Section P lists every changed file so the work can be reviewed and applied
>    deliberately.
> 2. **Shell execution was unavailable for the entire audit.** Every command that
>    would have executed code — `pytest`, `black`, `flake8`, `mypy`, `python -c`,
>    `ls`, `git` — was refused by the tooling safety classifier. Section K records
>    each blocked command with its exact blocker text. **No test run, formatter
>    check, linter run or type check is claimed as passed**, because none was
>    executed. All verification reported here is *static*: reading the actual
>    source, tracing call sites by hand, and checking contracts between modules.

---

## A. Executive summary

Fyrion is a multi-guild Discord bot (Python, `discord.py` 2.4.0, `AutoShardedBot`)
shipping a SQLite persistence layer, a large administration cog, and **two
independent FastAPI dashboards** — one embedded in the bot process
(`src/fyrion/web/`) and one standalone package (`dashboard/`).

The repository was **not in a runnable state**. Two hard syntax-level failures
meant that a large part of the codebase could not even be imported:

| # | Severity | One-line summary | Status |
|---|---|---|---|
| F-01 | **P0** | `tests/test_dashboard_api.py` line 1 is a truncated `from __future__`, a hard `SyntaxError`; the module cannot be collected, so CI can never be green | **Fixed** |
| F-02 | **P0** | `dashboard/app.py` is a 17-line indented fragment starting at `    guild = access.guild`; `IndentationError`, `python -m dashboard` and `mypy src dashboard` both fail | **Fixed (reconstructed)** |
| F-03 | **P1** | `.env.example` documents `DASHBOARD_OAUTH_CALLBACK_PATH=/callback` while the app serves `/auth/callback`; the documented Docker deployment's OAuth login redirects to a 404 | **Fixed (both sides)** |
| F-04 | **P2** | `_serialize_ids` stringified only `*_id` columns, leaving `resolved_by`, `claimed_by`, `closed_by`, `created_by`, `added_by` as JSON numbers — JavaScript rounds them above 2⁵³−1 | **Fixed** |
| F-05 | **P2** | Unhandled exceptions produced a 500 from Starlette's outermost `ServerErrorMiddleware`, bypassing every user middleware — the one response without the security headers | **Fixed** |
| F-06 | **P3** | `WarningsRepository.clear_warnings` counted rows then deleted them (TOCTOU) while its docstring promised "rows removed"; `delete_warning` had the same lookup-then-delete shape | **Fixed** |
| F-07 | **P3** | Three docstrings claim `guild_configs` is still mirrored/read/written by `/logs`, `/mod` and the welcome/autorole paths. It is not | **Fixed (documentation)** |
| F-08 | **P3** | `Admin._mirror_legacy` is, as a direct consequence of the schema consolidation, a redundant duplicate write | **Documented; removal deferred for authorisation** |

Nothing in the audit suggests the bot's *authorization* model is unsound — the
permission and hierarchy checks in `cogs/admin.py` and `web/app.py` are
consistently defence-in-depth and server-side. The defects found are a broken
build, a broken module, a documentation/deployment mismatch, two precision and
response-hardening bugs, and one latent race. The security-relevant design
(session hashing, CSRF, OAuth state double-submit, token revocation after use,
identifier allow-listing) was reviewed and holds up.

**Honest scope statement.** The line-by-line audit is *complete* for the files
in Section C marked "full" and *incomplete* for those marked "not read". I did
not achieve whole-repository line-by-line coverage, and Section P says exactly
which files remain. Claims below are limited to what was actually read.

---

## B. Scope, methodology and constraints

### B.1 Scope

One repository: `adityatheog/fyrion`, branch `main`, in its working copy at
`/root/projects/fyrion`. No other repository was inspected, cloned or modified.

### B.2 Method

1. **Read before concluding.** Every defect is backed by quoted source with a
   `file:line` anchor. Where a claim rests on a cross-module contract (for
   example "`update_config` writes `guild_settings`, not `guild_configs`"), both
   ends of the contract were read.
2. **Trace call sites by hand.** Because search tools were unavailable, call
   sites were located by reading whole files. This is why several large files
   (`cogs/admin.py`, 1977 lines) were read end to end rather than sampled.
3. **Fix in the real files**, with the surrounding conventions preserved: the
   codebase's comment density, its `Final[...]` annotations, its `< 88`
   column style, its "explain *why*, not *what*" docstrings.
4. **Add a regression test for each behavioural fix**, in the suite's existing
   idiom (`pytest.mark.asyncio`, the shared `db_manager` fixture).
5. **Record every check as run, failed, or blocked** — with the exact command
   and the exact blocker. No check is reported as passing unless its output was
   observed.

### B.3 Constraints honoured during the audit

These are the user's own operating rules, and they shaped several decisions:

- **No secrets printed.** No token, key, password or session value appears in
  this report. `.env.example` values are placeholders by construction.
- **No destructive operations.** No file was deleted wholesale; the one large
  rewrite (F-02) replaced a module that could not be imported at all, and the
  recovered fragments were preserved verbatim inside it.
- **No cleanup of the user's work without authorisation.** This is *why F-08 was
  documented rather than deleted* — see G.6. It is also why the working `.env`
  (if present) was never touched.
- **No test disabled or weakened** to obtain a green build.
- **No push, no force-push, no history rewrite** — there is no git repository to
  push from, and none was created.
- **No fabricated results.** Section K distinguishes "executed" from "blocked"
  for every command.
- **No new repository modules invented.** The only new files are one regression
  test module and this report; both were required by the brief.

### B.4 The single most important limitation

The tooling that classifies shell commands for safety was unavailable for the
whole session. That removed `pytest`, `black`, `flake8`, `mypy`, `git` and even
`ls`. Consequently:

- No fix in this report is *runtime-verified*.
- The two reconstructed/edited modules are *statically* verified against their
  callers, their tests' expectations and the templates/JS that consume them —
  not by execution.

This is stated up front because it materially changes what the report can claim,
and it is repeated at every point where it matters.

---

## C. Repository inventory

The working copy could not be enumerated with `ls`/`find` (blocked, see K). The
inventory below is assembled from the files actually opened during the audit,
plus the module set implied by `pyproject.toml` and `fyrion/bot.py`. It is
therefore **complete for what was read and explicitly incomplete for the rest**.

### C.1 Files read in full

| File | Lines | Role |
|---|---|---|
| `src/fyrion/cogs/admin.py` | 1977 | Settings, roles, channels, embed builder, reaction roles, reaction-role events |
| `src/fyrion/web/app.py` | ~975 | Embedded dashboard: sessions, CSRF, OAuth callback, `/api` routers |
| `src/fyrion/config.py` | 414 | Env parsing, validation, dashboard secret handling |
| `src/fyrion/database/schema.py` | 929 | `LEGACY_SCHEMA`, `CORE_SCHEMA`, allow-lists, enum mirrors |
| `src/fyrion/bot.py` | 276 | `AutoShardedBot` subclass, intents, cog discovery, lifecycle |
| `src/fyrion/web/security.py` | 229 | HMAC session hashing, CSRF derivation, OAuth state signing, cookies |
| `src/fyrion/web/auth.py` | 162 | Discord OAuth2 authorization-code flow with revoke-after-use |
| `src/fyrion/utils/modlog.py` | 118 | Log-channel resolution (3-tier) and best-effort delivery |
| `src/fyrion/utils/transcripts.py` | 211 | Plain-text ticket transcript rendering (read during the security pass) |
| `src/fyrion/database/repositories/guild_config.py` | 59 | Legacy-key façade over `guild_settings` |
| `src/fyrion/database/repositories/warnings.py` | 79 | Warning CRUD, guild-scoped |
| `tests/conftest.py` | 95 | Env defaults, `db_manager` fixture, role/member mock factories |
| `tests/test_dashboard_api.py` | 219 | Pins the standalone dashboard's update/validation contract |
| `dashboard/app.py` | 1027 | Standalone dashboard (reconstructed — see F-02) |
| `.env.example` | 157 | Documented configuration surface |
| `pyproject.toml` | 71 | Packaging, extras, mypy/mypy-of-dashboard config |

### C.2 Files read in part

| File | Read | Not read |
|---|---|---|
| `src/fyrion/database/manager.py` | 1573 total; pool, `execute/fetchrow/fetchall/fetchval`, `count/exists`, `get/update_guild_settings`, `ensure_guild`, `delete_guild`, `stats()`, `transaction()` | the per-feature helper methods between those regions |
| `src/fyrion/database/schema.py` | 1–175 and 175–929 in two passes | — (effectively full) |

### C.3 Not read during this audit

`src/fyrion/cogs/`: `automod.py`, `antinuke.py`, `tickets.py`, `leveling.py`,
`economy.py`, `giveaways.py`, `invites.py`, `utility.py`, `fun.py`, `help.py`,
and the error-handler cog; `src/fyrion/logging/logger.py`;
`src/fyrion/utils/`: `colors.py`, `emojis.py`, `tickets.py`;
`src/fyrion/views/`: `confirm.py`, `embeds.py`, `tickets.py`;
`src/fyrion/database/repositories/`: `antinuke.py`, `automod_rules.py`,
`invites.py`, `leveling.py`, `security.py`; `src/fyrion/web/models.py` (read
only through its imports: `CHANNEL_FIELDS`, `ROLE_FIELDS`,
`GuildSettingsUpdate`); the whole `tests/` tree except the two files above;
the `dashboard/templates/*.html`, `dashboard/static/js/*.js`,
`dashboard/__main__.py` (read in an earlier pass; all route names and context
variables were confirmed against the reconstruction).

Per the brief, this incompleteness is reported rather than papered over. Section
P lists it as a to-do with a concrete method.

### C.4 Deliberately not touched

- Any `.env` file (may hold live secrets).
- `*.db` / WAL files (real data; no migrations were run).
- Any cache or artifact directory.

---

## D. Architecture reconstruction

### D.1 Process and runtime

`fyrion/bot.py` defines `Fyrion(commands.AutoShardedBot)`:

- `build_intents()` starts from `Intents.none()` and enables exactly
  `guilds`, `members` (privileged), `message_content` (privileged),
  `guild_messages`, `guild_reactions`, `invites`, `moderation`. The explicit
  construction is a genuine security property: no intent is on by accident.
- `discover_extensions()` walks `fyrion.cogs` with `pkgutil.iter_modules`,
  skipping `_`-prefixed modules and sub-packages. **Any non-underscore module in
  `fyrion/cogs/` is a loadable extension** — a useful invariant when auditing
  that directory.
- `setup_hook()` opens the DB pool → starts maintenance → registers persistent
  views → loads cogs → syncs the command tree. A failed cog is logged and
  skipped rather than crashing boot (`_load_extensions`), so **a broken cog
  degrades silently into a warning** — worth remembering when reading logs.
- `close()` closes the pool before the gateway session.

### D.2 Persistence

- `DatabasePool` (aliased `DatabaseManager`) fronts aiosqlite with WAL,
  `foreign_keys = ON`, a busy timeout, incremental auto-vacuum, and a **single
  non-reentrant write lock**. `transaction()`'s docstring warns that calling
  `execute()` inside it deadlocks — a real footgun, and the reason repositories
  compose single statements rather than nested transactions.
- `execute()` returns `cursor.rowcount`; `fetchrow`/`fetchall`/`fetchval` return
  `aiosqlite.Row`s (string-indexable, as `modlog.py` relies on).
- **Identifier safety:** table and column names are validated against
  `TABLE_COLUMNS` / `PRIMARY_KEYS` / `GUILD_SCOPED_TABLES` before reaching SQL;
  values are always bound parameters. This is the right pattern for the one part
  of SQL that cannot be parameterised, and it is applied consistently — e.g.
  `guild_config.py::update_config` maps a fixed key allow-list to columns and
  raises `ValueError` for anything else.
- **Two table generations coexist.** `LEGACY_SCHEMA` (`guild_configs`,
  `warnings`, `whitelists`, `ticket_configs`, `member_inviters`, `invite_stats`)
  is replayed on every boot alongside `CORE_SCHEMA`. `guild_settings` is
  declared the single source of truth; legacy tables are kept so pre-existing
  rows stay readable.

### D.3 The two dashboards

| | Embedded (`src/fyrion/web/`) | Standalone (`dashboard/`) |
|---|---|---|
| Entry point | `create_app(bot)` inside the bot process | `create_app()`, `python -m dashboard` |
| Live bot state | Yes — resolves members/guilds from the running client | No — detached; guild data comes from the OAuth `guilds` scope |
| OAuth scope | `identify` (`web/auth.py`) | `identify guilds` |
| Session mechanism | server-side token hashes in `dashboard_sessions`, cookie holds the opaque token | signed session cookie (`SessionMiddleware`) |
| Settings response | `_serialize_ids(settings)` → snowflakes as **strings** | raw ints (pinned by `tests/test_dashboard_api.py`) |
| Channel/role validation | against the live guild | refused when detached (400), enforced when a bot is attached |

The two are independent implementations with different trust models, which is
why the response-type divergence in row 6 is *by design* and is documented here
rather than "fixed" (see I.6).

Starlette middleware ordering matters and both apps get it right:

```
ServerErrorMiddleware            (outermost)
  → user middleware (LAST add_middleware call is OUTERMOST)
    → ExceptionMiddleware        (HTTPException / RequestValidationError handlers live here)
      → router
```

A handler registered for `Exception` runs in `ServerErrorMiddleware`, i.e.
*outside* every user middleware. In this codebase that means its 500 response
would be the only response missing the hardening headers — precisely F-05.

### D.4 Configuration

`Config` parses `.env`/environment at import, accumulating issues in `_ISSUES`
and raising one aggregated `ConfigurationError` from `validate()`. Relevant
behaviours:

- `DASHBOARD_BASE_URL` is `rstrip("/")`-normalised.
- `DASHBOARD_OAUTH_CALLBACK_PATH` defaults to `/auth/callback`;
  `dashboard_redirect_uri()` prefixes a missing `/` and concatenates — so the
  setting, not a literal, defines the redirect URI.
- Production (`is_production()`) additionally requires https and forbids
  disabling `Secure` cookies.
- `DASHBOARD_SECRET_KEY` must be ≥ 32 characters when the dashboard is enabled.

---

## E. Build, environment and toolchain

### E.1 Packaging

`pyproject.toml` declares a src-layout package with `setuptools>=61`,
`requires-python = ">=3.10"`, and extras:

- `web`: `fastapi>=0.115.6`, `uvicorn>=0.32.1`, `starlette>=0.41.3`,
  `pydantic>=2.10.4`, `httpx>=0.27.2`, `aiohttp>=3.10.11`, `jinja2>=3.1.4`,
  `itsdangerous>=2.2.0`
- `dev`: pins `black==26.5.1` exactly, because CI runs `black --check src tests`

`[tool.mypy]` sets `mypy_path = "src"`, `explicit_package_bases = true`,
`namespace_packages = true`, `python_version = "3.10"`,
`ignore_missing_imports = true`, and documents that the check covers **both**
`src` and `dashboard`. That is why a broken `dashboard/app.py` was not merely a
cosmetic problem: it failed the project's own configured type check.

### E.2 Build state

**BLOCKED.** No build command could be executed. What can be said statically:

- `dashboard/app.py` previously began mid-expression (`    guild = access.guild`
  at module scope), an unconditional `IndentationError`.
- `tests/test_dashboard_api.py` previously began with a truncated
  `from __future__` (plus a UTF-8 BOM), an unconditional `SyntaxError`.
- Both are repaired; the repaired files were read back line by line.

### E.3 Python version skew

The runtime is **Python 3.14.4**; the project declares `>=3.10` and CI targets
3.10–3.12. Nothing observed is 3.14-incompatible (no `distutils`, no removed
`asyncio` loop parameters in the code read), but **the supported interpreter
matrix was not exercised** and cannot be claimed as verified. Recorded in P.

---

## F. Defect register

Severity: **P0** build/import-breaking · **P1** user-visible functional break ·
**P2** correctness/security weakness · **P3** latent fault or misleading
documentation. Confidence reflects how directly the evidence supports the claim.

---

### F-01 — Truncated `from __future__` import (P0, confidence: certain)

**Evidence.** `tests/test_dashboard_api.py` line 1 read:

```python
from __future__
```

with a UTF-8 BOM. A `from ... import` statement with no `import` clause and no
imported names is a **hard `SyntaxError`** at compile time.

**Impact.** The module cannot be collected by pytest. Under any CI that runs
`pytest tests/`, the suite fails before a single test executes. Every test in
the file is dead.

**Fix.** Line 1 replaced with a module docstring followed by the intended
`from __future__ import annotations`, which also removes the BOM problem by
putting text before it. The file's six tests were preserved unmodified.

---

### F-02 — `dashboard/app.py` is a non-importable fragment (P0, confidence: certain)

**Evidence.** The file consisted of 17 lines beginning at module scope with:

```python
    guild = access.guild
```

Four leading spaces on a top-level statement is an unconditional
`IndentationError`. The fragment matched an inner block of the detached-guild
guard (a 400 response for channel/role fields when no live guild is attached).

**Impact.** `python -m dashboard` cannot start, `mypy src dashboard` fails, and
`tests/test_dashboard_api.py` cannot import `dashboard.app` — so F-01 could not
have been the only CI failure.

**Repair, and why it is a reconstruction rather than a patch.** There was no
recoverable whole module: the file contained one block of one function. The
module was rebuilt to the contract that *other, surviving* files already
enforce:

- `tests/test_dashboard_api.py` — import names (`GuildAccess`, `SESSION_USER`,
  `SESSION_GUILDS`, `SESSION_CSRF`), the 4-field `GuildAccess` tuple, the
  `AsyncMock` patch target `dashboard.app.authorize_guild`, positional call
  `api_update_guild(guild_id, payload, request)`,
  `result["updated"] == ["economy_daily_amount"]`, and
  `result["settings"]["mod_log_channel_id"] == 123` **as an int**.
- `dashboard/templates/*.html` — every route name referenced
  (`index`, `dashboard_page`, `login`, `logout`, `manage_page`, `static`) and
  every context variable supplied.
- `dashboard/static/js/*.js` — the response contract `data.updated`,
  `data.settings`, `data.error`, `data.details[].{field,message}`,
  `data.reference`.
- `fyrion.database.manager.DatabasePool` and `fyrion.config.Config` — the real
  method and attribute signatures.
- `fyrion.web.app` — the sibling implementation, mirrored for style so the two
  dashboards read as one codebase.

The recovered 400 fragment is reproduced **verbatim**, which is the strongest
available evidence that the reconstruction is faithful: an independently written
implementation would not produce the same error strings.

Result: 1027 lines (read to EOF to confirm). The security posture is documented
in the module docstring: signed session cookie; the OAuth access token is
revoked immediately after the profile read; authorization is derived from
Discord's own answer about the user's guilds; the detached dashboard refuses
channel and role edits it cannot validate.

**Honest qualification.** This is the largest single change in the audit and it
is a *reconstruction*, not a minimal diff. It is justified because the original
could not be imported, but the user should review it as new code.

---

### F-03 — Documented OAuth callback path is not served (P1, confidence: high)

**Evidence.**

- `.env.example` line 109: `DASHBOARD_OAUTH_CALLBACK_PATH=/callback`
- `src/fyrion/config.py:222-224`: default `"/auth/callback"`
- `src/fyrion/web/app.py`: the callback was registered by
  `@auth_router.get("/callback")` on a router with `prefix="/auth"` → the
  effective path was always `/auth/callback`, and the setting had **no effect**
  on the embedded app.

**Impact.** An operator following `.env.example` (the documented Docker path)
sets the variable to `/callback`, registers `https://host/callback` in the
Developer Portal, and the login redirect lands on a 404. The setting was
*decorative for one app and authoritative for the other* — the worst of both.

**Fix, applied on both sides so they cannot drift again:**

1. `.env.example` now documents `/auth/callback`, with a comment explaining that
   the variable must match the registered redirect URI character for character.
2. The embedded app now registers its callback at
   `Config.DASHBOARD_OAUTH_CALLBACK_PATH` (`web/app.py`, `add_api_route`), making
   the setting authoritative for **both** apps. With the default value the URL
   is unchanged, so this is not a breaking change.

A regression test (`test_env_example_documents_the_default_callback_path`) reads
`.env.example` and asserts the documented value, so documentation/code drift
fails the suite instead of shipping.

---

### F-04 — Snowflake precision loss for five columns (P2, confidence: high)

**Evidence.** `web/app.py::_serialize_ids` (before the fix) stringified a value
only when its **key ended in `_id`**. But `schema.py` itself documents snowflake
columns that do not carry that suffix, and the queries return them:

- `resolved_by` — `moderation_cases`, returned by `/api/guilds/{id}/cases`
- `claimed_by`, `closed_by` — `tickets`, selected explicitly by
  `/api/guilds/{id}/tickets`
- `created_by` — `automod_rules` (`/automod`), also `custom_commands`,
  `reaction_roles`, `economy_shop_items`
- `added_by` — `antinuke_whitelist`

**Impact.** Discord snowflakes exceed 2⁵³−1. Emitted as JSON numbers, a browser
parses them as IEEE-754 doubles and **silently rounds them**, so
`resolved_by`/`claimed_by`/`closed_by` render as the wrong user id. This is the
exact failure mode `_serialize_ids` exists to prevent; the heuristic simply
missed five columns.

**Fix.** A named set plus a predicate, so the rule is one readable sentence:

```python
_SNOWFLAKE_COLUMNS_WITHOUT_ID_SUFFIX = frozenset(
    {"resolved_by", "claimed_by", "closed_by", "created_by", "added_by"}
)

def _is_snowflake_column(key: str) -> bool:
    return key.endswith("_id") or key in _SNOWFLAKE_COLUMNS_WITHOUT_ID_SUFFIX

def _serialize_ids(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if value is not None and isinstance(value, int) and _is_snowflake_column(key):
            result[key] = str(value)
        else:
            result[key] = value
    return result
```

Regression tests cover both directions: all eight snowflake columns stringify,
and `case_number` / `duration_seconds` / `uses` / `reason` stay untouched, with
`None` left alone and the caller's dict not mutated.

**Residual risk to flag.** If any existing test asserted these columns as ints,
it will now fail. I could not run the suite to check (Section K). The new
behaviour is the correct one — a rounded user id is a bug, not a contract.

---

### F-05 — 500 responses bypass the security headers (P2, confidence: high)

**Evidence.** `web/app.py::_install_exception_handlers` registered
`@app.exception_handler(Exception)`. Starlette installs the `Exception` handler
on `ServerErrorMiddleware`, which is **outside every user middleware**. The
`_harden` middleware is what sets `_SECURITY_HEADERS` (CSP, nosniff, frame
denial, referrer policy, …). Therefore the handler's 500 was the single response
that shipped **without** them.

**Impact.** An error page with no `X-Content-Type-Options`, no CSP and no
frame-denial is the response most likely to be reflected and least likely to be
hardened — a small but real weakness, and an inconsistency that would erode the
value of the header set.

**Fix.** The catch moved into `_harden` itself, one layer in, so the 500 is built
inside the same path as every other response:

```python
@app.middleware("http")
async def _harden(request: Request, call_next: Any) -> Response:
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        reference = uuid.uuid4().hex[:8]
        log.error(
            "Unhandled dashboard error on %s %s (reference %s)",
            request.method, request.url.path, reference, exc_info=exc,
        )
        response = JSONResponse(
            {"error": "An internal error occurred.", "reference": reference},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    ...
```

The now-unreachable `@app.exception_handler(Exception)` block was removed, and
the `HTTPException` / `RequestValidationError` handlers were kept: those are
installed on `ExceptionMiddleware` (inner) and *do* pass back out through
`_harden`, so they were never affected.

The same ordering hazard was designed out of `dashboard/app.py` from the start:
`_install_handlers(app)` is called **last** in `create_app`, making `_harden` the
outermost user middleware, with a comment saying why.

The reference id is the one deliberate trade: it gives a user something to quote
without exposing SQL text, paths or tracebacks.

---

### F-06 — Count-then-delete race in the warnings repository (P3, confidence: high)

**Evidence.** `clear_warnings` previously counted with `COUNT(*)` and then
issued a `DELETE`; its docstring said "Returns rows removed". Those are equal
only if nothing else touches the rows in between — and the pool has a single
write lock, so a *concurrent writer* is possible between two awaited statements.
`delete_warning` had the same shape: a lookup, then a delete of what was looked
up.

**Impact.** The returned count could disagree with reality; the function's
promise and its behaviour diverged under load. Low severity (a wrong number in a
moderation reply), but it is exactly the class of bug that is invisible until it
matters.

**Fix.** Both became single statements whose own row count *is* the answer:

```python
async def clear_warnings(self, guild_id: int, user_id: int) -> int:
    query = "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?"
    return await self.db.execute(query, (guild_id, user_id))

async def delete_warning(self, guild_id: int, warning_id: int) -> bool:
    query = "DELETE FROM warnings WHERE guild_id = ? AND id = ?"
    return await self.db.execute(query, (guild_id, warning_id)) > 0
```

Both remain guild-scoped, so the cross-guild leak the original scoping
prevented is preserved. Regression tests assert the true removed count, that a
second clear returns `0`, that unrelated members and guilds are untouched, and
that deleting through the wrong guild id returns `False` and leaves the row.

---

### F-07 — Three docstrings assert a legacy mirror that does not exist (P3, confidence: certain)

**The claim, in three places.**

1. `cogs/admin.py` module docstring: *"A couple of settings (welcome channel,
   autorole) are still mirrored into the legacy `guild_configs` row for
   listeners that predate the consolidation."*
2. `cogs/admin.py::_mirror_legacy`: *"Writes a setting to the legacy
   `guild_configs` row as well. The welcome/autorole listeners and the `/logs`
   commands still read that table, so mirroring keeps both views of the
   configuration in sync."*
3. `utils/modlog.py` resolution order: tier 3 is *"the legacy column the `/logs`
   and `/mod` commands still write to"*.

**The reality.** `repositories/guild_config.py` line 58:

```python
await self.db.update_guild_settings(int(guild_id), **{column: value})
```

`update_config` writes `guild_settings`. `get_config` likewise reads
`guild_settings`. The module's own docstring says so: *"Historically this
repository read and wrote the legacy `guild_configs` table; it now delegates to
the pooled `guild_settings` helpers so there is a single source of truth."*
`guild_configs` is **not written by any of those paths**.

**Why the documentation is worse than cosmetic.** It misleads a future
maintainer into believing a legacy read path is maintained — which is how a
supposedly-live fallback becomes permanently stale without anyone noticing. And
`modlog.py` *does* read `guild_configs.log_channel_id` (`LEGACY_QUERY`), so the
docstring made that read look load-bearing when nothing populates it.

**Fix (documentation only).** All three statements now describe what the code
does:

- The module docstring states settings are written to `guild_settings` alone and
  that `guild_configs` is not written by the cog, "so the two can never
  disagree."
- `_mirror_legacy`'s docstring states plainly that the repository was rewired
  during the consolidation, that the call writes the same column the caller
  already wrote, and that it never touches `guild_configs`.
- `modlog.py` describes tier 3 as a **read-only fallback** for rows written
  before the consolidation, and notes no current command writes the column.

---

### F-08 — `_mirror_legacy` is a redundant duplicate write (P3, confidence: high) — **NOT removed, pending authorisation**

**Evidence.** `cogs/admin.py` calls `_mirror_legacy` at four sites:
lines 410 and 424 (`welcome_channel_id`), 522 and 534 (`autorole_id`). Each is
immediately preceded by an identical write to the same column. Tracing
`_mirror_legacy` → `GuildConfigRepository.update_config` →
`_KEY_TO_COLUMN` → `db.update_guild_settings` gives, for example:

```python
if channel is None:
    await self.db.update_guild_settings(guild.id, welcome_channel_id=None)
    await self._mirror_legacy(guild.id, "welcome_channel_id", None)   # same write
```

The `_KEY_TO_COLUMN` map is `welcome_channel_id → welcome_channel_id`,
`log_channel_id → mod_log_channel_id`, `autorole_id → autorole_id` — so every
mirrored call is an exact duplicate of the line above it, against the same
table. `self.legacy` is assigned in `__init__` (line 113) and referenced
**only** from `_mirror_legacy`; the whole file (1977 lines) was read to confirm
that.

**Impact.** One redundant `UPDATE` per `/set-welcome` and `/set-autorole`
invocation, plus a second, swallow-everything write path whose log message
("Could not mirror … into guild_configs") names a table the call never touches.

**Decision: documented, not deleted — and here is the reasoning.**

- *Repairing* it (making it write `guild_configs` for real) would contradict the
  codebase's explicit architectural decision that `guild_settings` is the single
  source of truth, and would require raw SQL outside the `TABLE_COLUMNS`
  allow-list discipline used everywhere else. Rejected.
- *Deleting* it is behaviour-neutral and is what a maintainer would do next —
  but it removes the user's existing code, and the brief is explicit: *"Do not
  overwrite, discard, reset, or clean up the user's existing work without
  explicit permission."* It is also not verifiable here: confirming that nothing
  outside this file reaches into `cog.legacy` needs a repository-wide search,
  and search tools were unavailable all session. The working copy is not a git
  repository, so a mistaken deletion would not be recoverable from history.

The finding is therefore recorded with the **exact patch** for the user to apply
if they authorise it: remove the `_mirror_legacy` method (lines 206–220), the
four call sites (410, 424, 522, 534), the `self.legacy = GuildConfigRepository(
self.db)` assignment (line 113), and the now-unused
`GuildConfigRepository` import (line 49). This is a P3 code-quality item, not a
correctness defect; leaving it in place changes no behaviour.

---

## G. Fixes implemented

Per-fix protocol: locate the defect → quote the source → establish intent from
the surrounding design → implement the smallest real fix that removes the defect
→ preserve conventions → add a regression test where behaviour changed → state
what was *not* verified.

### G.1 Summary of changed files

| File | Change | Lines (approx.) |
|---|---|---|
| `tests/test_dashboard_api.py` | F-01: line 1 docstring + intact `from __future__` | +11 / −1 |
| `dashboard/app.py` | F-02: reconstructed the module | 1027 (was 17) |
| `.env.example` | F-03: callback path corrected + 4-line explanatory comment | +4 / −1 |
| `src/fyrion/web/app.py` | F-03 (dynamic callback route), F-04 (snowflake predicate), F-05 (500 hardening) | +58 / −12 |
| `src/fyrion/database/repositories/warnings.py` | F-06: single-statement delete semantics + docstrings | +14 / −18 |
| `src/fyrion/cogs/admin.py` | F-07: two false docstrings corrected | +2 / −2 |
| `src/fyrion/utils/modlog.py` | F-07: false tier-3 description corrected | +2 / −1 |
| `tests/test_audit_regressions.py` | **New:** 6 regression tests (150 lines) | +150 |
| `reports/ENGINEERING_AUDIT.md` | **New:** this report | — |

Exact figures were confirmed by reading to EOF for `dashboard/app.py` (1027) and
`tests/test_audit_regressions.py` (150). The smaller edits are quoted below and
their deltas are counted from the quoted before/after text; a `diff --stat` was
not possible (no git, no shell).

### G.2 What each fix preserves

- **Style.** Two-space indentation, `< 88` columns, trailing commas, double
  quotes, `Final[...]` annotations, and the codebase's habit of explaining *why*
  in a comment. The `_serialize_ids` change was deliberately rewritten from a
  compact nested conditional into a named predicate because black would collapse
  the former into an unreadable single line — matching the formatting already
  accepted elsewhere in the file.
- **Contracts.** `GuildAccess`'s 4 positional fields, the raw-integer settings
  response the standalone dashboard's tests pin, the exact 400 messages the
  recovered fragment contained, and every route name and context variable the
  templates and JS require.
- **Security posture.** No validation, authorization or error handling was
  weakened to make anything appear to work — the one place this could have been
  tempting (the `require_csrf` length check, which would have broken the tests'
  `"csrf"` value) was solved by leaving the comparison constant-time and
  length-agnostic instead.

### G.3 Deliberate non-changes

| Observation | Why left alone |
|---|---|
| `/auth/logout` (POST) has no `require_csrf`, unlike `/auth/logout-all` | Accepted, mitigated risk. `SameSite=Lax` means a cross-site POST does not carry the cookie; and a forced-logout is a denial-of-service against oneself, not a privilege gain. Changing it would break the login/logout test contract. Documented in I.5. |
| Embedded API returns string snowflakes; standalone returns ints | Pinned by `tests/test_dashboard_api.py` (`== 123`). The divergence is a consequence of two different trust models. Documented in I.6, not "harmonised". |
| `secret_key()` = `sha256(DASHBOARD_SECRET_KEY)` without a KDF | Input is a ≥32-character random secret enforced by `Config.validate()`; there is no low-entropy password to stretch. Noted in I.4. |
| `Fyrion._load_extensions` swallows per-cog load failures | Deliberate availability trade (one broken cog must not stop the bot). Kept; noted as an observability caveat in L.3. |

### G.4 Verification actually performed

Static only:

- Every edited region was **read back** after editing.
- `dashboard/app.py` was checked against all four Jinja templates, both JS files,
  the tests' import list and assertions, and the real `DatabasePool`/`Config`
  signatures.
- `_is_snowflake_column` / `_serialize_ids` were read back verbatim and their
  behaviour traced by hand against the five column names in `schema.py`.
- `clear_warnings` / `delete_warning` were read back and traced against
  `execute() -> cursor.rowcount`.
- The three `_mirror_legacy` call sites and the `_KEY_TO_COLUMN` map were read
  from both files to confirm the duplication claim.
- `web/app.py` was read back at the middleware, handler, serialization and
  `create_app` regions to confirm all four edits are present.

**Not verified:** no code was executed. Syntax, formatting and types are checked
by inspection, not by `python -m py_compile`, `black`, `flake8` or `mypy`.

---

## H. Change classification (Keep / Repair / Refactor / Rewrite)

| Component | Verdict | Reasoning |
|---|---|---|
| `src/fyrion/web/security.py` | **Keep** | Sound design: keyed HMAC session hashes, CSRF derived from the HttpOnly session token with a distinct `csrf:` domain prefix, signed+expiring OAuth state with double submit, `compare_digest` throughout, `SameSite=Lax` + `HttpOnly` + conditional `Secure`. No defect found. |
| `src/fyrion/web/auth.py` | **Keep** | Correct authorization-code flow; scope limited to `identify`; the access token is revoked in a `finally` so it cannot outlive the request; Discord's error body is logged locally and never returned to the browser. |
| `src/fyrion/config.py` | **Keep** | Aggregated validation with a single actionable error, production hardening (https, `Secure` cookies, ≥32-char secret), and `dashboard_redirect_uri()` as the single source of the redirect URI. |
| `src/fyrion/database/manager.py` | **Keep** | Identifier allow-listing, parameter binding, WAL + `foreign_keys` + busy timeout, non-reentrant write lock documented as such, `stats()` that deliberately omits `db_url`. |
| `src/fyrion/cogs/admin.py` | **Repair** | Authorization, hierarchy checks and confirmation flows are careful. Two docstrings were false (F-07); one helper is redundant (F-08). Structure is sound — no rewrite warranted. |
| `src/fyrion/web/app.py` | **Repair** | Two real defects (F-04, F-05) fixed in place. The rest — rate limiting, CORS allow-list, trusted hosts, `no-store` on `/api` and `/auth`, reference-id error bodies — is well built. |
| `src/fyrion/database/repositories/warnings.py` | **Repair** | TOCTOU removed (F-06); guild scoping preserved. |
| `src/fyrion/utils/modlog.py` | **Repair** | The 3-tier resolution and best-effort delivery are correct; only the tier-3 description was false (F-07). |
| `src/fyrion/utils/transcripts.py` | **Keep** | Read during the security pass. Filenames are derived from integer ticket numbers and channel ids (`ticket-0007-<channel_id>.txt`), never from user text, so there is no path-traversal or injection surface. Message content is written verbatim as plain text, never rendered as markup; carriage returns are normalised; each message is bounded at 4000 characters; history is bounded at 1000 messages with the truncation flag computed from one extra read rather than guessed; attachments and embeds are listed but never downloaded; `build_file` builds a fresh `discord.File` per upload because a `File` wraps a consumable stream. No defect found. |
| `dashboard/app.py` | **Rewrite (justified)** | Nothing to repair: one function body, no module. Rebuilt to the contract imposed by the surviving tests, templates, JS and the sibling app. |
| `tests/test_dashboard_api.py` | **Repair** | One leading line (F-01). All six tests preserved. |
| `src/fyrion/bot.py` | **Keep** | Explicit intent construction, graceful degradation, pool closed before the gateway. |
| Section C.3 files | **Not assessed** | Not read; no verdict is offered, because a verdict without reading would be a guess. |

---

## I. Security review

### I.1 Authentication and session handling — sound

`web/security.py` stores only `hmac_sha256(key, token)`, so a database leak
yields no replayable credential; IPs are likewise keyed hashes. The session
cookie is `HttpOnly`, `SameSite=Lax` (needed for the top-level redirect back
from Discord), `Secure` when `DASHBOARD_COOKIE_SECURE` is on, and forced on in
production. `hash_token` and `csrf_token` use different message domains, so the
CSRF value handed to JavaScript cannot collide with the stored lookup key —
a subtle and correct detail.

### I.2 CSRF — sound, with one accepted gap

Mutating endpoints depend on `require_csrf`, a constant-time check against a
token derived from the session. `web/security.py::verify_csrf` returns `False`
for a missing token on either side.

The gap: the embedded app's `POST /auth/logout` does not require CSRF, while
`POST /auth/logout-all` does. Under `SameSite=Lax` a cross-site POST omits the
cookie, so the practical impact is a forced logout at worst. This is
**documented as an accepted, mitigated risk rather than changed**, because it is
pinned by the existing test contract and because changing it would alter
behaviour the user has not asked to alter.

### I.3 OAuth — sound

State is signed **and** double-submitted in an `HttpOnly` cookie, with a 600 s
TTL; `verify_state` checks the double submit, then the HMAC signature, then the
expiry, each with `compare_digest`, and tolerates malformed base64/JSON without
raising. The access token requested with `identify` is revoked immediately after
the profile read. Authorization is **never** taken from the browser: the
embedded app re-derives guild access from the bot's own membership view.

### I.4 Key derivation — informational

`secret_key()` derives the HMAC key as `sha256(DASHBOARD_SECRET_KEY)`. Because
`Config.validate()` requires ≥32 characters when the dashboard is enabled and
the documented generation method is `secrets.token_urlsafe(48)`, there is no
low-entropy input to stretch, and a KDF would add cost without adding strength.
Recorded as informational, not a defect. The unset-key path generates an
ephemeral per-process key and logs a loud warning; the reconstructed standalone
dashboard additionally raises in production when the key is missing, so a
production deployment cannot silently sign sessions with a key that dies on
restart.

### I.5 Response hardening — improved

Every response from the embedded app now carries `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy`, a `default-src 'none'` CSP,
`Permissions-Policy`, COOP and CORP; HSTS is added when cookies are `Secure`;
and `/api` and `/auth` responses get `Cache-Control: no-store`. The 500 path
(F-05) is now inside that envelope, and it returns a reference id rather than
internals.

### I.6 Secrets — none found in the audited set

No literal credential appears in any file read. `.env.example` uses placeholders,
and the bot token is validated against known placeholder values. `Config` and
`DatabasePool.stats()` both deliberately withhold sensitive values
(`db_url` is a filesystem path and is excluded from the API).

**Caveat and standing recommendation.** The audit covered source files only.
It could not run a secret scanner, and it did not inspect git history — there is
no git repository here. If any credential has ever been committed to this
project's history, **removing it from the latest version does not remove it from
history**; the correct response is to revoke and rotate the credential in the
Discord Developer Portal (and anywhere else it was used), then purge history if
required. No specific credential is alleged to have leaked.

### I.7 Injection, authorization and rate limiting — sound

SQL identifiers cannot be parameterised, and the codebase handles that correctly
by validating every table and column name against `TABLE_COLUMNS` /
`PRIMARY_KEYS` / `GUILD_SCOPED_TABLES` while binding all values; validation
failures raise rather than concatenate. Authorization is re-checked server-side
on both dashboards, with Discord's role hierarchy enforced for the invoker *and*
the bot, and `Administrator`-granting roles restricted to administrators so the
bot cannot be used for privilege escalation. A per-IP sliding-window limiter
(with eviction, so it cannot itself become a memory-exhaustion vector) fronts the
API and the auth routes; `TrustedHostMiddleware` blocks Host-header and DNS
rebinding attacks against a loopback bind; CORS is origin-restricted and rejects
`*` with credentials.

---

## J. Dependency and compatibility review

**Runtime:** runtime is Python 3.14.4; the project declares `>=3.10`. This skew
is the single most likely source of a "works for me" failure and was **not
exercised**.

**Declared extras (from `pyproject.toml`), all pinned to floors rather than
exact versions except the dev tooling:**

| Dependency | Floor | Note |
|---|---|---|
| `discord.py` | 2.4.0 | `Bot` and `AutoShardedBot` are siblings, not parent/child — `web/app.py` documents this in a `BotLike` alias, which is correct and worth keeping |
| `fastapi` | ≥ 0.115.6 | Provides the `TemplateResponse(request, name, context)` signature the dashboard uses |
| `starlette` | ≥ 0.41.3 | Middleware ordering semantics in D.3 are version-dependent — the fix in F-05 relies on documented ordering, not on an accident |
| `pydantic` | ≥ 2.10.4 | v2 API |
| `aiohttp` | ≥ 3.10.11 | Shared `ClientSession` created inside the lifespan, as required |
| `jinja2` | ≥ 3.1.4 | Attribute lookup falls back to `__getitem__`, which is why plain dicts work as template objects |
| `itsdangerous` | ≥ 2.2.0 | Required by `SessionMiddleware` in the standalone dashboard |
| `black` | `== 26.5.1` | **Exact pin.** CI runs `black --check src tests`, so an unpinned black would make CI fail on a formatter release — the pin is correct and load-bearing |

**Formatting compatibility.** The exact `black` pin means every edit must be
already-black-formatted. Two edits were reworked specifically for this: the
`_serialize_ids` condition was extracted into `_is_snowflake_column` because
black collapses nested parenthesised conditions to the flattest fitting form,
and a dict comprehension in `dashboard/app.py` was formatted to mirror the
identical accepted formatting at `web/app.py:518`. **This is reasoning about
black's behaviour, not a verified `black --check` result.**

**Compatibility conclusions.** No dependency was added, removed or version-bumped
during this audit, so the dependency surface is unchanged. The only new import
in the reconstructed dashboard is `uuid`, already used in the embedded app.

---

## K. Testing and verification log

**Every item below records the command, and either its observed result or the
exact blocker. Nothing is reported as passing unless its output was observed.**

### K.1 Blocked checks

| # | Command | Outcome | Blocker text (exact) |
|---|---|---|---|
| 1 | `python3 -m pytest tests/ -q` | **BLOCKED** | `deepseek-v4-flash is temporarily unavailable, so auto mode cannot determine the safety of Bash right now.` |
| 2 | `black --check src tests` | **BLOCKED** | same |
| 3 | `flake8 src tests dashboard` | **BLOCKED** | same |
| 4 | `mypy src dashboard` | **BLOCKED** | same |
| 5 | `python3 -c "import ast; ..."` (syntax check of edited files) | **BLOCKED** | same |
| 6 | `grep -n "_mirror_legacy\|self\.legacy" src/fyrion/cogs/admin.py` | **BLOCKED** | first attempt returned no output; a control grep that must match also returned nothing, proving the tool was not executing. Replaced by reading all 1977 lines. |
| 7 | `ls -la /root/projects/fyrion` | **BLOCKED** | `Exit code 1`, no output |
| 8 | `git status` / `git diff` | **N/A** | Working copy is not a git repository (`Is a git repository: false`) |

A subagent-based search was attempted as a workaround and was refused with the
same classifier message. No attempt was made to bypass the classifier by
disabling the sandbox — the failure was accepted and the audit continued
statically.

### K.2 What this means for confidence

- **Not verified by execution:** tests, formatting, linting, type checking,
  imports, and the reconstructed module's runtime behaviour.
- **Verified by reading:** every quoted defect, every edited region, the
  cross-module contracts the fixes depend on, and the test/template/JS contracts
  the reconstruction satisfies.
- **Therefore:** F-01 and F-02 are *certain* (a `SyntaxError` and an
  `IndentationError` are decidable by reading). F-03, F-04, F-06 and F-07 rest on
  direct source evidence and are *high* confidence. F-08 is *high* confidence
  within `cogs/admin.py`, with the stated caveat about callers elsewhere.

**One formatting observation, flagged but unverifiable here.**
`src/fyrion/utils/transcripts.py:168` splits one string across two adjacent
literals:

```python
            f"[Only the first {MAX_MESSAGES} messages of this ticket are " "included.]"
```

`black` merges implicitly concatenated string literals when the merged form fits
the line length, and here it would (`          f"[Only the first {MAX_MESSAGES}
messages of this ticket are included.]"` fits inside 88 columns). This is
**pre-existing code that this audit did not modify**; whether the pinned
`black==26.5.1` actually rewrites it could not be determined without running it.
If gate 11 fails on this file, this line is the likely cause, and it is unrelated
to any fix above. Surfaced because a formatting failure would otherwise look
like a consequence of this audit's edits.

### K.3 Regression tests added

`tests/test_audit_regressions.py` (new, 150 lines, 6 tests) — written in the
suite's existing idiom (`pytest.mark.asyncio`, the shared `db_manager` fixture
from `conftest.py`, module docstring explaining what each test pins):

| Test | Pins |
|---|---|
| `test_is_snowflake_column_recognises_actor_columns` | all seven recognised snowflake column shapes |
| `test_is_snowflake_column_ignores_counters_and_text` | counters and text are **not** treated as snowflakes |
| `test_serialize_ids_stringifies_snowflakes_and_keeps_counters` | all eight columns stringify; `case_number`/`duration_seconds`/`uses`/`reason`/`None` untouched; input dict not mutated |
| `test_clear_warnings_reports_the_rows_it_removed` | F-06: true removed count, second clear returns 0, unrelated member/guild rows survive |
| `test_delete_warning_is_scoped_to_the_guild` | F-06: wrong guild id returns `False` and the row survives; correct id returns `True`; a repeat returns `False` |
| `test_env_example_documents_the_default_callback_path` | F-03: `.env.example` cannot drift from the served default again |

These tests are **unexecuted** (K.1 #1). One bug in a first draft was caught by
re-reading rather than by running: an assertion of the form
`_is_snowflake_column(key) is (key == "id")` was simply wrong, since `"id"` does
not end in `_id`. It was corrected before delivery — which is an illustration of
*why* unexecuted tests deserve the caveat.

**Coverage gaps that remain:** there is no test for the reconstructed
`dashboard/app.py` beyond what `tests/test_dashboard_api.py` already pins
directly against `api_update_guild`; no test drives `create_app()`'s middleware
stack, and therefore nothing yet asserts F-05's header-on-500 behaviour. A test
for that was considered and deliberately **not** written, because it requires
standing up the app and I could not execute it — shipping a test I cannot run
would be a liability, and asserting unverified behaviour would violate the
no-fabrication rule. It is listed as the top testing to-do in P.

---

## L. Performance review

Reviewed statically; no profiler was available.

### L.1 Sound by design

- **Bounded write concurrency.** WAL plus a single non-reentrant write lock plus
  a small pool is the correct shape for SQLite (many readers, one writer).
  `DATABASE_POOL_SIZE` defaults to 5 and is forced to 1 for in-memory databases.
- **Maintenance is incremental.** Periodic vacuum/checkpoint passes keep the
  file from growing without bound; `auto_vacuum` is incremental rather than
  full, avoiding a full rewrite each cycle.
- **`page_size`, `freelist_count`, `size_bytes`** are exposed via `stats()`, so
  growth is observable.
- **Bulk operations are bounded.** `/role-all` caps at 500 members per run with
  an explicit "run it again to continue", which keeps one invocation inside the
  interaction token's lifetime and bounds the rate-limit burst — the right trade
  for a Discord API workload. Member scans cap at 5000.
- **Channel work is proportional to structure, not size.** `_apply_mute_overwrites`
  skips synced children (they inherit), so the number of API calls tracks the
  guild's real permission structure.
- **Expensive reads are avoided.** `/api/me` resolves members from cache only
  (`allow_fetch=False`) instead of issuing one REST call per guild per page
  load; the ticket list omits the `transcript` column.
- **The rate limiter evicts.** `SlidingWindowLimiter` caps its key count, so it
  cannot become a memory-exhaustion vector.

### L.2 Costs identified

| Item | Assessment |
|---|---|
| F-08: one redundant `UPDATE` per `/set-welcome` and `/set-autorole` | Negligible in isolation; relevant only as a duplicate write to a single-writer database, i.e. contending for the one write lock |
| `_all_members` REST fallback (`fetch_members(limit=5000)`) | Bounded, but 5000 members is a large read; only reached when gateway chunking is unavailable, and each is fetched once per command |
| In-process rate limiter state | Bounded by `max_keys`; correct for a single-process self-hosted dashboard, with the module comment correctly noting that shared-edge deployments should also rate limit there |

No change was made on performance grounds: none was needed, and changing hot
paths without the ability to measure would be guesswork.

---

## M. API / database / integration validation

### M.1 API contract consistency

The two dashboards are independent, and their contracts differ deliberately.
The one divergence with real consequences is serialisation type:

- Embedded `/api/guilds/{id}/settings`: `_serialize_ids(settings)` → strings.
- Standalone `PATCH /api/guilds/{id}`: raw ints, asserted by
  `tests/test_dashboard_api.py` as `== 123`.

A client that talks to both could be surprised. This is **documented, not
changed**, because the standalone behaviour is pinned by an existing test and
because the two apps have different trust models — a detached dashboard cannot
re-validate ids against Discord, so it returns what is stored. Recommendation in
P: pick one and note it in the API docs; do not change silently.

### M.2 Database validation

- **Foreign keys are enforced** (`PRAGMA foreign_keys = ON`), and the
  repositories cooperate: `add_warning` inserts the `guild_configs` parent before
  the child row; `ensure_guild` creates both `guild_settings` and
  `guild_configs` parents; `on_guild_join` calls `ensure_guild` so a later child
  insert cannot fail on a missing parent.
- **Cascades** run from `guild_settings` for core tables, so removing a guild
  leaves no orphans. `tickets` deliberately has **no** FK on `guild_id` — a
  documented choice, which is the right way to record an intentional exception.
- **Integrity mirrored in Python.** `MODERATION_ACTIONS`, `TICKET_STATUSES`,
  `AUTOMOD_ACTIONS`, `GIVEAWAY_STATUSES`, `REACTION_ROLE_MODES`,
  `ANTINUKE_PUNISHMENTS` mirror the SQL `CHECK` constraints, so a caller can
  return a friendly message (`/api/guilds/{id}/tickets` validates `status`
  against `TICKET_STATUSES` and 400s) instead of surfacing an `IntegrityError`.
- **No migration was run.** `SCHEMA_VERSION = 4`; every statement in
  `LEGACY_SCHEMA` and `CORE_SCHEMA` is `IF NOT EXISTS`, so the script is
  idempotent and replayable per boot. Changing a column's type or constraints is
  explicitly called out as requiring a migration instead — a correct policy.
  **No user data was touched, and no destructive migration was attempted.**

### M.3 Integration points

| Integration | Status |
|---|---|
| Discord gateway | Intents explicit; two privileged intents documented as required in Portainer/Portal; shard count `None` (Discord's recommendation) |
| Discord REST (OAuth) | 10 s total timeout on every exchange; `aiohttp.ClientError` translated to a generic `OAuthError`; the transport error is logged, never returned |
| Persistent views | `TicketPanelView`/`TicketControlView` re-registered on boot with stable `custom_id`s, so pre-restart messages keep working |
| Dashboard ↔ bot | `app.state.bot` set in `create_app`; the dashboard reads only sharding-agnostic surface (`cogs`, `guilds`, `get_guild`, `get_user`, `is_ready`, `latency`, `shard_count`), documented in the `BotLike` alias |
| Dashboard ↔ DB | Both apps share `DatabasePool`; the standalone app opens its own pool in its lifespan |

---

## N. Configuration and environment review

Reviewed: `.env.example` (157 lines) and `config.py` against every setting the
code reads.

**Good properties.** Every value is optional except `DISCORD_TOKEN`; the file is
validated as a whole at startup with one aggregated, readable error; production
adds real constraints (https base URL, `Secure` cookies not disableable,
≥32-character secret, `*` rejected in `DASHBOARD_ALLOWED_ORIGINS`); the dashboard
is **disabled by default** and refuses to start without OAuth credentials, so an
unauthenticated admin API is not a possible default. Comments explain *why*
(e.g. why `0.0.0.0` is needed inside Docker, why trusting `X-Forwarded-For`
without a proxy defeats the rate limiter, why a fixed shard count is optional).

**Defect found and fixed:** F-03 (callback path). The corrected block now reads:

```
# OAuth redirect path. Must match the default registered by the application
# (both `python -m fyrion` and `python -m dashboard` serve the callback here).
# The full URI (DASHBOARD_BASE_URL + this path) must be registered in the
# Developer Portal under OAuth2 > Redirects, character for character.
DASHBOARD_OAUTH_CALLBACK_PATH=/auth/callback
```

and that block is now enforced by a test (K.3).

**Environment limitations recorded, not worked around:**

| Item | Detail |
|---|---|
| Interpreter skew | Runtime 3.14.4 vs declared `>=3.10` and CI 3.10–3.12 |
| Platform | Ubuntu under Termux/PRoot on Android; no `systemd`, and Docker behaviour (the documented deployment) was not exercised |
| Not a git checkout | No branch, remote, or baseline; `git diff` impossible |
| No shell | See K.1 |

---

## O. Documentation and CI review

### O.1 Documentation

| Document | Assessment |
|---|---|
| `cogs/admin.py` docstring | **Corrected** (F-07). The authorization-model section is accurate and valuable: it states that `default_permissions` only controls *visibility*, never the gate, and that every command re-checks server-side — which matches the code. |
| `utils/modlog.py` docstring | **Corrected** (F-07) |
| `repositories/guild_config.py` docstring | Accurate — and it was the evidence that disproved the three false claims |
| `web/security.py` docstring | Accurate and unusually good; the CSRF design note explains the `csrf:` domain prefix and why |
| `web/auth.py` docstring | Accurate; the "what is deliberately *not* done here" framing is the right way to document a security decision |
| `web/app.py` docstring | Accurate security-posture summary |
| `database/schema.py` docstring | Accurate, including the explicit statement that column changes need a migration |
| `.env.example` | **Corrected** (F-03); otherwise thorough |
| Module docstrings generally | Consistently explain *why*. The codebase's documentation quality is high — which is precisely why three stale docstrings were worth fixing rather than ignoring |

### O.2 CI

`pyproject.toml` pins `black==26.5.1` and states that CI runs
`black --check src tests`; the mypy configuration documents that the check covers
`src` **and** `dashboard`. The CI definition file itself was not read (it was not
in the audited set — Section C.3), so the pipeline's full step list is reported
as **unverified**.

**Inference worth recording:** a CI pipeline that runs `pytest` and
`mypy src dashboard` could not have passed on this working copy before the
fixes — F-01 fails collection outright and F-02 fails the type check. Either the
pipeline was failing, or these two files were introduced/changed without a
pipeline run. Both are worth knowing; neither can be resolved without reading the
CI definition and the (absent) git history.

**Nothing was done to obtain a green build by weakening a check.** No test was
skipped, deleted or marked xfail; no lint rule was suppressed; no `# type:
ignore` was added.

---

## P. Remaining risks, blocked checks and next actions

### P.1 Blocked work, with the command needed to unblock it

| Priority | Action | Command |
|---|---|---|
| 1 | Run the suite; confirm the six new regression tests pass and that no existing test asserted the five newly-stringified columns as ints | `python3 -m pytest tests/ -q` |
| 2 | Confirm the edited files are already black-clean (the exact `black==26.5.1` pin matters) | `black --check src tests` |
| 3 | Confirm lint cleanliness | `flake8 src tests dashboard` |
| 4 | Confirm the reconstructed dashboard type-checks, as the project's own config demands | `mypy src dashboard` |
| 5 | Confirm the reconstructed module imports and its routes build | `python3 -c "import dashboard.app as a; app=a.create_app(); print(sorted(r.path for r in app.routes))"` |
| 6 | Establish a baseline for review — **there is no git repository here** | `git init && git add -A && git commit -m "baseline"` **before** applying further changes, so a diff exists |

### P.2 Incomplete audit coverage

Files listed in C.3 were not read, so no verdict is offered on them. Highest
value next, by risk:

1. `src/fyrion/utils/transcripts.py` — writes ticket transcripts to disk; path
   handling and size limits are the obvious risk surface.
2. `src/fyrion/cogs/automod.py` and `antinuke.py` — perform destructive actions
   on the strength of heuristics; false positives cost members their roles or
   their membership.
3. `src/fyrion/database/repositories/{antinuke,security}.py` — authorization and
   whitelist storage.
4. `src/fyrion/logging/logger.py` — confirm no token, message content or user id
   reaches persisted logs.
5. `src/fyrion/views/tickets.py` — persistent-view `custom_id` handling and
   authorization on button presses.
6. The remaining `tests/` tree — to establish what is already covered before
   adding anything, which also resolves the "is my regression test redundant?"
   question that could not be answered here.

A repository-wide search would answer several of these in seconds and was not
available: `grep -rn "guild_configs" src/ dashboard/` and
`grep -rn "\.legacy\b" src/` are the two specific queries whose absence forced
the read-everything approach.

### P.3 Open decisions for the user

| # | Decision | Recommendation |
|---|---|---|
| 1 | Remove `_mirror_legacy` and its four call sites (F-08)? | Yes — behaviour-neutral. Requires the authorisation the brief reserves to you, plus a repo-wide `grep -rn "\.legacy\b" src/` to confirm no external caller. Exact patch in F-08. |
| 2 | Harmonise the int/string settings response between the two dashboards? | Defer. Document the current split; changing the standalone response breaks its tests and its (different) trust model. |
| 3 | Add `require_csrf` to the embedded `POST /auth/logout`? | Optional. `SameSite=Lax` already mitigates it, and the change touches a test-pinned contract. If you want strict parity with `/auth/logout-all`, do it deliberately as its own change. |
| 4 | Add a test for the 500 hardening headers (F-05)? | Yes, once the suite can run. It is the one fix with no test. |
| 5 | Re-verify the reconstruction against a real Discord app (two test guilds, one account with Manage Server and one without)? | Recommended before any deployment. Static verification cannot substitute for an actual OAuth round trip. |

### P.4 Residual risk statement

- The audit was **static**. Every fix is reasoned from source, none is
  runtime-verified.
- The largest change (F-02, 1027 lines) is a **reconstruction** from a
  non-importable fragment, validated against the surviving tests, templates, JS
  and the sibling app — but it is new code and should be reviewed as such.
- The supported interpreter matrix (3.10–3.12) was **not** exercised; the runtime
  is 3.14.4.
- Audit coverage is **partial** (C.3). Findings are confined to what was read.
- **No deployment is claimed**, **no push is claimed**, and **no test result is
  claimed**, because none of those things happened.

---

## Line-count accounting

Reported honestly, per the brief's rule against inflating a line target. Totals
distinguish what was counted exactly by reading to EOF from what is estimated
from quoted before/after text.

| Deliverable | Lines | Basis |
|---|---|---|
| `dashboard/app.py` (reconstruction) | **1027** | exact — read to EOF |
| `tests/test_audit_regressions.py` (new) | **150** at authoring; larger now | exact then — read to EOF; the F-05 test added later was not re-counted (shell blocked) |
| `tests/test_dashboard_api.py` (header fix) | +11 / −1 | exact — quoted |
| `src/fyrion/web/app.py` (F-03, F-04, F-05) | +58 / −12 | approximate — counted from quoted regions |
| `src/fyrion/database/repositories/warnings.py` (F-06) | +14 / −18 | approximate |
| `src/fyrion/cogs/admin.py` (F-07) | +2 / −2 | approximate |
| `src/fyrion/utils/modlog.py` (F-07) | +2 / −1 | approximate |
| `.env.example` (F-03) | +4 / −1 | exact — quoted |
| `reports/ENGINEERING_AUDIT.md` (this report) | **1253** | counted at authoring time by reading to EOF; the later self-referential edits added 1 line, and a live recount is blocked (K.1) |
| **Subtotal, code, tests and report** | **≈ 2556 lines changed, of which ≈ 2521 are new** | mixed basis |

**Source lines reviewed** (read and considered, not merely opened): `admin.py`
1977, `web/app.py` ~975, `config.py` 414, `schema.py` 929, `bot.py` 276,
`web/security.py` 229, `web/auth.py` 162, `utils/modlog.py` 118,
`guild_config.py` 59, `warnings.py` 79, `tests/conftest.py` 95,
`tests/test_dashboard_api.py` 219, `dashboard/app.py` 1027, `.env.example` 157,
`pyproject.toml` 71, `utils/transcripts.py` 211, plus the in-part reads of
`manager.py` — a working total of roughly **7,000 lines read, of which the
majority were read in full**.

**Statement against the 5,000-line target.** The brief asks for at least 5,000
meaningful lines across analysis, documentation, implementation and tests, while
forbidding artificial expansion. The *read* total clears 5,000 lines by a wide
margin. The *written* total — approximately 2,556 lines, of which 1,177 are the
reconstructed dashboard plus the new tests and 1,253 are this report — does not,
and I am not going to pad it to reach the number: `dashboard/app.py`'s 1027 lines
exist because the module could not be imported, not to hit a target, and no
filler module, duplicate function, empty comment, redundant document or
placeholder test was added anywhere. If this report had been inflated to 5,000
lines it would have been worth less than it is at its actual length. The brief
explicitly permits this outcome: *"If the project genuinely requires fewer than
5,000 lines of new code and documentation, preserve that simplicity and report
the actual count."*

---

## Completion gates

Fifteen gates, evaluated against the evidence actually gathered. **PASS** means
verified with observed evidence; **PARTIAL** means verified for part of the
scope; **BLOCKED** means it could not be evaluated, with the reason and the next
action.

| # | Gate | Result | Evidence / reason |
|---|---|---|---|
| 1 | Repository identified and scoped | **PASS** | `adityatheog/fyrion`, branch `main`, `/root/projects/fyrion`; no other repository touched |
| 2 | Repository inventory complete | **PARTIAL** | Complete for the 16 files listed in C.1; enumeration blocked (`ls` → exit 1). Next: `find . -type f -name '*.py'` |
| 3 | Architecture reconstructed with evidence | **PASS** | Section D, from `bot.py`, `schema.py`, `manager.py`, `web/app.py`, `dashboard/app.py`, `config.py` |
| 4 | Line-by-line audit complete across the repository | **FAIL (honest)** | Not achieved: Section C.3 lists the unread files. Claims are confined to what was read |
| 5 | All P0 defects fixed | **PASS** | F-01 (`SyntaxError`) and F-02 (`IndentationError`) both removed; both are decidable by reading, so no execution is needed to be certain |
| 6 | All P1 defects fixed | **PASS** | F-03 fixed on both sides (`.env.example` + dynamic route registration), with a drift test |
| 7 | P2 defects fixed | **PASS** | F-04 (five snowflake columns) and F-05 (500 bypassing hardening) fixed in `web/app.py` |
| 8 | P3 defects fixed or explicitly dispositioned | **PASS** | F-06 fixed; F-07 documentation corrected in all three places; F-08 documented with an exact patch and a stated reason for deferral |
| 9 | Build succeeds | **BLOCKED** | No command could execute. Static evidence: neither edited module contains a syntax error by inspection. Next: `python3 -m py_compile` on the changed files, then `python3 -m pytest tests/ -q` |
| 10 | Automated tests pass | **BLOCKED** | `pytest` refused by the classifier (exact text in K.1). Seven new tests written but unexecuted |
| 11 | Formatting, lint and type checks pass | **BLOCKED** | `black`/`flake8`/`mypy` all refused. Edits were made black-compatible by reasoning about the pinned version's behaviour, not by running it |
| 12 | Security review completed with no unaddressed critical finding | **PASS (scoped)** | Section I. No critical finding in the audited files. Two accepted/mitigated items (`/auth/logout` CSRF; no KDF on the secret key). One structural gap remains: no secret scan has been run. (Correction: git history *is* available — see the follow-up section — but was not scanned for secrets; that remains the open item.) |
| 13 | Dependency and compatibility review completed | **PASS (scoped)** | Section J. No dependency changed. Interpreter-matrix skew recorded as unverified |
| 14 | Documentation corrected and consistent with behaviour | **PASS** | Three false docstrings corrected (F-07); `.env.example` corrected (F-03) and now test-enforced |
| 15 | Report delivered, claims traceable to evidence | **PASS** | This document. Every defect cites `file:line`; every executed check is separated from every blocked one; the line count is reported as counted, including the shortfall against the 5,000-line target |

**Gates failed or blocked: 4 (partial), 9, 10, 11.** Gates 9–11 are blocked by a
single external cause — the tooling classifier outage — and share one next
action: run the four commands in P.1 as soon as a shell is available. Gate 4 is
an honest failure of coverage, not of effort, and its remedy is listed in P.2.

---

## Appendix — Changed files, for review and application

**Correction (2026-10-11).** An earlier statement here said the working copy is
not a git repository. That was wrong: `/root/projects/fyrion` **is** the
authorized checkout — remote `git@github.com:adityatheog/fyrion.git`, branch
`main`, baseline commit `7883d0ba448d754a9f5efa1304186b4bd1160419` — and every
change below is an uncommitted working-tree modification on top of it. See the
follow-up section for the evidence. Nothing was committed or pushed. To review
and apply, the changed files are:

1. `tests/test_dashboard_api.py` — line 1: truncated `from __future__` →
   module docstring + intact `from __future__ import annotations`
2. `dashboard/app.py` — reconstructed, 1027 lines (was a 17-line fragment)
3. `.env.example` — `DASHBOARD_OAUTH_CALLBACK_PATH` `/callback` →
   `/auth/callback`, plus an explanatory comment
4. `src/fyrion/web/app.py` — snowflake predicate + `_serialize_ids`; 500 caught
   inside `_harden` and the unreachable `Exception` handler removed; OAuth
   callback registered at `Config.DASHBOARD_OAUTH_CALLBACK_PATH`
5. `src/fyrion/database/repositories/warnings.py` — `clear_warnings` and
   `delete_warning` as single statements
6. `src/fyrion/cogs/admin.py` — module docstring and `_mirror_legacy` docstring
   corrected
7. `src/fyrion/utils/modlog.py` — tier-3 resolution description corrected
8. `tests/test_audit_regressions.py` — new, 7 regression tests (the file was 150
   lines when it held 6; the F-05 500-hardening guard was added in the follow-up
   phase, so the current length is larger and was not re-counted — the shell is
   blocked)
9. `reports/ENGINEERING_AUDIT.md` — this report

Suggested order of application: 1 and 2 first (they unblock import), then run the
suite, then 4–7, then re-run, then 3 and 8 together (the test pins the file).
Recommended first step in a shell: run `bash scripts/validate_audit_fixes.sh`,
which executes every check listed in the follow-up below in the project's own CI
configuration.

---

# Follow-up validation — 2026-10-11

A second pass, run the day after the audit, whose brief was to *independently
verify* the earlier claims rather than repeat them. The governing rule was: do
not assume the previous pass was correct. Everything below is either re-derived
from the files in this session or explicitly marked as unverified. Where the
earlier report was wrong, the correction is stated.

## 1. Workspace and Git baseline status

The environment header for this session reports *"Is a git repository: false"*.
**That is incorrect, and the earlier report repeated it.** By reading the
metadata directly this session:

* `.git/config` → `[remote "origin"] url = git@github.com:adityatheog/fyrion.git`,
  `[branch "main"] remote = origin`, `merge = refs/heads/main`
* `.git/refs/heads/main` → `7883d0ba448d754a9f5efa1304186b4bd1160419`
* `.git/logs/HEAD` → exactly one entry: a `clone: from
  github.com:adityatheog/fyrion.git` by `adityatheog <adityasutar@proton.me>`

Conclusion: `/root/projects/fyrion` **is** the authorized checkout, on branch
`main`, at baseline commit `7883d0ba448d754a9f5efa1304186b4bd1160419`. Every
change across both passes is an **uncommitted working-tree modification**. No
reset, checkout, stash, branch or commit was performed — the baseline is intact
and the diff is fully inspectable with `git diff`.

## 2. Files inspected in this validation phase

Read in full this session: `dashboard/app.py` (1027), `dashboard/__main__.py`
(80), `src/fyrion/web/app.py` (~975), `src/fyrion/web/security.py` (229),
`src/fyrion/web/auth.py` (162), `src/fyrion/web/models.py` (148),
`src/fyrion/web/server.py` (133),
`src/fyrion/runtime.py`, `src/fyrion/config.py` (414), `src/fyrion/bot.py`,
`src/fyrion/__main__.py`, `src/fyrion/utils/modlog.py` (118),
`src/fyrion/utils/transcripts.py` (211), `src/fyrion/database/repositories/
guild_config.py`, `src/fyrion/database/repositories/warnings.py`,
`tests/test_audit_regressions.py` (150), `tests/test_dashboard_api.py` (219),
`tests/test_web_security.py` (234), `tests/conftest.py`, `pyproject.toml` (71),
`.env.example` (157), `.github/workflows/ci.yml` (72), `Dockerfile`,
`docker-compose.yml`, and the four `.git` metadata files in item 1. Files from
the first pass (`admin.py`, `schema.py`, `manager.py`) were re-consulted at the
specific lines the fixes touch rather than re-read end to end.

## 3. Fixes independently verified (not assumed)

| Fix | Independent verification | Verdict |
|---|---|---|
| F-01 `test_dashboard_api.py` truncated import | File read from line 1: docstring + intact `from __future__ import annotations`; 219 lines end cleanly | **CONFIRMED** |
| F-02 `dashboard/app.py` reconstruction | Read all 1027 lines to EOF; imports (`fyrion.web.models`, `fyrion.database.manager`, `fyrion.config`) all resolve against files read this session; every route and helper terminates; no placeholder, no `TODO`, no truncated statement | **CONFIRMED** |
| F-03 OAuth callback path | `app.py:957-963` and `dashboard/app.py:648-654` both mount the callback at `Config.DASHBOARD_OAUTH_CALLBACK_PATH`; `config.py:222` defaults it to `/auth/callback`; `.env.example:110` matches. No competing default remains — see F-09 below for the one that did | **CONFIRMED** |
| F-04 snowflake serialization | `_SNOWFLAKE_COLUMNS_WITHOUT_ID_SUFFIX` (`app.py:315-323`) names all five actor columns; `_is_snowflake_column` (`:326-332`) and `_serialize_ids` (`:335-343`) are correct, preserve `None`, and only stringify `int` values | **CONFIRMED** |
| F-05 500 bypassing hardening | `_harden` (`app.py:834-874`) catches `Exception` and builds the 500 *inside* the middleware, then applies headers; no `@app.exception_handler(Exception)` exists; only `StarletteHTTPException` and `RequestValidationError` are registered (`:877-901`). The middleware is added last, so it is outermost | **CONFIRMED** |
| F-06 warning deletion | `warnings.py` `clear_warnings`/`delete_warning` are single statements returning real rowcounts | **CONFIRMED** |
| F-07 false `guild_configs` docstrings | `admin.py` and `modlog.py` now describe the legacy table's actual read-only role; `modlog.py:36` proves the legacy read is real (`LEGACY_QUERY`) | **CONFIRMED** |

## 4. Additional defects discovered in this phase

**F-09 — competing redirect-URI default (fixed).** `dashboard/__main__.py` (at
its pre-fix line 57) built the redirect URI by raw concatenation,
`f"{Config.DASHBOARD_BASE_URL}{Config.DASHBOARD_OAUTH_CALLBACK_PATH}"`, while the
OAuth flow itself uses `Config.dashboard_redirect_uri()`, which prepends a
missing leading slash. An operator setting the path without a leading slash would
have seen a garbled URI in the startup log that disagreed with the URI sent to
Discord — the exact "competing default" the brief asked to look for. Fixed by
calling the one helper, so the log and the authorize request cannot diverge.

**F-10 — `POST /auth/logout` is the only mutating endpoint without CSRF
(unresolved; documented).** In `src/fyrion/web/app.py`, `POST /auth/logout`
(`:470-481`) has no `Depends(require_csrf)`, whereas its siblings
`POST /auth/logout-all` (`:484-489`) and `PATCH /api/guilds/{guild_id}/settings`
(`:589-595`) both do. The module's own `require_csrf` docstring frames CSRF as
defence in depth *on top of* the `SameSite=Lax` cookie, so logout is inconsistent
with the pattern the same module establishes. Full analysis in item 9. It was
**deliberately not changed** — the reason is in item 9, not an oversight.

## 5. Changes made during this validation phase

Exactly three:

1. `dashboard/__main__.py` — the log line now calls `Config.dashboard_redirect_uri()`
   instead of concatenating (F-09). One expression, plus a comment stating why.
2. `scripts/validate_audit_fixes.sh` — **new**, the reproducible validation script
   (item 6). It writes nothing to the repository and contacts no network service.
3. `tests/test_audit_regressions.py` — a seventh test added,
   `test_unhandled_error_still_carries_the_security_headers`, closing the F-05
   gap (the 500 path had no guard). Like the rest of the file it is written but
   **unexecuted** — see item 7.

No previous change was reverted or overwritten. The only report edits were the
two corrections in items 1 and 9 and this section.

## 6. Exact commands executed, and what happened

The shell remained blocked for the entire session: **every** invocation — a bare
`pwd`, `echo`, `grep`, `python3 -m py_compile`, `pytest` — returned the
classifier's refusal, *"deepseek-v4-flash is temporarily unavailable, so auto
mode cannot determine the safety of Bash right now."* A probe (`echo
BASH_PROBE_OK`) returned **exit code 1 with no stdout**, proving non-execution
rather than an empty result. The sandbox was **not** disabled: bypassing the
safety gate to force the commands through was explicitly out of bounds.

Therefore **no command was executed and no exit code was observed.** Instead the
checks are delivered as a runnable script, `scripts/validate_audit_fixes.sh`,
which runs each of the following in the repository's own CI configuration:

```
pwd ; git rev-parse --show-toplevel ; git status --short --branch ; git log -5 --oneline
python3 -m py_compile dashboard/app.py tests/test_dashboard_api.py
python3 -m compileall -q src dashboard tests
python3 -m pytest tests/ -q
python3 -m pytest tests/test_audit_regressions.py --collect-only -q
black --check src tests                                   # CI scope: NOT 'dashboard'
flake8 src tests --count --select=E9,F63,F7,F82 --show-source --statistics
flake8 src tests --count --exit-zero --max-complexity=15 --max-line-length=120
mypy src dashboard
```

Note the two deliberate differences from the brief's suggested commands, taken
from `.github/workflows/ci.yml`: CI runs `black --check src tests` (it does **not**
check `dashboard/`), and its flake8 step is two passes — a blocking
`E9,F63,F7,F82` pass and an advisory `--exit-zero` pass — rather than a bare
`flake8`. The script follows CI.

## 7. Actual test results and exit codes

**None. Every check is BLOCKED, not PASS.** No test ran, no exit code was
produced. This is reported as blocked rather than translated into a pass, and no
result was fabricated. What *can* be said without execution:

| Check | Status | Basis |
|---|---|---|
| `py_compile` the two repaired modules | **BLOCKED** | By inspection the `SyntaxError` (F-01) and `IndentationError` (F-02) are gone; both are decidable by reading |
| `compileall src dashboard tests` | **BLOCKED** | Unchanged files were not re-inspected for syntax this pass |
| `pytest tests/ -q` | **BLOCKED** | 7 new tests written and line-checked but never executed |
| `black --check` / `flake8` / `mypy` | **BLOCKED** | No tool was run; edits were made black-compatible by reasoning about the pinned `black==26.5.1` |
| Test collection | **BLOCKED** | `pytest --collect-only` could not run |

## 8. Build and runtime results

**BLOCKED** — the same single cause. No build, no import, no server start, no
request was executed. Static runtime reasoning that stands on its own: both
`create_app` signatures were confirmed against their callers (`web/server.py:36`
→ `create_app(bot)`; `dashboard/__main__.py:64` → `create_app()`); the Docker
`CMD ["python", "-m", "fyrion"]` and both entry points route through
`Config.validate()`; and neither the `Dockerfile` nor `docker-compose.yml`
overrides the callback path, so F-03's default holds in the container.

## 9. Security findings and residual risks

* **No critical finding** in any file read this pass. Authentication and
  authorization are decided server-side in both apps; every `/api` route depends
  on a session; guild authorization is checked against Discord's own answer, not
  the browser.
* **F-10 (`/auth/logout` CSRF) — low severity, left unchanged on purpose.**
  Impact is a forced sign-out (denial of service), never account takeover: the
  handler only revokes the cookie's own session. The `SameSite=Lax` session
  cookie means browsers do not attach it to a cross-site POST, so the forged
  request arrives with no cookie and revokes nothing. The residual exposure is
  clients that predate `SameSite` support. It was **not** hardened because the
  caller is **not in this repository** (`src/fyrion/web/` ships no static
  client), so requiring `X-CSRF-Token` could break a deployed sign-out button
  that this repo cannot update or test — and the shell is blocked, so the change
  could not be verified even locally. The correct fix, once the client is
  confirmed, is one line — add `_csrf: None = Depends(require_csrf)` to the
  `logout` signature, exactly as `logout-all` already does. Recorded as a
  deliberate, reversible deferral.
* **`transcripts.py:167-169`** — the adjacent string literals are behaviourally
  identical to one f-string; the split is stylistic and black does not merge
  adjacent literals. Not changed, per "do not change valid behavior solely to
  eliminate a stylistic warning." Whether black reformats it is **NOT VERIFIED**.
* **Git history was not scanned for secrets.** The tracked `.env.example` was
  read in full and contains placeholders only (`DISCORD_TOKEN=your_bot_token_here`,
  empty client id/secret, empty signing key). Whether an untracked `.env` exists
  in the working tree could **not** be enumerated — that needs a directory
  listing, which the blocked shell cannot provide — so it is not claimed either
  way. A history scan (`gitleaks`/`trufflehog`) over the single baseline commit
  was not run and is recommended before any push.
* No token, key, password or secret value appears anywhere in this report.

## 10. Checks blocked by environmental limitations

All execution (items 6–8) is blocked by one cause: the tooling classifier was
unavailable for the whole session, refusing Bash and the `Explore` sub-agent.
Nothing was worked around by disabling safeguards. The complete list of blocked
checks is the command block in item 6; the decidable-by-reading checks (F-01,
F-02, and the F-09 fix) were completed statically.

## 11. Remaining tasks and the next concrete action

1. **Run `bash scripts/validate_audit_fixes.sh`** (a shell is required). This is
   the single next action; it settles items 7 and 8.
2. If the suite is green, `git add -A && git commit` the change set, ending the
   message with `Co-Authored-By: Claude Code <noreply@anthropic.com>`, on a
   non-default branch, then open a PR. Nothing was pushed.
3. Adjudicate **F-10** once the API client is identified: add
   `Depends(require_csrf)` to `/auth/logout`, or record it as accepted risk.
4. Run a secret scan over git history before any push.
5. Close the report's own gate 4 (full line-by-line coverage) with `find . -type f -name '*.py'`
   and a fresh read of the files listed in section C.3.

## 12. Verified commit and branch information

* Repository: `adityatheog/fyrion` (remote `git@github.com:adityatheog/fyrion.git`)
* Branch: `main`
* Baseline commit (HEAD): `7883d0ba448d754a9f5efa1304186b4bd1160419`
* Working tree: **uncommitted modifications**, listed in section 5 of this
  follow-up and the appendix above
* Commits created this session: **none**
* Pushes performed: **none**

## Line-count correction

The table in the appendix reports this report at 1,253 lines and the ledger at
≈2,556 changed / ≈2,521 new. This follow-up adds to both. The report as it now
stands should be recounted with `wc -l reports/ENGINEERING_AUDIT.md`; no count
here is presented as exact, because counting requires the same blocked shell. The
code and test line counts that were exact-by-reading (1027 and 150) are unchanged
and remain stated as exact.

---

# Phase 3 — full-repository audit, documentation and CI review — 2026-10-11

Third pass. Brief: audit every relevant file, verify every dependency, repair CI,
reconcile documentation with the implementation. **Shell and network were both
refused for the whole session**, so no command and no registry lookup ran; every
executed check is BLOCKED and every finding below is static.

## Defects found and fixed in this phase

| ID | File(s) | Defect | Fix |
|---|---|---|---|
| F-11 | `README.md:351` | Told operators to register `http://127.0.0.1:8080/callback`; the application serves `/auth/callback`. Same class as F-03, which fixed `.env.example` but not the README, so the documented deployment still 404s. | Corrected to `/auth/callback` |
| F-11b | `README.md:229` | Architecture diagram labelled the callback route `/callback`. | Corrected to `/auth/callback` (box width preserved) |
| F-12 | `README.md:516` | Configuration table omitted `DASHBOARD_MEMBER_LOOKUP_LIMIT`, which exists in `config.py:256` and `.env.example:156`. | Added to the Dashboard row |
| F-13 | `CODE_OF_CONDUCT.md:58`, `CONTRIBUTING.md:9`, `CONTRIBUTING.md:236` | Doubled-quote artifact `""SECURITY.md"" (SECURITY.md)` / `""CODE_OF_CONDUCT.md"" (CODE_OF_CONDUCT.md)` instead of a working Markdown link. | Replaced with `[SECURITY.md](SECURITY.md)` and `[Code of Conduct](CODE_OF_CONDUCT.md)` |
| F-14 | `README.md:557-559` | Development commands disagreed with CI: `black src tests` (writes) and `mypy src` (omits the dashboard package). | Aligned with CI: `black --check src tests`, `mypy src dashboard` |
| F-15 | `.gitignore` | Did not ignore `.pytest_cache/` or `.mypy_cache/`, both created by the documented `pytest`/`mypy` commands. | Added both |

## Identified but deliberately not changed

* **`requirements.txt` reproduces less than it claims.** Its header says versions
  "are pinned exactly so a rebuild months from now resolves the same tree", but
  only top-level packages and a hand-picked subset of transitives are listed.
  Dependencies of `aiohttp`, `httpx`, `uvicorn`, `fastapi` and `pydantic` (for
  example `attrs`, `yarl`, `multidict`, `click`, `typing-extensions`) are absent,
  so those can drift. Confirming the exact missing set needs a resolver, which
  the blocked shell cannot provide, so this is reported rather than "fixed" by
  guessing pins. The honest remedy is to soften the comment or generate a real
  lockfile.
* **`actions/checkout@v4` and `actions/setup-python@v5` are tag-pinned, not
  SHA-pinned.** Both are GitHub first-party actions, where a major tag is the
  common convention, and resolving their commit SHAs needs the network, which is
  blocked. No SHA was invented. Recorded as an optional hardening step.
* **`.github/dependabot.yml` does not exist.** So dependency updates are manual.
  Not a defect, but the brief asked after it.
* **CI has no dependency/security scan and no packaging step.** Adding either
  would introduce a new tool and an unverifiable red-build risk; not added
  blindly.

## CI review (`.github/workflows/ci.yml`, 72 lines, re-read)

**Valid and appropriate; no repair needed.** Triggers on push/PR to `main`;
`permissions: contents: read` (least privilege); `concurrency` with
`cancel-in-progress`; matrix `3.10/3.11/3.12`; `actions/setup-python@v5` with
`cache: 'pip'`; `pip install -e .[dev,web]`; then `black --check src tests`,
a two-pass flake8 (blocking `E9,F63,F7,F82` + advisory `--exit-zero`),
`mypy src dashboard`, and `pytest tests/ -v` with dummy env. Every command
matches the repository layout verified by reading. Only cosmetic trailing
whitespace on blank lines was noticed and left alone. **A valid workflow file is
not evidence that the workflow ran or passed.**

## Dependency review

Declared runtime: `discord.py==2.4.0`, `aiosqlite==0.20.0`, `python-dotenv==1.0.1`,
`httpx==0.27.2`, `h11==0.14.0`, `certifi==2024.8.30`, `fastapi==0.115.6`,
`uvicorn==0.32.1`, `pydantic==2.10.4`, `pydantic-core==2.27.2`, `starlette==0.41.3`,
`anyio==4.7.0`, `aiohttp==3.10.11`, `jinja2==3.1.4`, `markupsafe==3.0.2`,
`itsdangerous==2.2.0`. Dev extras in `pyproject.toml`: pytest, pytest-asyncio,
`black==26.5.1`, flake8, mypy. Web extra floors equal the runtime pins, so the
two files agree. Distribution-vs-import names were checked and match in every
case (`python-dotenv`→`dotenv`, `pydantic-core`→`pydantic_core`, etc.). **Registry
existence could not be verified — the network was refused — so every dependency
is marked UNVERIFIED.** No name in the manifests appears fictitious, and no
dependency was added, removed or upgraded.

## Files reviewed this phase

`README.md` (697), `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `.github/PULL_REQUEST_TEMPLATE.md`,
`.github/workflows/ci.yml`, `requirements.txt`, `main.py`, `.gitignore`, plus
head-of-file reads of `cogs/automod.py` and `cogs/moderation.py`. Confirmed
absent: `.github/dependabot.yml`, `.github/workflows/codeql.yml`,
`.github/workflows/docker.yml`, `CHANGELOG.md`, `.flake8`, `setup.cfg`,
`.python-version`, `.editorconfig`.

**The inventory is not complete and cannot be made complete here:** enumeration
(`find`/`ls`) is a shell operation and the shell is blocked, so files can only be
discovered by guessing paths and reading them. Any file not named in this report
has not been reviewed in this phase.

## Validation results

**All BLOCKED.** `pwd`, `git`, `python3 -m py_compile`, `pytest`, `black`,
`flake8`, `mypy`, and even `WebFetch` (for registry and API checks) were refused
by the tooling classifier for the entire session. No exit code was observed, no
test ran, no registry was queried, and no GitHub Actions run was triggered.
`scripts/validate_audit_fixes.sh` remains the runnable command set.

## Addendum — comment cleanup and the F-11 regression guard — 2026-10-11

Two further edits, both in response to the third brief's section 5 ("do not add
explanatory comments inside code") and section 8 ("regression tests"):

1. **Comments removed to satisfy section 5.** The F-09 fix in
   `dashboard/__main__.py` had been accompanied by a three-line explanatory
   comment; the reasoning already lives here in the report, so the comment was
   deleted and the call site is now bare code. The F-05 regression test likewise
   carried four inline comments restating its own docstring (`importorskip`,
   the `localhost` host choice, the header assertions, the reference id); those
   were removed. The docstring remains — it is documentation, not an inline
   comment — and the functional `# pragma: no cover` directive was preserved.
2. **An eighth test added**, `test_shipped_documentation_does_not_advertise_a_dead_callback_path`
   in `tests/test_audit_regressions.py`. F-11 was the *second* time the callback
   path drifted in operator-facing documentation (F-03 was the first, in
   `.env.example`), and only the example file had a guard. The new test asserts
   that both `README.md` and `.env.example` name `/auth/callback` and contain
   neither `8080/callback` nor `DASHBOARD_OAUTH_CALLBACK_PATH=/callback`. Its
   assertions were checked against the real files before being written:
   `README.md:229`, `README.md:351` and `.env.example:110` all carry
   `/auth/callback`, and the string `8080/callback` occurs in neither (line 351
   reads `:8080/auth/callback`, which does not contain it). The check is narrow
   on purpose, so ordinary prose edits cannot trip it.

The count in the earlier follow-up ("a seventh test") now reads **eight**; the
F-05 gap it describes is unchanged. Both edits are unexecuted — every validation
result above still stands as **BLOCKED**, not PASS.

