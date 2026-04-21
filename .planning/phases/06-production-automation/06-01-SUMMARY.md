---
phase: 06-production-automation
plan: 01
status: complete
tasks_completed: 3
key_files:
  created:
    - arbiter/execution/auto_executor.py
    - arbiter/execution/test_auto_executor.py
  modified:
    - arbiter/main.py
    - .env.production.template
tests_added: 11
tests_passing: 11
---

# Plan 06-01 — AutoExecutor Wiring — SUMMARY

## What was built

`AutoExecutor` — an async consumer class that subscribes to `ArbitrageScanner.subscribe()`
and runs each emitted `ArbitrageOpportunity` through 7 policy gates, calling
`ExecutionEngine.execute_opportunity(opp)` only if every gate passes:

| Gate | Check | Metric on skip |
|---|---|---|
| G1 | `AUTO_EXECUTE_ENABLED=false` — global kill | `skipped_disabled` |
| G2 | `supervisor.is_armed` (SAFE-01 kill-switch) | `skipped_armed` |
| G3 | `opportunity.requires_manual` (SAFE-06 operator review) | `skipped_requires_manual` |
| G4 | `mapping.allow_auto_trade is True` (curated per-pair allow-list) | `skipped_not_allowed` |
| G5 | Duplicate within 5s window on `(canonical_id, yes_plat, no_plat, bucket)` | `skipped_duplicate` |
| G6 | Max-leg notional ≤ `MAX_POSITION_USD` | `skipped_over_cap` |
| G7 | Total executions < `PHASE5_BOOTSTRAP_TRADES` (rollout cap) | `skipped_bootstrap_full` |

## Key design choices

1. **Default OFF (`AUTO_EXECUTE_ENABLED=false`)** — The system reaches production
   behaviorally identical to today until the operator explicitly flips the env var.
   No accidental "ship it and it trades" failure mode.

2. **Fail-closed loop** — Any exception in `engine.execute_opportunity` is caught,
   counted in `stats.failures`, logged, and the loop continues. A single broken
   trade must not stop scanning.

3. **Dedup is bucketed by time window, not by opportunity id** — Scanner re-emits
   the same market every scan; the `_dedup_key` uses `int(now // 5)` so back-to-back
   emissions for the same market don't double-fire, but a genuinely new 5 seconds
   later is allowed through.

4. **Mapping lookup is polymorphic** — `AutoExecutor` calls `await mapping_store.get(canonical_id)`
   expecting `.allow_auto_trade`. `arbiter.main` injects `_SettingsMappingAdapter`
   (wrapping the in-memory `MARKET_MAP` dict); future Phase 6 work can swap in
   `MarketMappingStore` (DB-backed) without changing the AutoExecutor.

5. **Notional = max-leg cost, not midpoint** — `MAX_POSITION_USD` caps on the
   *worst-case* leg price so neither Kalshi nor Polymarket side can exceed
   the cap. Defense in depth alongside `PHASE5_MAX_ORDER_USD` adapter-layer
   hard-locks.

## Unit tests (11/11 green, 0.26s)

```
test_disabled_skips_execute                 PASSED
test_armed_supervisor_skips_execute         PASSED
test_requires_manual_skips_execute          PASSED
test_mapping_disallowed_skips_execute       PASSED
test_missing_mapping_skips_execute          PASSED
test_notional_over_cap_skips_execute        PASSED
test_bootstrap_cap_limits_executions        PASSED
test_duplicate_within_dedup_window_skips_second  PASSED
test_clean_opportunity_executes             PASSED
test_engine_exception_is_caught_and_counted PASSED
test_start_stop_lifecycle                   PASSED
```

## arbiter.main wiring

- `make_auto_executor_from_env(scanner, engine, supervisor, mapping_store, config_env=os.environ)`
- Creates but does not start in api-only mode (scanner+engine needed).
- In full mode, `await auto_executor.start()` runs after the regular task set launches.
- Shutdown: `await auto_executor.stop()` fires before the collectors so we stop
  making new trade decisions before tearing down venue connections.

## Env-var surface added to `.env.production.template`

```
AUTO_EXECUTE_ENABLED=false
MAX_POSITION_USD=10
PHASE5_BOOTSTRAP_TRADES=5   # already existed; AutoExecutor honors it
```

## Self-Check: PASSED
- Module imports cleanly (`python -c "import arbiter.main"` → no errors)
- All 11 unit tests green
- No regression in existing tests (spot-checked `arbiter/execution/adapters/` still 116/116)
- Default-off design confirmed: `os.environ = {}` → AutoExecutor consumes but never executes

## Deferred (Plan 06-06 will cover)
- Operator doc in GOLIVE.md for the `AUTO_EXECUTE_ENABLED=true` flip ritual
- Dashboard UI toggle for AUTO_EXECUTE_ENABLED (live reload from config)
