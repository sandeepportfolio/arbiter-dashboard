---
phase: 04-sandbox-validation
reviewed: 2026-04-17T00:00:00Z
depth: standard
files_reviewed: 29
files_reviewed_list:
  - .env.sandbox.template
  - .gitignore
  - arbiter/config/settings.py
  - arbiter/execution/adapters/kalshi.py
  - arbiter/execution/adapters/polymarket.py
  - arbiter/execution/adapters/test_kalshi_place_resting_limit.py
  - arbiter/execution/adapters/test_polymarket_phase4_hardlock.py
  - arbiter/sandbox/README.md
  - arbiter/sandbox/__init__.py
  - arbiter/sandbox/aggregator.py
  - arbiter/sandbox/conftest.py
  - arbiter/sandbox/evidence.py
  - arbiter/sandbox/fixtures/__init__.py
  - arbiter/sandbox/fixtures/kalshi_demo.py
  - arbiter/sandbox/fixtures/polymarket_test.py
  - arbiter/sandbox/fixtures/sandbox_db.py
  - arbiter/sandbox/reconcile.py
  - arbiter/sandbox/test_aggregator.py
  - arbiter/sandbox/test_graceful_shutdown.py
  - arbiter/sandbox/test_kalshi_fok_rejection.py
  - arbiter/sandbox/test_kalshi_happy_path.py
  - arbiter/sandbox/test_kalshi_timeout_cancel.py
  - arbiter/sandbox/test_one_leg_exposure.py
  - arbiter/sandbox/test_phase_reconciliation.py
  - arbiter/sandbox/test_polymarket_fok_rejection.py
  - arbiter/sandbox/test_polymarket_happy_path.py
  - arbiter/sandbox/test_rate_limit_burst.py
  - arbiter/sandbox/test_safety_killswitch.py
  - arbiter/sandbox/test_smoke.py
  - arbiter/sql/init-sandbox.sh
  - docker-compose.yml
findings:
  critical: 0
  warning: 5
  info: 6
  total: 11
status: issues_found
---

# Phase 4: Code Review Report

**Reviewed:** 2026-04-17
**Depth:** standard
**Files Reviewed:** 29 (production adapters + sandbox harness + docker/SQL wiring)
**Status:** issues_found

## Summary

Phase 4 delivers a well-structured sandbox validation harness. The production
adapter changes (`PHASE4_MAX_ORDER_USD` hard-lock in `polymarket.py` and
`kalshi.py::place_resting_limit`) are implemented consistently with matching
safe-default failure modes, rate-limiter/circuit-breaker wiring, and explicit
unit coverage. Safety guards are layered correctly: fixture-level assertions
refuse to construct adapters without the $5 cap, notional-check assertions in
test bodies fail fast before touching adapters, and the `@pytest.mark.live`
collection-time gate prevents accidental live-fire.

The issues found are primarily about **correctness of the test scaffolding**
rather than the production code. The most important items:

1. A boundary-case unit test (`test_resting_limit_hardlock_boundary_exact_equal_allowed`)
   mislabels what it actually tests — its docstring promises an exact-equal
   boundary assertion but the code tests a clear under-cap case. This leaves
   the `notional == cap` path for `place_resting_limit` uncovered.
2. `_load_project_dotenv` auto-loads `.env` with `override=True` at module
   import time, which can silently clobber shell-exported sandbox vars. The
   fixture safety guards fail closed on this, so it does not risk data loss,
   but it will surprise operators.
3. The recorded-PnL fallback in the aggregator mixes two accounting conventions
   (explicit `realized_pnl` vs. signed cash-flow) and can return a partial sum
   when only some rows carry `realized_pnl`.
4. `init-sandbox.sh` uses unquoted `$db` and `$POSTGRES_USER` interpolation in
   a SQL heredoc — SQL injection is theoretically possible if those env vars
   ever become operator-controlled. In the current docker-compose pipeline the
   values are fixed, so the risk is bounded.
5. Two live-fire tests retain TEST-ONLY fallback paths that would post
   malformed bodies to Kalshi (`time_in_force: "resting"` / `"GTC"` — Kalshi
   uses *absence* of the field). These paths are now dead (Plan 04-02.1 made
   `place_resting_limit` a public method), but the dead fallbacks will produce
   confusing errors if ever triggered by a regression.

No critical security issues. The `PHASE4_MAX_ORDER_USD` adapter-layer hard-lock
is the primary defense and is implemented with belt-and-suspenders semantics
(strict `>` comparison, unparseable → 0.0 cap, bypassed before any HTTP call).

## Warnings

### WR-01: `place_resting_limit` boundary test does not actually exercise the documented boundary

**File:** `arbiter/execution/adapters/test_kalshi_place_resting_limit.py:253-277`
**Issue:** The test is named
`test_resting_limit_hardlock_boundary_exact_equal_allowed` and its docstring
says:

> qty=5 * price=1.00 == 5.00 == cap → allowed

But the actual call on line 271-273 uses `price=0.99, qty=5`, giving a notional
of $4.95 (strictly *under* cap). The inline comment on line 274 even
acknowledges this: `0.99 * 5 = 4.95 — definitively under cap; use it to keep
price valid (<1)` — the test author noticed that `price=1.0` hits the
`0 < price < 1` exclusive validation and silently switched to a
strictly-under-cap case.

Net effect: the `notional == cap` path for `place_resting_limit` is NOT
covered. The sister test
`test_polymarket_phase4_hardlock.py::test_hardlock_boundary_exact_equal_allowed`
is able to test `price=1.0, qty=5` because `polymarket.py::place_fok` does
not validate price until after the hard-lock check, so the two adapters have
divergent coverage.

**Fix:** Pick a boundary case that satisfies `0 < price < 1`:

```python
# notional = qty * price = cap exactly. 10 * 0.50 = 5.00 == PHASE4_MAX_ORDER_USD.
monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
result = await adapter.place_resting_limit(
    "ARB-HL-3", "T", "C", "yes", 0.50, 10,
)
assert session.post.called, "notional == cap must be ALLOWED (strict > comparison)"
assert result.status == OrderStatus.SUBMITTED
```

This also aligns the test name, docstring, and assertion.

---

### WR-02: `_load_project_dotenv` uses `override=True`, silently clobbering shell-exported sandbox vars

**File:** `arbiter/config/settings.py:37-40`
**Issue:** `_load_project_dotenv` iterates candidate `.env` files and calls
`load_dotenv(candidate, override=True)`. This means values loaded from a stale
`.env` at the repo root will override values the operator already exported via
`set -a; source .env.sandbox; set +a`.

Scenario that bites:

1. Operator has `.env` at repo root (production config) with
   `DATABASE_URL=postgresql://.../arbiter_dev`.
2. Operator sources `.env.sandbox` → shell env has
   `DATABASE_URL=postgresql://.../arbiter_sandbox`.
3. `pytest` imports `arbiter.config.settings` → `_load_project_dotenv` runs
   at module top-level and overrides `DATABASE_URL` back to `arbiter_dev`.
4. `sandbox_db_pool` fixture fails closed on the safety assertion, which is
   good, but the failure message ("got 'postgresql://.../arbiter_dev'") is
   confusing because the operator did source `.env.sandbox`.

The fixture guards (`sandbox_db_pool`, `demo_kalshi_adapter`) mean this cannot
actually connect to the wrong DB — the failure mode is "confusing assertion
message" rather than "pointed at prod." Still worth fixing because the
sandbox harness is the guardrail operators rely on.

**Fix:** Use `override=False` so shell-exported values win (the canonical
python-dotenv pattern for test environments), or explicitly pick the right
`.env` for the context:

```python
# Shell-exported vars should win over file-loaded defaults.
load_dotenv(candidate, override=False)
```

Alternative: document in `arbiter/sandbox/README.md` that operators should
temporarily move `.env` out of the way before running Phase 4 live-fire, or
export `DOTENV_PATH=/dev/null` (requires a small refactor of `_load_project_dotenv`
to respect such a signal).

---

### WR-03: Aggregator `_compute_recorded_pnl` returns partial sum when some rows carry `realized_pnl` and others do not

**File:** `arbiter/sandbox/aggregator.py:114-168`
**Issue:** The function has two accounting paths:

1. Preferred: sum `realized_pnl` across all rows that carry the field.
2. Fallback: for every row, derive a signed cash-flow from
   `fill_price * fill_qty * side_sign - fee`.

The preferred path triggers if *any* row has `realized_pnl` set
(`any_realized = True`). But rows without the field are silently skipped
(`continue` at line 137). For a scenario whose `execution_orders.json` has
e.g. 3 rows — buy leg with `realized_pnl: null`, sell leg with
`realized_pnl: 2.50`, and a fee row without the field — the function returns
`2.50` and ignores the other two rows entirely.

The fallback path (which would correctly sum cash-flows for all rows) only
runs when *zero* rows have `realized_pnl`. This creates an either-all-or-none
contract that the schema/scenario harness does not enforce.

There is also a semantic mismatch: the preferred path reports true realized
P&L; the fallback reports signed cash-flow minus fees. These are not the same
quantity (realized P&L subtracts cost basis). For a simple within-scenario
"balance_delta vs. recorded cash movement" reconciliation they happen to
agree, but the `ScenarioReconcileResult.pnl_recorded` field is labelled as
P&L in downstream Markdown rendering.

**Fix:** Pick one convention and enforce it. Either:

```python
# Option A: require all rows to carry realized_pnl, fail loudly otherwise.
if any("realized_pnl" in row and row["realized_pnl"] is not None for row in execution_orders):
    missing = [i for i, row in enumerate(execution_orders)
               if row.get("realized_pnl") is None]
    if missing:
        raise ValueError(
            f"mixed realized_pnl schema: rows {missing} lack the field; "
            f"scenario harness must set it on every row or none"
        )
    return sum(float(row["realized_pnl"]) for row in execution_orders)

# Option B: always use cash-flow fallback; never trust realized_pnl column.
# (simpler; matches downstream "balance_delta == recorded_pnl" expectation)
```

Also rename the downstream `pnl_recorded` field to `cash_flow_recorded`
(or similar) when using Option B so the VALIDATION.md is accurate.

---

### WR-04: `init-sandbox.sh` has unquoted SQL identifier interpolation

**File:** `arbiter/sql/init-sandbox.sh:12-13`
**Issue:** The heredoc passed to `psql` interpolates `$db` and
`$POSTGRES_USER` without quoting:

```sh
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<EOSQL
CREATE DATABASE $db;
GRANT ALL PRIVILEGES ON DATABASE $db TO $POSTGRES_USER;
EOSQL
```

If `POSTGRES_MULTIPLE_DATABASES` ever becomes operator-controlled (directly or
via a misconfigured CI), a value like `arbiter_sandbox;DROP DATABASE
arbiter_dev;--` would execute as two statements. Today the value is hardcoded
in `docker-compose.yml` to `arbiter_sandbox`, so the exposure is bounded — but
treating init scripts as "safe because the env is controlled" is how SQL
injection surprises happen later.

There is also a word-splitting risk: `for db in $(echo ... | tr ',' ' ')`
splits on whitespace in addition to commas, so a value like
`arbiter_sandbox arbiter_audit` would create two DBs without going through the
comma-separated contract.

**Fix:** Quote identifiers via `psql` variable substitution (which does proper
SQL identifier escaping), and use `IFS=,` to split on commas only:

```sh
#!/bin/bash
set -eu

if [ -n "${POSTGRES_MULTIPLE_DATABASES:-}" ]; then
    echo "Creating additional databases: $POSTGRES_MULTIPLE_DATABASES"
    IFS=','
    for db in $POSTGRES_MULTIPLE_DATABASES; do
        db_trimmed="$(echo "$db" | xargs)"  # strip whitespace
        echo "  -> CREATE DATABASE $db_trimmed"
        psql -v ON_ERROR_STOP=1 \
             --username "$POSTGRES_USER" \
             -v db="$db_trimmed" \
             -v owner="$POSTGRES_USER" <<'EOSQL'
CREATE DATABASE :"db";
GRANT ALL PRIVILEGES ON DATABASE :"db" TO :"owner";
EOSQL
    done
fi
```

The `:"var"` syntax tells psql to quote the value as a SQL identifier.

---

### WR-05: `polymarket.py::_is_rate_limit_error` matches "429" anywhere in message

**File:** `arbiter/execution/adapters/polymarket.py:231-245`
**Issue:** The matcher uses `"429" in msg`. Any exception whose string
representation contains the substring "429" will be classified as
rate-limited — including, e.g., a market token id ending in `...429...`
surfaced in an error message, or a timestamp/order-count that happens to
contain those digits.

A false positive here is moderately costly: the adapter will record a circuit
failure, apply a retry-after penalty to the rate limiter, and return FAILED
instead of letting the real error surface. On a live-fire the operator sees
`"rate_limited (2.0s)"` and starts debugging the wrong problem.

**Fix:** Anchor the match to error-message patterns that actually come from
py-clob-client, not raw digit substrings:

```python
@staticmethod
def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    # py-clob-client surfaces rate-limiting as messages containing either
    # an HTTP status token ("http 429", "status: 429") or one of the
    # canonical phrasings. Avoid bare-digit match to prevent false positives
    # on token ids or timestamps that include "429" incidentally.
    return (
        "http 429" in msg
        or "status: 429" in msg
        or "status code 429" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "too many requests" in msg
    )
```

## Info

### IN-01: `test_graceful_shutdown.py` fallback path posts invalid Kalshi order body (`time_in_force="resting"`)

**File:** `arbiter/sandbox/test_graceful_shutdown.py:204-212`
**Issue:** The TEST-ONLY Step 3 raw-HTTP fallback builds an order body with
`"time_in_force": "resting"`. Kalshi does not accept the literal `"resting"`
as a TIF value — the real adapter (`kalshi.py::place_resting_limit`) OMITS
the field entirely (see comment at adapters/kalshi.py:327: `# NB: NO
time_in_force — absence = GTC/resting at Kalshi.`).

After Plan 04-02.1 this fallback is dead code: Step 1
(`adapter.place_resting_limit`) always resolves. If it ever regresses and
Step 3 fires, the test will report a confusing `HTTP 400: invalid
time_in_force` and operators will not easily find that the fallback itself
is wrong.

**Fix:** Either remove the Step 3 fallback entirely now that
`place_resting_limit` is production, or match the production body shape by
omitting `time_in_force`:

```python
body = {
    "ticker": market_id,
    "client_order_id": client_order_id,
    "action": "buy",
    "side": side,
    "type": "limit",
    "count_fp": f"{float(qty):.2f}",
    # no time_in_force → resting/GTC at Kalshi
}
```

Same issue applies to `test_kalshi_timeout_cancel.py:209-219` which uses
`"time_in_force": "GTC"` — Kalshi does not document "GTC" as an accepted TIF
either. Both fallbacks look like they were never exercised against a real
Kalshi server.

---

### IN-02: `test_graceful_shutdown.py` manifest write is not written on assertion failure

**File:** `arbiter/sandbox/test_graceful_shutdown.py:552-582`
**Issue:** The manifest write lives after the `try/finally` block. If any
assertion inside the try block fails, the manifest is not written — so the
aggregator misses this scenario entirely and the per-task map shows
"pending live-fire" instead of surfacing the failure.

The `"phases_seen": sorted(phases_seen) if "phases_seen" in dir() else []`
pattern is also fragile: `dir()` inside a function returns module names plus
locals; the check works by accident and is easy to misread during review.

**Fix:** Wrap assertion failures so they do not prevent manifest capture
(and always record the observed-or-empty state), or keep the current behavior
but add an explicit manifest stub in the `finally` block:

```python
finally:
    ...existing cleanup...
    if "phases_seen" not in locals():
        phases_seen = set()
```

And move the manifest write into the `finally` block so failed scenarios are
still captured in evidence/04/ for the aggregator to report.

---

### IN-03: `.env.sandbox.template` ships with the same default password baked into docker-compose

**File:** `.env.sandbox.template:18` and `docker-compose.yml:18`
**Issue:** The template hard-codes
`DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_sandbox`
and `docker-compose.yml` defaults `POSTGRES_PASSWORD` to the same literal.
This is acceptable for a local dev/test DB but the password leaks through
both files — rotating it requires two changes. On an operator workstation,
anyone reading either file can connect to the local postgres.

Low-risk because the DB is bound to localhost and contains only test data,
but worth tracking.

**Fix:** Either accept this as documented local-only convention (add a
comment in both files stating "local dev only, not a real secret"), or
parameterize the sandbox DATABASE_URL in the template to pull from
`$PG_USER`/`$PG_PASSWORD`:

```bash
# The template expects PG_USER and PG_PASSWORD to be set via docker-compose .env.
DATABASE_URL=postgresql://${PG_USER:-arbiter}:${PG_PASSWORD:-arbiter_secret}@localhost:5432/arbiter_sandbox
```

---

### IN-04: `test_polymarket_fok_rejection.py` tautological assertion

**File:** `arbiter/sandbox/test_polymarket_fok_rejection.py:151-152`
**Issue:**

```python
_fok_pitfall_literal = "FOK_ORDER_NOT_FILLED_ERROR"
assert _fok_pitfall_literal in "FOK_ORDER_NOT_FILLED_ERROR"  # tautology: keeps literal in source
```

The comment acknowledges the assertion is a tautology whose only purpose is
to keep the literal string in the file for grep-ability. This works but is
confusing to readers. A module-level constant with `# noqa: F841` or a
`__all__`-equivalent would express the intent better.

**Fix:**

```python
# Exported for aggregator grep — "FOK_ORDER_NOT_FILLED_ERROR" is the Pitfall
# 4 sentinel from py-clob-client.
FOK_NOT_FILLED_SENTINEL = "FOK_ORDER_NOT_FILLED_ERROR"
```

No `assert` needed; the constant survives module-level references.

---

### IN-05: Docker Compose `version` key is obsolete

**File:** `docker-compose.yml:1`
**Issue:** `version: "3.9"` is ignored by modern Docker Compose (v2+) and
generates a deprecation warning on every invocation. It does not affect
behavior but clutters log output during Phase 4 operator workflows
(`docker compose up -d` is in the README runbook).

**Fix:** Delete the line.

---

### IN-06: `_DOTENV_PATH` module-level side effect at import time

**File:** `arbiter/config/settings.py:44`
**Issue:** `_DOTENV_PATH = _load_project_dotenv()` runs at module import and
mutates `os.environ` as a side effect. Combined with WR-02's `override=True`,
any test that imports `arbiter.config.settings` — including the Phase 4
smoke test indirectly — triggers dotenv loading before fixtures run. This
makes import ordering observable and couples test behavior to whether the
repo root has a stale `.env`.

**Fix:** Defer dotenv loading to `load_config()` and make the path
overridable:

```python
def load_config(dotenv_path: Optional[Path] = None) -> ArbiterConfig:
    if dotenv_path is not None:
        load_dotenv(dotenv_path, override=False)
    elif not os.environ.get("ARBITER_NO_DOTENV"):
        _load_project_dotenv()
    cfg = ArbiterConfig()
    ...
```

This also addresses WR-02 by making the load explicit and testable.

---

_Reviewed: 2026-04-17_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
