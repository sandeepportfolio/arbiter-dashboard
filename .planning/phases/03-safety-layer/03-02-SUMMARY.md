---
phase: 03-safety-layer
plan: 02
subsystem: safety
tags: [risk-manager, per-platform-exposure, order-rejected-incident, safety-layer]
requires:
  - arbiter.config.settings.SafetyConfig
  - arbiter.config.settings.ArbiterConfig.safety
  - arbiter.execution.engine.RiskManager
  - arbiter.execution.engine.ExecutionEngine
  - arbiter.execution.engine._record_incident
  - arbiter.scanner.arbitrage.ArbitrageOpportunity
provides:
  - "RiskManager._platform_exposures (per-venue aggregate, keyed by platform name)"
  - "RiskManager.check_trade per-platform pre-trade gate"
  - "RiskManager.record_trade / release_trade per-platform exposure accounting"
  - "ExecutionEngine._emit_rejection_incident (structured order_rejected incident)"
  - "rejection_type taxonomy: per_market | per_platform | total_exposure | daily_loss | daily_trades | stale_quote | low_confidence | thin_edge | not_ready | unknown"
affects:
  - arbiter.execution.engine.RiskManager.__init__ (new safety_config kwarg)
  - arbiter.execution.engine.RiskManager.check_trade (per-platform block added)
  - arbiter.execution.engine.RiskManager.record_trade (new yes/no platform + exposure kwargs)
  - arbiter.execution.engine.RiskManager.release_trade (new yes/no platform + exposure kwargs)
  - arbiter.execution.engine.ExecutionEngine.__init__ (RiskManager wired with config.safety)
  - arbiter.execution.engine.ExecutionEngine.execute_opportunity (rejection → incident path)
  - arbiter.execution.engine.ExecutionEngine.update_manual_position (record/release include per-platform splits)
tech-stack:
  added: []
  patterns:
    - "Optional SafetyConfig injection with float('inf') fallback for backward-compat"
    - "Structured rejection_type derived from reason-string prefix matching"
    - "order_rejected incident flows through the existing incident WebSocket event (no new event type)"
key-files:
  created:
    - .planning/phases/03-safety-layer/deferred-items.md
  modified:
    - arbiter/execution/engine.py
    - arbiter/execution/test_engine.py
decisions:
  - "RiskManager accepts optional safety_config; absent → float('inf') per-platform ceiling preserves legacy behaviour"
  - "Per-platform check fires AFTER per-market to keep the most-specific limit rule-first; per-market messages stay verbatim for regression callers"
  - "order_rejected incidents are severity='info' (informational, not warning) — they are expected safety decisions, not errors; one-leg/critical is reserved for actual risk"
  - "Plan 03-07 will add a filtered 'Rejected orders' sub-view inside the incident panel — no new WebSocket event type needed now"
metrics:
  duration: "~6min"
  completed: 2026-04-17
---

# Phase 03 Plan 02: RiskManager Per-Platform Exposure (SAFE-02) Summary

## One-liner

RiskManager now tracks per-platform aggregate exposure against `SafetyConfig.max_platform_exposure_usd` and emits a structured `order_rejected` ExecutionIncident on every rejection, surfaced through the existing incident WebSocket event.

## What Shipped

- **`RiskManager` (arbiter/execution/engine.py)**
  - New optional `safety_config: Optional[SafetyConfig] = None` kwarg. When omitted (legacy callers / bare tests), `_max_platform_exposure` defaults to `float('inf')` — backward-compat preserved.
  - New `_platform_exposures: Dict[str, float]` dict — keyed by platform name (e.g. `"kalshi"`, `"polymarket"`).
  - `check_trade` adds a per-platform aggregate block: for each of `opp.yes_platform` and `opp.no_platform`, computes the leg exposure (`suggested_qty * price`), and if `_platform_exposures[platform] + leg > _max_platform_exposure`, returns `(False, f"Per-platform exposure limit exceeded on {platform}")`. Same-platform legs (defensive — cross-platform arbs shouldn't land on one venue) are aggregated before the compare. Order of checks: status → confidence → edge → quote age → daily trades → daily loss → **per-market → per-platform (NEW)** → total.
  - `record_trade` / `release_trade` accept three modes:
    - Legacy: `record_trade(id, exposure, pnl)` — no per-platform side effect.
    - Single-platform: `record_trade(id, exposure, platform="kalshi")`.
    - Cross-platform arb leg split: `record_trade(id, total, yes_platform=..., no_platform=..., yes_exposure=..., no_exposure=...)`.
- **`ExecutionEngine` (arbiter/execution/engine.py)**
  - `__init__` now wires `RiskManager(config.scanner, safety_config=getattr(config, "safety", None))` so production always gets the SafetyConfig-bound per-platform ceiling.
  - `execute_opportunity`'s early-return rejection path now calls the new helper `_emit_rejection_incident(opp, reason)` before returning `None`, and logs at INFO level (was DEBUG) so rejections are visible in ops logs.
  - New `_emit_rejection_incident` helper constructs metadata:
    - `event_type: "order_rejected"` (consumed by the existing incident WS event; plan 03-07 adds a filtered view)
    - `rejection_type: <taxonomy>` (see below)
    - `reason: <full reason string>`
    - `yes_platform`, `no_platform`, `canonical_id`, `suggested_qty`
    - `platform: <name>` only when `rejection_type == "per_platform"`
  - Generates a synthetic `REJ-<millis>-<uuid4hex[:4]>` arb_id (distinct from real ARB-* ids) so rejected orders don't collide with executions.
  - `update_manual_position` extended — all `record_trade` / `release_trade` call sites now pass `yes_platform`/`no_platform`/`yes_exposure`/`no_exposure` splits so `_platform_exposures` stays consistent with `_open_positions` across the manual-entry lifecycle.

## `rejection_type` Taxonomy

| rejection_type   | Triggered when `check_trade` returns                          | Severity | Structured `platform` key? |
|------------------|---------------------------------------------------------------|----------|----------------------------|
| `per_market`     | `"Per-market exposure limit exceeded"`                        | info     | no                         |
| `per_platform`   | `"Per-platform exposure limit exceeded on {p}"`               | info     | yes                        |
| `total_exposure` | `"Total exposure limit exceeded"`                             | info     | no                         |
| `daily_loss`     | `"Daily loss limit reached"`                                  | info     | no                         |
| `daily_trades`   | `"Daily trade limit reached"`                                 | info     | no                         |
| `stale_quote`    | `"Stale quote: {age}s"`                                       | info     | no                         |
| `low_confidence` | `"Low confidence: {x}"`                                       | info     | no                         |
| `thin_edge`      | `"Edge too thin: {x}¢"`                                       | info     | no                         |
| `not_ready`      | `"Opportunity not ready: {status}"`                           | info     | no                         |
| `unknown`        | any future RiskManager reason that doesn't match the prefixes | info     | no                         |

Reason-string-based dispatch is a lightweight anti-coupling: RiskManager stays single-responsibility (returns reason text), and `_emit_rejection_incident` is the single hop that classifies.

## How `order_rejected` Flows to the Dashboard

1. `RiskManager.check_trade` returns `(False, reason)`.
2. `ExecutionEngine.execute_opportunity` calls `await self._emit_rejection_incident(opp, reason)`.
3. `_emit_rejection_incident` calls `_record_incident` which hits `self.record_incident`.
4. `record_incident` appends to `self._incidents` (200-entry deque), fans out to every `_incident_subscribers` queue, and persists via `self.store.insert_incident(...)` when a Postgres store is wired.
5. `arbiter/api.py`'s existing `_broadcast_loop` already drains `engine.subscribe_incidents()` and emits the generic `incident` WebSocket event with the full ExecutionIncident.to_dict() payload.
6. `arbiter/web/dashboard.js`'s existing `renderIncidentQueue` renders the message text — `order_rejected` incidents land in the existing incident panel with no visual break.

**No new WebSocket event type was introduced.** Plan 03-07 will read `incident.metadata.event_type === "order_rejected"` to render a filtered "Rejected orders" sub-view inside the risk section.

## Tests — all green

Added to `arbiter/execution/test_engine.py`:

| Test                                                | Verifies                                                                                   |
|-----------------------------------------------------|---------------------------------------------------------------------------------------------|
| `test_risk_per_platform_limit`                      | Per-platform rejection fires when a $60 Kalshi leg would push Kalshi to $310 > $300 limit |
| `test_risk_per_platform_allows_within_limit`        | Same opp with $100 prior Kalshi exposure → $160 < $300 → approved                         |
| `test_risk_per_market_limit_still_fires`            | Regression: $400 prior + $150 new on MKT1 → "Per-market" reason emitted                   |
| `test_rejected_order_emits_incident`                | Stale-quote opp → ExecutionIncident with severity=info, metadata.event_type=order_rejected |
| `test_rejected_order_incident_per_platform`         | Per-platform rejection → rejection_type='per_platform' AND platform='kalshi' in metadata |

Full `pytest arbiter/execution/test_engine.py -x` → **25 passed** (20 pre-existing + 5 new).

Broader regression: `pytest arbiter/safety/ arbiter/test_api_safety.py arbiter/execution/ arbiter/test_config_loading.py arbiter/test_readiness.py` → **120 passed, 4 skipped, 1 pre-existing failure** (see Deferred).

## Deviations from Plan

### [Rule 1 — Bug] Bulk stress test fixture blocked by new default

- **Found during:** Task 1 regression sweep.
- **Issue:** `test_bulk_dry_run_executes_120_opportunities` failed after Task 1 because `make_engine` did not patch the new `config.safety.max_platform_exposure_usd` default of $300. The test runs 120 consecutive `$0.10` polymarket legs ($12 each) against a single engine, accumulating $1,200 on polymarket — well past $300.
- **Fix:** Bumped `config.safety.max_platform_exposure_usd = 1_000_000.0` in the test `make_engine` helper alongside the existing `_max_total_exposure = 50_000` loosening. No production code change — the fixture merely acknowledges that synthetic bulk tests need wider ceilings than the production SafetyConfig default.
- **Files modified:** `arbiter/execution/test_engine.py` (make_engine helper)
- **Commit:** `e6d0ab1`

### [Rule 2 — Missing Functionality] Manual-position lifecycle needs per-platform splits

- **Found during:** Task 1 implementation audit of all `record_trade` / `release_trade` call sites.
- **Issue:** The plan's `<behavior>` mentions ExecutionEngine record_trade call sites need per-platform extension. The `update_manual_position` branch (around engine.py:1150-1163) had three record_trade/release_trade calls that the plan description covered only implicitly ("Find all existing self.risk.record_trade(...) call sites and extend"). Without this fix, `_platform_exposures` would drift out-of-sync with `_open_positions` across the manual-entry lifecycle — specifically, manual entries would count toward per-market but not per-platform, creating a blind spot where a manually-entered $290 Kalshi position would NOT block a subsequent $60 Kalshi auto-trade.
- **Fix:** Extended all three `record_trade` / `release_trade` calls in `update_manual_position` to pass `yes_platform`, `no_platform`, `yes_exposure`, `no_exposure`.
- **Files modified:** `arbiter/execution/engine.py` (update_manual_position branch)
- **Commit:** `e6d0ab1`

### [Rule 2 — Missing Functionality] Added `not_ready` rejection_type

- **Found during:** Task 1 implementation of `_emit_rejection_incident`.
- **Issue:** The plan's rejection_type taxonomy lists 8 types but does not cover `"Opportunity not ready: {status}"` which is the first check in `check_trade`. An incident with `rejection_type="unknown"` would lose useful operator context.
- **Fix:** Added a `"not ready" in r` branch that maps to `rejection_type="not_ready"` before the `"unknown"` fallthrough.
- **Files modified:** `arbiter/execution/engine.py::_emit_rejection_incident`
- **Commit:** `e6d0ab1`

## Deferred Issues

### `test_complete_stub_satisfies_protocol` — pre-existing failure in base commit

Detailed in `.planning/phases/03-safety-layer/deferred-items.md`. Verified pre-existing by `git stash` + repro. Scope: plan 03-05 (cancel_all impl) or a targeted Protocol-conformance fix plan.

## Authentication Gates

**None encountered.** All tests use in-process fakes; no live credentials required.

## Threat Flags

**None.** The plan's `<threat_model>` covered:
- T-3-02-A (TOCTOU): unchanged — single asyncio coroutine ensures no intra-process race.
- T-3-02-B (float overflow): unchanged — same `suggested_qty * price` math, bounded by ScannerConfig.max_position_usd.
- T-3-02-C (info disclosure): unchanged — incident stream remains public read-only WS.
- T-3-02-D (adapter bypass): unchanged — adapters still only constructed inside ExecutionEngine.
- T-3-02-E (repudiation): **strengthened** — every rejection now logs at INFO (was DEBUG) AND emits a persisted incident (when Postgres store is wired).

No new attack surface introduced.

## SAFE-02 Observable Truths — all met

- [x] Per-platform limit rejects an opportunity when the sum of existing per-platform exposure plus the new leg exposure on that platform exceeds `SafetyConfig.max_platform_exposure_usd` (`test_risk_per_platform_limit`).
- [x] Per-market limit check still rejects when `(existing + exposure)` on a single `canonical_id` exceeds `config.max_position_usd` (`test_risk_per_market_limit_still_fires`).
- [x] Every rejection emits a structured ExecutionIncident with `severity='info'`, `metadata.event_type='order_rejected'`, and `reason` text identifying which limit fired (`test_rejected_order_emits_incident`, `test_rejected_order_incident_per_platform`).
- [x] Rejected incidents flow through the existing `incident_subscribers` queue so `api.py` broadcasts them to the dashboard as the generic incident event — no new event type (verified by grep in dashboard.js + unchanged renderIncidentQueue).
- [x] Unit tests assert: per-platform rejection fires when a $290 Kalshi exposure would push Kalshi over $300 (`test_rejected_order_incident_per_platform`); per-platform does NOT fire if the same exposure fits within both platforms (`test_risk_per_platform_allows_within_limit`); per-market rejection still fires for single-market breach (`test_risk_per_market_limit_still_fires`).

## Commits

| Task | Message                                                                                         | Commit    |
|------|-------------------------------------------------------------------------------------------------|-----------|
| 0    | test(03-02): add failing tests for per-platform RiskManager + order_rejected incident           | `96c9932` |
| 1    | feat(03-02): RiskManager per-platform exposure + order_rejected incident                        | `e6d0ab1` |

## Self-Check: PASSED

- **Files modified (2):** `arbiter/execution/engine.py`, `arbiter/execution/test_engine.py` — verified via `git status` + `git log --stat`.
- **Files created (1):** `.planning/phases/03-safety-layer/deferred-items.md` — verified present.
- **Commits (2):** `96c9932`, `e6d0ab1` — verified in `git log --oneline`.
- **Test suite:** `pytest arbiter/execution/test_engine.py -x` → 25 passed, 0 failed.
- **Backward compat:** `python -c "from arbiter.execution.engine import RiskManager; rm = RiskManager(c.scanner); rm.record_trade('X', 10.0); print('OK')"` → exits 0.
- **Acceptance greps all passed:** `_platform_exposures` ≥ 4 (actual 11 lines), `Per-platform exposure limit exceeded on` ≥ 1 (actual 2), `async def _emit_rejection_incident` = 1, `event_type.*order_rejected` ≥ 1, `rejection_type` ≥ 1 (actual 11), `incident` in dashboard.js = 62 (unchanged).
