---
phase: 02-execution-operational-hardening
plan: 02
subsystem: database
tags: [postgres, asyncpg, migrations, execution, persistence, jsonb]

# Dependency graph
requires:
  - phase: 02-execution-operational-hardening
    provides: "Order/ExecutionIncident/ArbExecution dataclass shapes (engine.py, stable this phase)"
provides:
  - "arbiter/sql/migrations/ directory with forward-only versioned migrations"
  - "arbiter/sql/migrate.py runner (apply_pending, status, schema_migrations table)"
  - "arbiter/execution/store.py ExecutionStore: asyncpg-backed CRUD for orders/fills/incidents/arbs"
  - "execution_arbs, execution_orders, execution_fills, execution_incidents tables + partial indexes"
  - "MockPool/MockConn fixture reusable for future execution tests"
affects: [02-06-engine-integration, 02-03-recovery, 02-04-reconciliation, future-execution-work]

# Tech tracking
tech-stack:
  added: []  # asyncpg already present; no new dependencies introduced
  patterns:
    - "Forward-only SQL migrations tracked in schema_migrations table (complements existing init.sql legacy bootstrap)"
    - "ExecutionStore mirrors PositionLedger lifecycle (pool min_size=2/max_size=10/command_timeout=30)"
    - "Partial indexes on hot predicates (non-terminal orders, open incidents)"
    - "Every state transition writes (idempotent ON CONFLICT ... DO UPDATE)"

key-files:
  created:
    - "arbiter/sql/__init__.py"
    - "arbiter/sql/migrate.py"
    - "arbiter/sql/migrations/001_execution_persistence.sql"
    - "arbiter/execution/store.py"
    - "arbiter/execution/test_store.py"
  modified: []

key-decisions:
  - "Introduce arbiter/sql/migrations/ as forward-only path; leave arbiter/sql/init.sql untouched as legacy bootstrap"
  - "ExecutionStore mirrors PositionLedger pool config verbatim to keep load behaviour consistent across the two stores"
  - "arb_id is required for every upsert_order; derive from ARB-NNNNNN-... prefix when not passed explicitly"
  - "terminal_at is set only for terminal statuses (FILLED/CANCELLED/FAILED/ABORTED/SIMULATED); dynamic SQL clause is chosen from two fixed literals, not from user input"
  - "Integration tests gate on DATABASE_URL so CI without Postgres still passes with 0 skipped-as-failed"

patterns-established:
  - "Forward-only migration pattern: new DDL goes in arbiter/sql/migrations/NNN_name.sql; runner handles idempotency"
  - "Store CRUD shape: dataclass -> parameterized INSERT ... ON CONFLICT DO UPDATE; Record -> dataclass via static _row_to_order helper"
  - "MockPool/MockConn unit-test pattern: acquire() returns async-context-manager yielding MockConn that records (method, sql, args) tuples"

requirements-completed: [EXEC-02]

# Metrics
duration: 5min
completed: 2026-04-16
---

# Phase 02 Plan 02: Execution State Persistence Summary

**Postgres-backed ExecutionStore with forward-only migrations and a MockPool test suite that mirrors PositionLedger verbatim, giving Phase 2's engine integration a durable, restart-safe audit trail for orders, fills, incidents, and arbs.**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-04-16T20:37:58Z
- **Completed:** 2026-04-16T20:42:44Z
- **Tasks:** 3
- **Files created:** 5

## Accomplishments

- `execution_arbs`, `execution_orders`, `execution_fills`, `execution_incidents` tables with FK integrity (`execution_orders.arb_id -> execution_arbs`, `execution_fills.order_id -> execution_orders`) and partial indexes on hot predicates (non-terminal orders, open incidents, non-null client_order_id).
- Forward-only migration runner (`python -m arbiter.sql.migrate [--status]`) that tracks applied files in `schema_migrations` and is no-op on repeat runs.
- `ExecutionStore` class with the same lifecycle shape as `PositionLedger` (`connect/disconnect/acquire/init_schema`) plus `upsert_order`, `insert_fill`, `insert_incident`, `record_arb`, `list_non_terminal_orders`, and `get_order`. Every state transition writes; `ON CONFLICT (order_id) DO UPDATE` makes repeated calls idempotent. All SQL is parameterized (`$1, $2, ...`) — the only dynamic clause (`terminal_clause`) is selected from two fixed string literals based on `OrderStatus` enum membership.
- 8 passing unit tests and 2 DATABASE_URL-gated integration tests via a reusable `MockPool`/`MockConn` fixture — PositionLedger's 5 tests still pass (sanity).

## Task Commits

1. **Task 1: Migration + runner** — `6db9a17` (feat)
   - `arbiter/sql/__init__.py`
   - `arbiter/sql/migrate.py`
   - `arbiter/sql/migrations/001_execution_persistence.sql`
2. **Task 2: ExecutionStore class** — `c35c31d` (feat)
   - `arbiter/execution/store.py`
3. **Task 3: test_store.py** — `40f9995` (test)
   - `arbiter/execution/test_store.py`

Plan metadata commit will be authored by the orchestrator.

## Files Created/Modified

- `arbiter/sql/__init__.py` — empty, makes `arbiter.sql` an importable package
- `arbiter/sql/migrate.py` — forward-only migration runner with `apply_pending(db_url)`, `status(db_url)`, and a `python -m` CLI
- `arbiter/sql/migrations/001_execution_persistence.sql` — DDL for the four execution tables + indexes
- `arbiter/execution/store.py` — `ExecutionStore` CRUD; `_derive_arb_id` and `_opp_to_jsonb` helpers
- `arbiter/execution/test_store.py` — `MockPool`/`MockConn` fixture, 8 unit tests, 2 DATABASE_URL-gated integration tests

## Decisions Made

- **Migrations as a sibling of legacy init.sql.** The existing `arbiter/sql/init.sql` is left untouched; new schema goes into `arbiter/sql/migrations/NNN_*.sql`. Reviewers pick the forward-only pipeline from Plan 02-02 onward without disturbing the docker-compose bootstrap.
- **Pool config mirrored exactly from PositionLedger.** `min_size=2, max_size=10, command_timeout=30`. Consistent resource footprint across stores; Pitfall 8 (pool starvation) is already mitigated by never interleaving HTTP inside an `acquire()` block.
- **Single dynamic-SQL clause is enum-gated, not user-gated.** `terminal_clause` is picked from two fixed literals based on `order.status in _TERMINAL_STATUSES`. No caller data ever reaches the f-string — T-02-05 (SQL injection) is mitigated by structure, not just by grep.
- **Integration tests auto-skip when DATABASE_URL is unset.** CI without Postgres stays green; local developers opt in by sourcing `.env` before pytest.

## Deviations from Plan

None — plan executed exactly as written. Two tiny clarity tweaks, both within the plan's explicit guidance:

- In `test_store.py` the `_row_to_order_passthrough_check` symbol the plan flagged as a typo was removed (as the plan instructed: "do NOT include ... that was a typo placeholder").
- In `test_upsert_order_does_not_set_terminal_at_when_pending` the assertion was simplified to explicitly check for `terminal_at = execution_orders.terminal_at` inside the `ON CONFLICT` branch — the plan's nested `if/split/[1]/if` expression was equivalent but hard to parse. Same test intent, same test outcome.
- In the integration test `test_integration_incident_persisted` metadata assertion, added a `isinstance(md, str)` fallback so the test works regardless of whether asyncpg has a JSONB codec registered. Defensive, not a behaviour change.

## Issues Encountered

None.

## User Setup Required

None for this plan — the migration runner requires `DATABASE_URL`, which is already documented in the project's `.env.template`. To apply the migration against a running Postgres:

```bash
DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_dev python -m arbiter.sql.migrate
DATABASE_URL=... python -m arbiter.sql.migrate --status   # show applied vs pending
```

## Notes for Plan 02-06 (Engine Integration)

- `ExecutionStore` is designed for constructor-injection into `ExecutionEngine`. Engine should call `await store.upsert_order(order, arb_id=arb_id, client_order_id=client_order_id)` on every state transition (`PENDING -> SUBMITTED -> FILLED | PARTIAL | CANCELLED | FAILED | ABORTED | SIMULATED`). `terminal_at` is handled automatically.
- Engine should call `await store.record_arb(arb_execution)` once at submit time and again on each status flip — it upserts the `execution_arbs` row and delegates both legs to `upsert_order`.
- `client_order_id` only applies to Kalshi orders (idempotency key). Polymarket has no client-side id — pass `None`.
- Incidents: every `ExecutionIncident` emitted by `ExecutionEngine.subscribe_incidents()` should be mirrored to `store.insert_incident(incident)`. Resolution updates (setting `status="resolved"` + `resolved_at`) re-use the same method — `ON CONFLICT (incident_id) DO UPDATE` handles it.

## Notes for Plan 02-03 / Recovery Flow

- `await store.list_non_terminal_orders()` is the entry point for restart reconciliation — returns every order in `pending | submitted | partial` sorted by `submitted_at` so recovery can process them in submission order.
- Pair this with the `execution_arbs` FK to walk from an open arb back to its orders.

## Next Phase Readiness

- Plan 02-06 (engine integration) can import `from arbiter.execution.store import ExecutionStore` and wire it into `ExecutionEngine`. No circular imports — `store.py` imports from `engine.py`, not the reverse.
- Plan 02-03 (recovery) has `list_non_terminal_orders` as its starting point.
- CI without Postgres: `pytest arbiter/execution/test_store.py` passes 8 + skips 2.
- CI with Postgres (via docker-compose): `DATABASE_URL=... pytest arbiter/execution/test_store.py` runs the full 10 tests including round-trip persistence.

## Self-Check: PASSED

- [x] `arbiter/sql/__init__.py` exists
- [x] `arbiter/sql/migrate.py` exists; imports `apply_pending`, `status`, `_list_migration_files`
- [x] `arbiter/sql/migrations/001_execution_persistence.sql` exists; contains all 4 CREATE TABLE statements + partial indexes + FKs
- [x] `arbiter/execution/store.py` exists; `ExecutionStore` class with 10 async methods; pool config matches PositionLedger
- [x] `arbiter/execution/test_store.py` exists; 8 unit tests pass; 2 integration tests skip cleanly without DATABASE_URL
- [x] Commit `6db9a17` exists (Task 1 — migration + runner)
- [x] Commit `c35c31d` exists (Task 2 — ExecutionStore)
- [x] Commit `40f9995` exists (Task 3 — test suite)
- [x] PositionLedger test suite still passes (5 tests) — nothing in shared utils perturbed

---
*Phase: 02-execution-operational-hardening*
*Completed: 2026-04-16*
