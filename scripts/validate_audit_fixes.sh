#!/usr/bin/env bash
#
# Reproducible validation for the Fyrion audit-and-repair work.
#
# The audit session that produced this script ran with its shell blocked by the
# tooling classifier, so nothing in it could be executed there. This script
# exists so every check the report could not run is runnable, in the project's
# own configuration, by anyone with a shell. It performs no writes to the
# repository and touches no network service: it compiles, lints, type-checks and
# tests the working tree, and it inspects the fixes textually.
#
# Usage:
#     bash scripts/validate_audit_fixes.sh            # full run
#     bash scripts/validate_audit_fixes.sh --quick    # compile + targeted greps only
#
# Exit status: 0 when every *required* check passed, 1 otherwise. Checks that
# cannot run (missing tool, missing dependency) are reported as BLOCKED and count
# as failures, matching the report's PASS/FAIL/BLOCKED/NOT CONFIGURED scheme.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

PYTHON="${PYTHON:-python3}"
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

FAILURES=0
BLOCKED=0

pass()    { printf 'PASS     %s\n' "$1"; }
fail()    { printf 'FAIL     %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
blocked() { printf 'BLOCKED  %s\n' "$1"; BLOCKED=$((BLOCKED + 1)); }
info()    { printf 'INFO     %s\n' "$1"; }
section() { printf '\n=== %s ===\n' "$1"; }

# Runs a command, echoing it first, and records pass/fail by exit status.
run() {
    local label="$1"; shift
    printf '$ %s\n' "$*"
    if "$@"; then
        pass "$label"
    else
        fail "$label (exit $?)"
    fi
}

# ---- 1. Workspace and git baseline -----------------------------------------
section "Workspace and git baseline"
printf '$ pwd\n'; pwd
printf '$ git rev-parse --show-toplevel\n'
if git rev-parse --show-toplevel 2>/dev/null; then
    pass "inside a git work tree"
    printf '$ git status --short --branch\n'
    git status --short --branch
    printf '$ git log -5 --oneline\n'
    git log -5 --oneline
    printf '$ git rev-parse HEAD\n'
    git rev-parse HEAD
else
    fail "not a git work tree (expected the checkout of adityatheog/fyrion)"
fi

# ---- 2. Syntax: the two critical repairs -----------------------------------
section "Syntax (the two critical repairs)"
run "py_compile dashboard/app.py"        "$PYTHON" -m py_compile dashboard/app.py
run "py_compile tests/test_dashboard_api.py" "$PYTHON" -m py_compile tests/test_dashboard_api.py
run "compileall src dashboard tests"      "$PYTHON" -m compileall -q src dashboard tests

# ---- 3. Targeted textual checks for the fixes ------------------------------
section "Fix-presence checks (textual)"
grep -q '_SNOWFLAKE_COLUMNS_WITHOUT_ID_SUFFIX' src/fyrion/web/app.py \
    && pass "F-04 snowflake predicate present" \
    || fail "F-04 snowflake predicate missing"
for col in resolved_by claimed_by closed_by created_by added_by; do
    grep -q "\"$col\"" src/fyrion/web/app.py \
        && pass "F-04 covers $col" || fail "F-04 misses $col"
done
grep -q 'add_api_route(' src/fyrion/web/app.py \
    && grep -q 'Config.DASHBOARD_OAUTH_CALLBACK_PATH' src/fyrion/web/app.py \
    && pass "F-03 callback mounted at the configured path" \
    || fail "F-03 callback route not found"
grep -q 'DASHBOARD_OAUTH_CALLBACK_PATH=/auth/callback' .env.example \
    && pass "F-03 .env.example documents /auth/callback" \
    || fail "F-03 .env.example callback path wrong"
if grep -q 'exception_handler(Exception)' src/fyrion/web/app.py; then
    fail "F-05 an Exception handler is still registered (would bypass hardening)"
else
    pass "F-05 no Exception handler (500 is caught inside _harden)"
fi

if [ "$QUICK" = "1" ]; then
    section "Summary (--quick)"
    printf 'failures=%d blocked=%d\n' "$FAILURES" "$BLOCKED"
    [ "$FAILURES" -eq 0 ] && [ "$BLOCKED" -eq 0 ]
    exit $?
fi

# ---- 4. Test suite ---------------------------------------------------------
section "Tests"
printf '$ %s -m pytest tests/ -q\n' "$PYTHON"
if "$PYTHON" -m pytest tests/ -q; then
    pass "pytest tests/"
    # Confirm the new regression tests are actually collected, not skipped.
    if "$PYTHON" -m pytest tests/test_audit_regressions.py --collect-only -q \
        2>/dev/null | grep -q 'test_'; then
        pass "regression tests are collected"
    else
        fail "regression tests were not collected"
    fi
else
    fail "pytest tests/"
fi

# ---- 5. Formatting, lint, types (mirrors .github/workflows/ci.yml) ---------
section "Quality pipeline (as configured in CI)"
if command -v black >/dev/null 2>&1; then
    run "black --check src tests (CI scope)" black --check src tests
else
    blocked "black not installed (pip install -e '.[dev]')"
fi

if command -v flake8 >/dev/null 2>&1; then
    run "flake8 blocking pass (E9,F63,F7,F82)" \
        flake8 src tests --count --select=E9,F63,F7,F82 --show-source --statistics
    # Advisory pass: never fails the build, run for the record.
    printf '$ flake8 advisory pass (exit-zero)\n'
    flake8 src tests --count --exit-zero --max-complexity=15 --max-line-length=120 \
        --statistics || true
    pass "flake8 advisory pass (informational)"
else
    blocked "flake8 not installed"
fi

if command -v mypy >/dev/null 2>&1; then
    run "mypy src dashboard" mypy src dashboard
else
    blocked "mypy not installed"
fi

# ---- 6. Summary ------------------------------------------------------------
section "Summary"
printf 'failures=%d blocked=%d\n' "$FAILURES" "$BLOCKED"
if [ "$FAILURES" -eq 0 ] && [ "$BLOCKED" -eq 0 ]; then
    printf 'ALL CHECKS PASSED\n'
    exit 0
fi
printf 'NOT CLEAN\n'
exit 1
