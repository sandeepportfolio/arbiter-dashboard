---
phase: 04-sandbox-validation
plan: 02
subsystem: infra
tags: [config, adapter, postgres, docker-compose, env-vars, polymarket, kalshi, safety]

requires:
  - phase: 03-safety-layer
    provides: SAFE-02 exposure limits + SAFE-05 graceful shutdown (hard-lock is ADDITIVE above these)
provides:
  - "env-var-sourced base URLs (KALSHI_BASE_URL, KALSHI_WS_URL, POLYMARKET_CLOB_URL)"
  - "PHASE4_MAX_ORDER_USD notional hard-lock in PolymarketAdapter.place_fok"
  - "docker-compose multi-DB init (arbiter_sandbox alongside arbiter_dev)"
  - ".env.sandbox.template operator credential template"
  - ".gitignore coverage for .env.sandbox + evidence/04/"
affects: [04-01, 04-03, 04-04, 04-05, 04-06, 04-07, 04-08, 05-live-trading]

tech-stack:
  added: []
  patterns:
    - "field(default_factory=lambda: os.getenv(...)) for env-var-sourced dataclass defaults"
    - "POSTGRES_MULTIPLE_DATABASES + init-script convention for docker-compose multi-DB"
    - "Adapter-layer belt-and-suspenders: notional = qty * price comparison BEFORE HTTP call"

key-files:
  created:
    - arbiter/execution/adapters/test_polymarket_phase4_hardlock.py
    - arbiter/sql/init-sandbox.sh
    - .env.sandbox.template
  modified:
    - arbiter/execution/adapters/polymarket.py
    - arbiter/config/settings.py
    - docker-compose.yml
    - .gitignore

key-decisions:
  - "Strict > comparison in hard-lock (notional == cap allowed) to honor operator intent"
  - "Unparseable PHASE4_MAX_ORDER_USD falls back to 0.0 cap (maximally restrictive failure mode)"
  - "Production URL defaults preserved in default_factory so unset env = unchanged behavior"
  - "Used <<EOSQL (no dash) in init-sandbox.sh to avoid tab-vs-space heredoc fragility on Windows hosts"

patterns-established:
  - "Phase 4 blast-radius hard-lock: adapter layer checks notional BEFORE any network I/O"
  - "Env-var opt-in for all Phase 4 production-code changes: no-op when env var unset"
  - "docker-compose init-script mount at /docker-entrypoint-initdb.d/ for side-channel DB setup"

requirements-completed: [TEST-01, TEST-02]

duration: 8min
completed: 2026-04-17
---

# Phase 04 Plan 02: Sandbox Enablement — Production-Code Surgical Edits Summary

**Three env-var-sourced URL defaults + adapter-layer $5 notional hard-lock + docker-compose multi-DB init + operator credential template, all backwards-compatible with unchanged production behavior when env vars unset.**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-04-17T07:20:40Z
- **Completed:** 2026-04-17T07:28:45Z
- **Tasks:** 3
- **Files modified:** 4 (1 created test file, 3 created infra files, 4 edited)

## Accomplishments

- `PolymarketAdapter.place_fok` now enforces PHASE4_MAX_ORDER_USD notional hard-lock with structured-log emission and strict `>` comparison (5/5 unit tests pass)
- `KalshiConfig.base_url`, `KalshiConfig.ws_url`, and `PolymarketConfig.clob_url` are env-var-sourced via the existing `field(default_factory=lambda: os.getenv(...))` pattern; production defaults preserved
- `docker-compose.yml` extended with `POSTGRES_MULTIPLE_DATABASES=arbiter_sandbox` + `init-sandbox.sh` mount; `docker-compose config` renders successfully
- `.env.sandbox.template` provides operator-ready copy-to-fill template with all 12 required env vars
- `.gitignore` now excludes `.env.sandbox`, `evidence/04/`, and `keys/kalshi_demo_private.pem`

## Task Commits

1. **Task 1 RED: Failing hard-lock tests** — `5ddd933` (test)
2. **Task 1 GREEN: Hard-lock implementation** — `38136d9` (feat)
3. **Task 2: Env-var-sourced URLs** — `31b0c05` (feat)
4. **Task 3: docker-compose multi-DB + template + gitignore** — `983ad16` (feat)

_Task 1 used TDD cycle: test commit then feat commit. No refactor needed (implementation minimal)._

## Files Created/Modified

- `arbiter/execution/adapters/polymarket.py` — added `import os` (line 4) and PHASE4 hard-lock block (lines 91-114) between client-is-None guard and `_place_fok_reconciling` call
- `arbiter/execution/adapters/test_polymarket_phase4_hardlock.py` — NEW; 5 unit tests (unset/over/under/boundary/unparseable)
- `arbiter/config/settings.py` — KalshiConfig.base_url (line 365), KalshiConfig.ws_url (line 371), PolymarketConfig.clob_url (line 385) converted to `field(default_factory=lambda: os.getenv(...))`
- `docker-compose.yml` — postgres service gained `POSTGRES_MULTIPLE_DATABASES: arbiter_sandbox` env var (line 19) and `./arbiter/sql/init-sandbox.sh:/docker-entrypoint-initdb.d/init-sandbox.sh:ro` volume mount (line 25)
- `arbiter/sql/init-sandbox.sh` — NEW; bash initdb script with `set -e`, `POSTGRES_MULTIPLE_DATABASES` env parse, `CREATE DATABASE`/`GRANT ALL PRIVILEGES` per DB; mode 100755 set via `git update-index --chmod=+x`
- `.env.sandbox.template` — NEW; operator credential template with DRY_RUN=false, Kalshi demo URLs, Polymarket prod URL+throwaway wallet, PHASE4_MAX_ORDER_USD=5, optional Telegram vars
- `.gitignore` — appended 3 lines under "Phase 4 sandbox credentials + evidence" section

## Interface Contracts Published (for downstream Phase 4 plans)

- **Env var names (canonical):** `KALSHI_BASE_URL`, `KALSHI_WS_URL`, `POLYMARKET_CLOB_URL`, `PHASE4_MAX_ORDER_USD`
- **Hard-lock semantics:**
  - Unset `PHASE4_MAX_ORDER_USD` → no-op (production behavior unchanged)
  - Set + `notional_usd = float(qty) * float(price) > float(PHASE4_MAX_ORDER_USD)` → returns `_failed_order` with status `OrderStatus.FAILED`, error string starts with `PHASE4_MAX_ORDER_USD hard-lock:`, structlog event `polymarket.phase4_hardlock.rejected` emitted
  - Strict `>` comparison — notional == cap is allowed
  - Unparseable value → 0.0 cap → any positive notional rejected (safe failure mode)
- **URL defaults preserved:** Unset `KALSHI_BASE_URL` → `https://api.elections.kalshi.com/trade-api/v2`; unset `POLYMARKET_CLOB_URL` → `https://clob.polymarket.com`; etc.
- **Multi-DB convention:** `POSTGRES_MULTIPLE_DATABASES=<csv>` env var + `/docker-entrypoint-initdb.d/init-sandbox.sh` mount creates listed DBs on first volume init
- **Fixture guard-rail target:** Plan 04-01 sandbox fixtures SHOULD assert `PHASE4_MAX_ORDER_USD` env var is set before constructing `PolymarketAdapter` (safety rail per PATTERNS.md)

## Decisions Made

- **Strict `>` comparison (not `>=`):** Notional exactly equal to cap is allowed, matching operator intent. A $5.00 notional with $5 cap is NOT rejected.
- **0.0 fallback for unparseable env:** Any malformed env string causes maximum restriction (any positive notional rejected), which is the safe failure mode per threat T-04-02-08.
- **No-dash heredoc (`<<EOSQL`) in init-sandbox.sh:** The `<<-EOSQL` form strips leading tabs but NOT spaces, which made it fragile when files are authored on Windows hosts (tabs may not be preserved). Using `<<EOSQL` with SQL body at column 0 sidesteps the issue entirely. Shell syntax verified with `bash -n`.
- **Preserved production defaults verbatim:** All three URL conversions keep the exact prior string as the `os.getenv` default, so any test or runtime path that does NOT set the env var continues to produce production URLs. Verified via two `python -c` assertions.

## Deviations from Plan

None — plan executed exactly as written except for one micro-adjustment:

- **Heredoc form** in `arbiter/sql/init-sandbox.sh` uses `<<EOSQL` (no dash) instead of the plan's suggested `<<-EOSQL`. This is NOT a deviation from intent (still produces the same SQL); it is a robustness choice to avoid tab-vs-space fragility when authored on Windows. `bash -n` confirms valid syntax, and the SQL body at column 0 is correct for non-indented heredoc. Documented in Decisions Made above.

## Issues Encountered

- **Pre-existing test_api_integration.py failure:** `test_api_and_dashboard_contracts` asserts `"ARBITER LIVE"` heading string that was renamed in Phase 03-07. This failure pre-dates Plan 04-02 and is documented in PROJECT.md "Known cross-phase drift". NOT caused by my changes (confirmed by running `pytest arbiter/ --ignore=arbiter/test_api_integration.py --ignore=arbiter/sandbox -x -q` → 225 passed, 3 skipped). Out of scope for Plan 04-02 per executor scope boundary rule.

## User Setup Required

None from this plan's code changes directly. BUT the `.env.sandbox.template` it created is the operator starting point for Plan 04-04 (Kalshi demo account setup) and Plan 04-05 (Polymarket test-wallet funding). Plan 04-08 will author the full `arbiter/sandbox/README.md` operator bootstrap.

## Next Phase Readiness

- **Plan 04-01 (sandbox conftest fixtures):** Can now assert `PHASE4_MAX_ORDER_USD` env var presence before building `PolymarketAdapter` — the interface contract is in place.
- **Plan 04-03 onward:** Can copy `.env.sandbox.template` → `.env.sandbox` and `source` it; Kalshi demo URL override is plumbed end-to-end (settings.py → adapter).
- **docker-compose:** Next `docker compose up -d postgres` (on a fresh volume) will create `arbiter_sandbox` alongside `arbiter_dev` via init-sandbox.sh.
- **No blockers** for downstream Phase 4 plans from this plan's surface.

## Self-Check: PASSED

**Files created (verified on disk):**
- FOUND: arbiter/execution/adapters/test_polymarket_phase4_hardlock.py
- FOUND: arbiter/sql/init-sandbox.sh (mode 100755)
- FOUND: .env.sandbox.template

**Commits (verified via git log):**
- FOUND: 5ddd933 (test RED)
- FOUND: 38136d9 (feat GREEN)
- FOUND: 31b0c05 (feat Task 2)
- FOUND: 983ad16 (feat Task 3)

**Unit tests:** 5/5 hard-lock tests pass; 74/74 total adapter tests pass (no regression in pre-existing adapter tests).

**Success criteria:**
- `pytest arbiter/execution/adapters/test_polymarket_phase4_hardlock.py -v` → 5 passed
- `KalshiConfig().base_url` unset-env → production URL (preserved)
- `KALSHI_BASE_URL=...demo...` → override URL (works)
- `docker-compose config` → YAML valid
- `bash -n arbiter/sql/init-sandbox.sh` → syntax OK
- `.env.sandbox.template` contains all 12 required entries
- `.gitignore` contains `.env.sandbox` and `evidence/04/`

## TDD Gate Compliance

- **RED gate:** `5ddd933` (test) committed before any production code
- **GREEN gate:** `38136d9` (feat) committed after test commit
- **REFACTOR gate:** None needed; implementation was minimal and clear

All three task scopes satisfy the plan's threat model mitigations (T-04-02-01 through T-04-02-08).

---
*Phase: 04-sandbox-validation*
*Completed: 2026-04-17*
