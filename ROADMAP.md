# Fyrion Production Overhaul Roadmap

Audit date: 2026-09-18. Target: `discord.py` bot at `/home/dev/workspace/fyrion`.

This roadmap is written for the Developer to execute and the Reviewer to verify.
Every phase lists exact files, the change it must accomplish, and acceptance
criteria. File:line citations point at the current state that motivates each
change.

## Audit summary (state at time of writing)

The codebase is further along than a greenfield build. Two data-access layers
coexist, the schema carries a deliberate legacy tier, and several "to build"
features already exist and only need consolidation. Findings, grouped:

- **Two DB layers.** `src/fyrion/database/manager.py` (`DatabasePool`) is the
  real, WAL-tuned, pooled implementation and is what the bot uses
  (`src/fyrion/bot.py:26`, `:114`). `src/fyrion/database/connection.py`
  (`DatabaseManager`) is a single-connection helper with **no WAL, no pool, no
  busy_timeout** (`connection.py:18-45`). It is **never instantiated anywhere**
  (grep for `DatabaseManager()` returns nothing); three repositories only import
  it for a type hint (`repositories/invites.py:5`, `repositories/security.py:5`,
  `repositories/guild_config.py:6`). At runtime those repos receive the pool via
  `bot.db`. So `connection.py` is dead code that misrepresents the DB config.
- **WAL is correctly enabled -- in the pool only.** `manager.py:236-240` sets
  `journal_mode=WAL`, `synchronous=NORMAL`, `wal_autocheckpoint=1000`,
  `busy_timeout` (`:233`), `foreign_keys=ON` (`:232`), `temp_store=MEMORY`
  (`:234`), plus incremental auto-vacuum (`:244-276`) and a write lock
  (`manager.py:170`, `:359`). Config is wired (`config.py:174-182`). The
  **appearance** of a WAL problem comes entirely from the stale `connection.py`.
- **Schema duplication across three locations.** See Phase 2 for the itemized
  list. In short: `guild_configs` (legacy) vs `guild_settings` (core) overlap on
  `welcome_channel_id`/`log_channel_id`/`autorole_id`; `automod_configs`
  (legacy) vs `automod_rules` (core); `ticket_configs` (legacy) vs
  `guild_settings.ticket_*`; `warnings` (legacy) vs `moderation_cases`
  (core, action `warn`); and a **third** schema block lives inside the economy
  cog (`cogs/economy.py:86-122`, `SCHEMA_STATEMENTS`).
- **Security bug (test-confirmed).** `DatabasePool.fetch_one`, `fetch_many`,
  `count`, `exists` skip the table allow-list when `where`/`columns` are empty,
  because `_where_clause` returns early (`manager.py:456-457`) and
  `_select_columns` returns `*` (`manager.py:508-509`) without ever calling
  `_columns_for`. An unknown/injected table name reaches SQLite instead of
  raising `ValueError`. `test_unknown_table_and_column_are_rejected`
  (`tests/test_database_manager.py:68-74`) fails on exactly this. `_quote`
  (`manager.py:123-131`) only blocks embedded `"`, so the injected string is
  neutralized as a quoted identifier rather than executed -- but the contract is
  violated and the test is red.
- **Error handling is already a clean global tree.** The `ErrorHandler` cog
  (`cogs/error_handler.py`) installs one `bot.tree.on_error`, renders clean
  ephemeral embeds, separates expected vs unexpected, adds a reference id, and
  restores the prior handler on unload (`:311-322`). The older
  `errors/handler.py` (`setup_error_handlers`, installed at `bot.py:138`) is a
  primitive precursor; the cog saves and supersedes it (`error_handler.py:318`).
  Both register `on_command_error` -- harmless duplication, but redundant.
- **Emoji usage is already safe-ish.** No hardcoded `<:name:id>` literals exist
  anywhere (grep is empty). Emoji are unicode escapes (`\U0001...`) or
  `PartialEmoji`. `admin.py:110-140` and `utils/tickets.py:110-125` validate
  custom emoji against `bot.get_emoji`. What is missing is a single shared
  resolver with unicode fallbacks; the logic is duplicated in two places.
- **Anti-nuke is genuinely absent.** `cogs/logging.py` only *logs* bans, kicks,
  channel deletes and role deletes (`logging.py:332`, `:361`, `:382`); there is
  no threshold detection, no actor tracking, no auto-response. `automod.py`
  covers per-member message spam via a token bucket (`automod.py:155-196`,
  `:515-556`) but nothing about mass moderation actions. This is the largest
  real gap.
- **Economy is atomic already.** Debits/credits go through
  `adjust_balance` whose guard is in the SQL (`manager.py:960-1008`),
  transfers are single-transaction (`manager.py:1010-1040`), deposits/withdraws
  use conditional UPDATEs (`economy.py:710-721`), and wagers debit up front
  (`economy.py:2030-2064`). `/gamble` (coinflip, `economy.py:2070-2129`),
  `/slots` (`:2135`) and `/blackjack` (`:2387`, with `BlackjackView`
  `:1071-1214` and `settle_blackjack` `:2326`) all exist. This item is about
  hardening and test coverage, not construction.
- **Test baseline: 70 pass, 1 fail.** The one failure is the validation bug
  above. Tests require `pip install -e .` first (package `fyrion` must be
  importable; `tests/conftest.py:18`).

Priority order for the Developer: **Phase 0 (security fix) first**, then Phase 5
(anti-nuke, the real feature gap), then the consolidation phases (1-4), then
Phase 6-7 hardening. Phases 1-4 are mostly deletion and merging, low-risk but
high-clarity.

---

## Phase 0 -- Fix the identifier-validation bypass (security, blocking)

**Why first:** a red test on a security control, and the generic CRUD helpers
are the foundation every other phase leans on.

Files to modify:
- `src/fyrion/database/manager.py`

Changes:
1. Make table validation unconditional in every public generic helper. In
   `fetch_one` (`:629`), `fetch_many` (`:647`), `count` (`:712`) and `exists`
   (`:720`), call `self._columns_for(table)` (or an equivalent
   `self._check_table(table)` helper) **before** building any SQL, so an unknown
   table raises `ValueError` even when `where` is empty and `columns` is `None`.
2. Add a small `_check_table(table)` classmethod next to `_columns_for`
   (`:434-439`) that raises `ValueError` for unknown tables, and call it at the
   top of `insert`, `upsert`, `update`, `delete`, `increment`, `fetch_one`,
   `fetch_many`, `count`, `exists`. (Several already validate via
   `_check_columns`; the gap is only the no-column/no-where paths.)
3. Do not weaken `_quote` -- keep it as defense in depth.

Acceptance criteria:
- `tests/test_database_manager.py::test_unknown_table_and_column_are_rejected`
  passes.
- Full suite: `pip install -e ".[dev]" && python -m pytest -q` -> **71 passed,
  0 failed**.
- `pool.fetch_one("nonexistent_table", {})` raises `ValueError` (add a test).
- No SQL string is ever built from an unvalidated table name (Reviewer greps
  each helper for a `_check_table`/`_columns_for` call before the f-string).

---

## Phase 1 -- Collapse to one WAL-optimized async connection helper

**Goal:** one connection layer. `DatabasePool` in `manager.py` is already it;
the work is removing the misleading duplicate and retargeting stale imports.

Files to modify / delete:
- Delete `src/fyrion/database/connection.py` (dead: never instantiated; only
  imported for type hints at `repositories/invites.py:5`,
  `repositories/security.py:5`, `repositories/guild_config.py:6`).
- `src/fyrion/database/repositories/invites.py` -- change the type hint from
  `DatabaseManager` to `typing.Any` (matching `warnings.py:17` and
  `tickets.py`), or to a `Protocol` (see below). Remove the
  `from fyrion.database.connection import DatabaseManager` import (`:5`).
- `src/fyrion/database/repositories/security.py` -- same (`:5`).
- `src/fyrion/database/repositories/guild_config.py` -- same (`:6`).
- Optional but recommended: add `src/fyrion/database/protocol.py` defining a
  `DBLike` `typing.Protocol` (methods `execute`, `fetchrow`, `fetchall`,
  `fetchval`, plus the domain helpers repos actually call) so repositories get a
  real type instead of `Any`.

Verify (no change needed, just confirm during review):
- `manager.py:236-240` enables WAL/synchronous/autocheckpoint for on-disk DBs
  and correctly skips them for in-memory (`:236`).
- `manager.py:232-234` sets `foreign_keys`, `busy_timeout`, `temp_store` on
  every connection.
- `bot.py:134-136` connects the pool and starts maintenance.

Acceptance criteria:
- `grep -rn "database.connection" src/ tests/ dashboard/` returns nothing.
- `connection.py` no longer exists.
- Full test suite still green (repos still work against the pool via `bot.db`).
- `DatabasePool.stats()` (`manager.py:1511`) reports `journal_mode == "wal"`
  for an on-disk database (add/confirm a test using a temp file DB, since the
  existing pool fixture is in-memory).

---

## Phase 2 -- Schema de-duplication

**Goal:** one authoritative schema module, no overlapping tables, economy DDL
moved out of the cog.

### 2a. Itemized duplications (each is a finding to resolve)

| # | Duplicate A | Duplicate B | Overlap |
|---|-------------|-------------|---------|
| 1 | `guild_configs` `schema.py:51-57` (legacy) | `guild_settings` `schema.py:139-176` (core) | `welcome_channel_id`, `log_channel_id`->`mod_log_channel_id`, `autorole_id`, `anti_link_enabled` |
| 2 | `automod_configs` `schema.py:84-99` (legacy) | `automod_rules` `schema.py:222-249` (core) | whole-guild automod toggles vs per-rule rows |
| 3 | `ticket_configs` `schema.py:101-106` (legacy) | `guild_settings.ticket_category_id`/`ticket_log_channel_id` `schema.py:156-158` | ticket category + log channel |
| 4 | `warnings` `schema.py:59-67` (legacy) | `moderation_cases` action `'warn'` `schema.py:188-209` | warning history |
| 5 | economy DDL in `cogs/economy.py:86-122` (`SCHEMA_STATEMENTS`: `economy_shop_items`, `economy_cooldowns`) | none in `schema.py` | schema defined outside the schema module |

Note the dual-write today: `manager.ensure_guild` inserts into **both**
`guild_settings` and `guild_configs` (`manager.py:758-766`) precisely because
the legacy repos still read `guild_configs`. That coupling is the thing to
break.

### 2b. Target state

Files to modify:
- `src/fyrion/database/schema.py` -- move `economy_shop_items` and
  `economy_cooldowns` DDL out of the economy cog into `CORE_SCHEMA`, and add
  both tables to `TABLE_COLUMNS`/`PRIMARY_KEYS`/`GUILD_SCOPED_TABLES` so they can
  use the generic CRUD helpers. Bump `SCHEMA_VERSION` (`:44`, currently `2`).
- `src/fyrion/cogs/economy.py` -- delete `SCHEMA_STATEMENTS` (`:86-122`) and the
  `ensure_schema`/`reset_schema_flag`/`schema_ready` machinery
  (`:607-621`); rely on the pool applying the schema at boot
  (`manager.py:287-302`). Update `cog_load` (`:1247`) accordingly.
- Migrate the legacy repositories onto the core tables (breaks the
  `guild_configs` dependency):
  - `src/fyrion/cogs/moderation.py` (`WarningsRepository` at `:46`) -> move
    warnings onto `moderation_cases` with `action='warn'`, or keep the
    `warnings` table but stop treating it as a separate concept. **Decision
    required** -- see below.
  - `src/fyrion/database/repositories/guild_config.py` -> back it with
    `guild_settings` instead of `guild_configs`, mapping `log_channel_id` ->
    `mod_log_channel_id`. Callers: `admin.py:155`, `moderation.py:47`,
    `welcome.py:78`/`:270`, `logging.py:72`/`:428`.
  - `src/fyrion/database/repositories/automod.py` -- **dead code**
    (`AutoModConfigRepository` is imported nowhere; grep confirms). Delete the
    file and the `automod_configs` table from `LEGACY_SCHEMA` (`:84-99`).
- After migration, remove the `guild_configs` dual-write from
  `manager.ensure_guild` (`:762-766`) and `delete_guild` (`:793-795`).

**Decision required from the team lead before 2b executes:** whether to
physically drop the legacy tables (`guild_configs`, `warnings`, `whitelists`,
`ticket_configs`, `member_inviters`, `invite_stats`, `automod_configs`) or keep
them for one release behind a migration. Dropping tables is destructive and
needs a migration script + operator note. Recommendation: keep `whitelists`,
`member_inviters`, `invite_stats` (no core-schema equivalent yet -- they back
invites/anti-link), delete only the truly duplicated `automod_configs` now, and
schedule `guild_configs`/`warnings`/`ticket_configs` removal after repos are
migrated.

Acceptance criteria:
- No `CREATE TABLE` statement exists outside `src/fyrion/database/schema.py`
  (`grep -rn "CREATE TABLE" src/` shows only `schema.py`).
- `AutoModConfigRepository` and `automod_configs` no longer exist; grep is empty.
- `guild_config.py` reads/writes `guild_settings`; no code path writes
  `guild_configs` (grep for `guild_configs` empty except a migration note).
- `SCHEMA_VERSION` bumped; `schema_meta`/`PRAGMA user_version` updated
  (already handled by `manager._apply_schema` `:287-302`).
- All cogs that used the legacy repos still function; full suite green, plus a
  new test that a guild's welcome/log/autorole settings survive a round trip
  through the migrated `GuildConfigRepository`.

---

## Phase 3 -- Safe custom emoji resolver with unicode fallbacks

**Goal:** one resolver used everywhere, so a custom emoji the bot cannot see
degrades to a unicode glyph instead of rendering as raw text or raising.

Files to create / modify:
- Create `src/fyrion/utils/emojis.py` with:
  - A named catalog of the unicode fallbacks currently scattered as `\U0001...`
    literals (success/error/warn/loading, ticket/lock/wave, poll digits at
    `utility.py:125-137`, etc.).
  - `resolve(bot, key, *, fallback)` -> returns a `str`/`PartialEmoji` usable in
    embeds and buttons; when `key` names a custom emoji the bot cannot access
    (`bot.get_emoji(id) is None`), returns `fallback`.
  - Move the shared parse/validate logic that is currently duplicated in
    `cogs/admin.py:96-140` (`emoji_storage_key`, `parse_emoji_input`) and
    `utils/tickets.py:110-125` into this module; have both call sites import it.
- Modify `src/fyrion/cogs/admin.py` and `src/fyrion/utils/tickets.py` to import
  from `utils/emojis.py` (remove the duplicated `MAX_UNICODE_EMOJI_LENGTH` and
  parse functions at `admin.py:71`/`:96-140` and `tickets.py:60`/`:110-125`).
- Replace inline `\U0001...` literals in cog embeds with named constants from
  the catalog where it improves clarity (optional, incremental).

Acceptance criteria:
- One module owns emoji parsing/validation/fallback; `admin.py` and
  `utils/tickets.py` contain no duplicate `parse_emoji_input`/
  `emoji_storage_key` definitions.
- Unit tests for `resolve`: (a) unicode input returns unchanged, (b) a custom
  emoji id the bot cannot see returns the fallback, (c) a visible custom emoji
  returns a usable `PartialEmoji`, (d) garbage input raises `ValueError`.
- No hardcoded `<:name:id>` literals introduced (`grep` stays empty).

---

## Phase 4 -- Consolidate the global command error tree

**Goal:** exactly one error handler, no redundant precursor.

Files to modify / delete:
- Delete `src/fyrion/errors/handler.py` (`setup_error_handlers`) -- its job is
  fully covered by the `ErrorHandler` cog (`cogs/error_handler.py`).
- `src/fyrion/bot.py` -- remove the import (`:27`) and the
  `setup_error_handlers(self)` call (`:138`). The cog installs the handler on
  load (`error_handler.py:450-451`).
- `src/fyrion/cogs/error_handler.py` -- since there is no longer a prior handler
  to preserve, either keep the save/restore (harmless, `:318-322`) or simplify
  `_previous_handler` to `None`. Keep the cog's `on_command_error` listener
  (`:425-447`); remove the now-deleted bot-level one that lived in
  `errors/handler.py:56-61`.

**Caution:** the `ErrorHandler` cog is loaded by extension discovery
(`bot.py:64-80`, `discover_extensions`). Confirm it loads before any command can
error in practice -- it installs `on_error` in `__init__`, which runs during
`_load_extensions` (`bot.py:144`), before gateway connect
(`bot.py:151`). Good.

Acceptance criteria:
- `errors/handler.py` no longer exists; `grep -rn "setup_error_handlers"` empty.
- Exactly one assignment to `bot.tree.on_error` in the codebase
  (`error_handler.py:319`).
- Existing error-handler behavior unchanged: failed commands still reply with an
  ephemeral embed carrying a reference id for unexpected errors. Add a test that
  loading the `ErrorHandler` cog sets `bot.tree.on_error` and unloading restores
  the previous value.

---

## Phase 5 -- Anti-nuke rate limiters (the real feature gap)

**Goal:** detect and stop mass-destructive actions (mass ban/kick, mass channel
or role deletion, webhook/role spam) by a single actor within a window, using
audit-log attribution, with a per-guild allow-list and configurable thresholds.

This is greenfield. Nothing today watches *volume* of moderation actions
(`logging.py` only records single events at `:332`, `:361`, `:382`).

Files to create:
- `src/fyrion/database/repositories/` -- extend the existing `security.py`
  (currently only `whitelists`, `:1-27`) or add an `antinuke` repository for
  per-guild config: enabled flag, thresholds per action, window seconds,
  punishment (strip-roles / kick / ban the offending actor), and an actor
  allow-list (trusted admins/bots). Add the backing table(s) to
  `schema.py` (`CORE_SCHEMA`) with the allow-lists updated -- coordinate with
  Phase 2 so all DDL stays in `schema.py`.
- `src/fyrion/cogs/antinuke.py` -- a cog that:
  - Listens on `on_audit_log_entry_create` (preferred: gives the actor
    directly) and falls back to `on_member_ban`/`on_member_remove`/
    `on_guild_channel_delete`/`on_guild_role_delete` correlated with a recent
    audit-log lookup for older gateway paths.
  - Keeps a per-`(guild_id, actor_id, action)` sliding window counter
    (reuse the token-bucket style already proven in
    `automod.py:155-196`; factor it into a shared `utils/ratelimit.py`).
  - On threshold breach: applies the configured punishment to the actor
    (respecting role hierarchy and the allow-list), records a
    `moderation_cases` row (`manager.create_moderation_case` `:801`), and alerts
    the mod-log channel.
  - Provides `/antinuke` config slash commands (enable, set threshold, manage
    allow-list, status).

Files to modify:
- `src/fyrion/config.py` -- add any global caps/defaults if needed
  (mirroring the `DATABASE_*` pattern at `:174-182`).
- Factor the token bucket out of `automod.py` into `src/fyrion/utils/ratelimit.py`
  and have both AutoMod and AntiNuke import it (removes future duplication).

**Security note for the Reviewer:** anti-nuke acts against server staff, so the
allow-list, hierarchy checks, and "never punish the guild owner / never punish
Fyrion itself" guards are correctness-critical. The punishment path must be
idempotent (a burst of events must not enqueue N bans of the same actor).

Acceptance criteria:
- A simulated burst of N deletions/bans by one actor within the window triggers
  exactly one punishment and one `moderation_cases` entry (unit test with a fake
  event stream and a fake guild).
- Actors on the allow-list, the guild owner, and the bot are never actioned
  (tests cover each).
- Thresholds, window, and punishment are per-guild configurable and persisted.
- Bucket state is pruned so memory does not grow unbounded (mirror
  `automod._prune_state` `:303-331`).
- All DDL for the feature lives in `schema.py`, not in the cog.

---

## Phase 6 -- Economy hardening and game coverage

**Goal:** the engine exists and is atomic; lock it down with tests and confirm
no non-atomic read-modify-write remains.

Files to review / modify:
- `src/fyrion/database/manager.py` -- `adjust_balance` (`:960-1008`),
  `transfer_balance` (`:1010-1040`): confirm the CHECK-constraint + guarded
  UPDATE cannot go negative and cannot double-apply. These look correct; add
  tests for concurrent debits against the same wallet.
- `src/fyrion/cogs/economy.py` -- audit every `credit`/`debit` path that does a
  read then a write for a race window. `take_up_to` (`:661-683`) already retries
  once; `deposit`/`withdraw` (`:685-748`) use conditional UPDATEs. The
  post-payout balance reads in `/gamble` (`:2097`) and `/slots` (`:2177`) are
  display-only after an atomic credit -- acceptable, but confirm no logic branches
  on that read.
- Blackjack: `BlackjackView` (`:1071-1214`), `settle_blackjack` (`:2326-2385`),
  `blackjack_payout` (`:507-521`). Confirm the stake is debited before the view
  is shown (it is, via `_take_stake` at `:2419`) and that a timeout settles the
  hand exactly once (`on_timeout` `:1204-1214` + `interaction_check` `:1106`).

Acceptance criteria:
- New tests: two concurrent `adjust_balance(-x)` calls where only one can
  succeed both resolve correctly (one wins, one raises `InsufficientFundsError`,
  balance never negative).
- New tests for `blackjack_payout` covering push, natural blackjack (3:2),
  player bust, dealer bust, and double-down.
- Coinflip (`/gamble`) and slots payout math tested against fixed RNG seeds.
- No economy code path reads a balance and then writes a derived value without a
  guarded conditional UPDATE (Reviewer walks each `repo.` call in `economy.py`).

---

## Phase 7 -- Cog modularity and cross-cutting cleanup

**Goal:** reduce per-cog boilerplate and tighten conventions surfaced by the
audit.

Findings:
- No shared cog base. Every cog re-implements `self.db = bot.db`, `_respond`,
  `_reject`, embed helpers (e.g. `moderation.py:67-83`, `economy.py:1294-1347`,
  `admin.py:152-155`). Cogs mix `commands.Cog` and `commands.GroupCog`
  (`moderation.py:40`, `logging.py:422`, `welcome.py:264`).
- `automod.py` defines two cogs in one file (`AutoMod` `:217`, `AutoModCommands`
  `:815`) -- fine, but document why (listener vs command surface).

Files to create / modify:
- Create `src/fyrion/cogs/_base.py` (underscore-prefixed so it is **not** loaded
  as an extension -- `discover_extensions` skips `_`-prefixed modules,
  `bot.py:78-79`) with a `FyrionCog` base providing `self.db`, a shared
  ephemeral `respond`/`reject`, and standard embed builders.
- Migrate cogs to the base incrementally; do not change command signatures or
  `custom_id`s (persistent views depend on stable ids, `bot.py:37-40`).

Acceptance criteria:
- A shared base exists and at least the moderation, economy, and admin cogs use
  it; duplicated `_respond`/`_reject` bodies are removed.
- `discover_extensions()` still returns the same command-bearing cogs (the base
  module is not loaded as an extension). Confirm with a test asserting
  `_base` is absent from the discovered list.
- No change to synced command names or persistent view `custom_id`s.
- Full suite green.

---

## Cross-phase verification

- Always `pip install -e ".[dev]"` before running tests; the `fyrion` package
  must be importable (`tests/conftest.py:18`).
- Run `python -m pytest -q` after every phase. Baseline today: **70 passed, 1
  failed**; after Phase 0 it must be **all green** and stay green thereafter.
- The pool test fixture is in-memory (WAL is skipped for `:memory:` by design,
  `manager.py:236`). Any assertion about `journal_mode == 'wal'` must use a
  temp-file database.
- Destructive schema changes (dropping legacy tables in Phase 2) require an
  explicit go-ahead from the team lead and a migration note for operators;
  do not drop tables silently.
