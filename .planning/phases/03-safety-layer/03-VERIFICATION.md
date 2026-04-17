---
phase: 03-safety-layer
verified: 2026-04-16T21:25:00Z
status: gaps_found
score: 17/18
overrides_applied: 0
gaps:
  - truth: "Per-platform exposure tracking fires on filled status only — submitted live orders do not register exposure until both legs confirm"
    status: partial
    reason: "In _live_execution (engine.py line 814-824), record_trade is called only when status == 'filled'. When status == 'submitted' (both orders dispatched but confirmation pending), per-platform exposure accumulates zero. A burst of 'submitted' in-flight orders can allow a subsequent check_trade to pass the per-platform ceiling check before prior fills land. Dry-run path (_simulate_execution line 759) correctly calls record_trade unconditionally."
    artifacts:
      - path: "arbiter/execution/engine.py"
        issue: "Lines 814-824: record_trade guarded by `if status == 'filled':` inside the `if status in {'submitted', 'filled'}:` block. The outer condition is correct; the inner guard is too strict for live-mode per-platform accounting."
    missing:
      - "Either (a) add record_trade call for the 'submitted' state with the same leg-split kwargs, or (b) add a parallel _platform_exposures increment at order-submission time (before confirmation) and decrement it on fill/cancel — whichever matches the intended accounting model."
human_verification:
  - test: "Operator kill-switch ARM + RESET end-to-end"
    expected: "ARM button triggers window.confirm + prompt; badge flips to ARMED (red); Reset appears with 30s cooldown; after cooldown Reset resets to Disarmed. Backend logs show trip_kill + adapter.cancel_all."
    why_human: "WebSocket + HTTP auth round-trip with browser UI; confirm modal interaction not automatable in pytest"
  - test: "Shutdown banner visibility before WebSocket close"
    expected: "Ctrl+C on running server causes #shutdownBanner to show 'Server shutting down — cancelling open orders' before the WS close event; after phase=complete no auto-reconnect"
    why_human: "Requires running server + open browser session; sequence timing not assertable in automated tests"
  - test: "Rate-limit pills color transition under load"
    expected: "Dashboard pills transition from green (.ok) to amber (.warn) when adapters are throttled"
    why_human: "Requires generating real throttle state under load; cannot assert color without a browser session"
---

# Phase 3: Safety Layer — Verification Report

**Phase Goal:** Safety Layer — implement kill-switch (SafetySupervisor + POST /api/kill-switch + audit), per-platform exposure limits + order_rejected incidents, one-leg exposure hero alerting, per-adapter rate limiting with 429 handling + periodic broadcast, graceful shutdown ordering (cancel_all before task cancel), market-mapping resolution-criteria schema, and operator-facing dashboard consolidation for all of the above.
**Verified:** 2026-04-16T21:25:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | SafetySupervisor.trip_kill() cancels all open orders across adapters within 5 seconds and emits kill_switch WS event | VERIFIED | supervisor.py asyncio.gather with wait_for(5.0) per adapter; _publish({"type": "kill_switch"}) at end of trip_kill; test_supervisor.py 13/13 passing |
| 2 | Once armed, ExecutionEngine.execute_opportunity rejects every new opportunity until reset | VERIFIED | allow_execution returns (False, "Kill switch armed: ...", state.to_dict()) when _state.armed; chained_gate wired in main.py line 300; test_allow_execution_armed passes |
| 3 | POST /api/kill-switch with action=arm requires operator auth (401 unauthenticated) and a reason body field (400 when empty) | VERIFIED | api.py line 589: await require_auth(request) as first call; line 603: returns 400 when not reason; test_api_safety.py 4 passing |
| 4 | POST /api/kill-switch with action=reset respects min_cooldown_seconds — 400 while cooldown not elapsed | VERIFIED | supervisor.py reset_kill lines 212-215: raises ValueError("Kill switch cooldown: ...") when now < cooldown_until; api.py line 619-620: except ValueError returns 400 |
| 5 | Telegram notifier sends kill_armed message when trip_kill succeeds; send failures do NOT abort trip_kill | VERIFIED | supervisor.py lines 155-163: try/except around notifier.send; test_telegram_failure_does_not_abort_trip passes |
| 6 | safety_events Postgres table captures one INSERT per arm/reset (append-only, never UPDATE/DELETE) | VERIFIED | safety_events.sql CREATE TABLE confirmed; persistence.py exposes only insert_safety_event + list_events (no UPDATE/DELETE); SafetyEventStore INSERT-only |
| 7 | Concurrent arm attempts — exactly one trip goes through; others see armed state and no-op | VERIFIED | supervisor.py asyncio.Lock serializes trip_kill; if self._state.armed: return early at line 136; test_concurrent_arm_serializes passes |
| 8 | Dashboard WS client receives kill_switch payload and does not throw on the new type | VERIFIED | dashboard.js line 1136: else if (message.type === "kill_switch") { state.safety = {..., killSwitch: message.payload}; }; node --check passes |
| 9 | Per-platform exposure limits check both legs before order submission and emit order_rejected incidents | PARTIAL — gap on submitted state | check_trade (engine.py lines 248-267) correctly checks both legs at pre-trade time; order_rejected incident emitted via _emit_rejection_incident (line 413). However record_trade only fires on 'filled' not 'submitted' — exposure register is incomplete for in-flight live orders (CR-02). Dry-run path correct. |
| 10 | One-leg exposure: when exactly one leg fills, structured one_leg_exposure incident emitted + Telegram + dedicated WS event | VERIFIED | engine.py _recover_one_leg_risk lines 996-1032: emits incident with event_type="one_leg_exposure" metadata; self._safety.handle_one_leg_exposure called; supervisor.py handle_one_leg_exposure fires Telegram + _publish({"type": "one_leg_exposure", "payload": ...}) |
| 11 | Every outbound call from KalshiAdapter and PolymarketAdapter acquires rate_limiter token before HTTP/SDK I/O | VERIFIED | kalshi.py: 5 acquire() calls across all outbound methods; polymarket.py: 4 acquire() calls; all outbound methods covered; execution tests 113 passing |
| 12 | On HTTP 429, adapter invokes apply_retry_after + records circuit failure + returns FAILED Order; FOK never retries | VERIFIED | kalshi.py lines 128-144: 429 branch with apply_retry_after; circuit.record_failure(); returns _failed_order with "rate_limited"; polymarket.py lines 172-177: same pattern; no retry after 429 for FOK |
| 13 | api.py runs periodic _rate_limit_broadcast_loop emitting rate_limit_state WS event every 2 seconds; stats expose available_tokens/max_requests/remaining_penalty_seconds | VERIFIED | api.py line 826: _rate_limit_broadcast_loop method exists; line 230: task created; retry.py lines 325/329/331: all three stats fields present |
| 14 | On SIGINT/SIGTERM, main.py calls safety.trip_kill BEFORE any task.cancel(); second SIGINT triggers os._exit(1) | VERIFIED | main.py line 417: await run_shutdown_sequence(safety, tasks); run_shutdown_sequence lines 131-144: prepare_shutdown before task.cancel loop; line 396: os._exit(1) on second signal; test_graceful_shutdown_cancels_orders_before_tasks passes (3/3) |
| 15 | KalshiAdapter.cancel_all chunks orders in 20-sized slices with one rate-limit token per chunk; PolymarketAdapter.cancel_all uses run_in_executor(client.cancel_all) | VERIFIED | kalshi.py: CANCEL_ALL_CHUNK_SIZE=20 line 274; chunking loop lines 298-404; per-chunk acquire() line 310; batched DELETE path; polymarket.py: run_in_executor(client.cancel_all()) line 422; acquire() line 418 |
| 16 | safety.prepare_shutdown() broadcasts shutdown_state with phase='shutting_down' BEFORE trip_kill; phase='complete' in finally | VERIFIED | supervisor.py lines 285-308: _publish shutdown_state before trip_kill; finally block publishes phase=complete; test_prepare_shutdown_broadcasts_before_trip passes |
| 17 | MARKET_MAP schema accepts optional resolution_criteria dict; MarketMapping dataclass exposes it; GET + POST /api/market-mappings handles it; idempotent SQL migration; mapping_state WS event fires on update; dashboard state captures event | VERIFIED | settings.py: resolution_criteria field on MarketMappingRecord; market_map.py: resolution_criteria_json + resolution_match_status on dataclass; init.sql: ADD COLUMN IF NOT EXISTS; api.py line 435: _broadcast_json mapping_state on update; dashboard.js line 1145: else if (message.type === "mapping_state") { state.mappingUpdates...}; 3 config tests passing |
| 18 | Dashboard renders kill-switch panel (ARM/RESET/cooldown/badge) + rate-limit pills + one-leg alert + shutdown banner + mapping resolution comparison; index.html and dashboard.html in parity; textContent XSS guard | VERIFIED | dashboard.html lines 178-212: safetySection, killSwitchArm, oneLegAlertPanel, shutdownBanner; same selectors in index.html confirmed by grep; dashboard.js: renderSafetyPanel, renderRateLimitBadges, renderOneLegAlert, renderShutdownBanner all defined; buildSafetyView/buildRateLimitView/buildMappingComparison exported from dashboard-view-model.js; 11/12 vitest pass (1 pre-existing label-drift failure unrelated to Phase 3 — see deferred-items.md) |

**Score:** 17/18 truths verified (1 partial gap on SAFE-02 live-mode exposure tracking)

---

### Deferred Items

None — all items addressed within Phase 3 scope.

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `arbiter/safety/__init__.py` | Safety package namespace; exports SafetySupervisor, SafetyState, SafetyAlertTemplates, SafetyEventStore | VERIFIED | Exists; imports confirmed working |
| `arbiter/safety/supervisor.py` | SafetySupervisor class + SafetyState + SafetyConfig wiring; allow_execution, trip_kill, reset_kill, subscribe, prepare_shutdown, handle_one_leg_exposure | VERIFIED | 442 lines; all required methods present |
| `arbiter/safety/alerts.py` | SafetyAlertTemplates with kill_armed/kill_reset/one_leg_exposure static methods | VERIFIED | 59 lines; all three static methods present |
| `arbiter/safety/persistence.py` | SafetyEventStore (asyncpg INSERT-only) + RedisStateShim | VERIFIED | 159 lines; INSERT-only confirmed; no UPDATE/DELETE methods |
| `arbiter/sql/safety_events.sql` | CREATE TABLE safety_events (append-only) + index | VERIFIED | CREATE TABLE IF NOT EXISTS safety_events + CREATE INDEX |
| `arbiter/execution/adapters/base.py` | PlatformAdapter Protocol with async def cancel_all | VERIFIED | cancel_all in Protocol with docstring at lines 72-83 |
| `arbiter/execution/adapters/kalshi.py` | Full cancel_all with chunking + rate_limiter.acquire per chunk | VERIFIED | CANCEL_ALL_CHUNK_SIZE=20; chunking loop; per-chunk acquire; 429 handling |
| `arbiter/execution/adapters/polymarket.py` | Full cancel_all via run_in_executor(client.cancel_all) | VERIFIED | Lines 399-456; acquire before SDK call; rate-limit error detection |
| `arbiter/utils/retry.py` | RateLimiter.stats exposes available_tokens/max_requests/remaining_penalty_seconds | VERIFIED | Lines 325/329/331: all three fields confirmed |
| `arbiter/api.py` | POST /api/kill-switch + GET /api/safety/status + GET /api/safety/events + _rate_limit_broadcast_loop + mapping_state broadcast + rate_limits in _build_system_snapshot | VERIFIED | All routes registered; loop at line 826; snapshot includes safety + rate_limits keys |
| `arbiter/main.py` | run_shutdown_sequence helper + chained_gate + os._exit(1) | VERIFIED | run_shutdown_sequence at line 106; chained_gate at line 289; os._exit(1) at line 396 |
| `arbiter/sql/init.sql` | ALTER TABLE market_mappings ADD COLUMN IF NOT EXISTS resolution_criteria JSONB + resolution_match_status | VERIFIED | Lines 65-66 confirmed |
| `arbiter/web/dashboard.html` | safetySection + shutdownBanner + oneLegAlertPanel markup | VERIFIED | Lines 178-212 |
| `arbiter/web/dashboard.js` | renderSafetyPanel + renderRateLimitBadges + renderOneLegAlert + renderShutdownBanner + all 5 WS tolerance branches + click handlers | VERIFIED | All render functions at lines 1341/1374/1388/1437; WS branches at lines 1136-1148 |
| `arbiter/web/dashboard-view-model.js` | buildSafetyView + buildRateLimitView + buildMappingComparison exported | VERIFIED | Lines 213/234/264 |
| `arbiter/web/styles.css` | kill-switch-controls, rate-limit-pill (.ok/.warn/.crit), one-leg-pulse animation, shutdown-banner, criteria-chip | VERIFIED | All selectors present; @keyframes one-leg-pulse at line 4267 |
| `index.html` | Safety markup parity with dashboard.html | VERIFIED | safetySection/killSwitchArm/shutdownBanner/oneLegAlertPanel all confirmed present at same line numbers as dashboard.html |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| arbiter/main.py | SafetySupervisor.allow_execution | chained_gate wiring engine.set_trade_gate(chained_gate) | VERIFIED | Lines 289-300 in main.py |
| arbiter/execution/engine.py::execute_opportunity | SafetySupervisor.allow_execution | existing _check_trade_gate hook | VERIFIED | Line 437: gate_allowed, gate_reason, gate_context = await self._check_trade_gate(opp) |
| arbiter/api.py::_broadcast_loop | SafetySupervisor.subscribe() | safety_queue task in asyncio.wait | VERIFIED | Line 764: safety_queue = self.safety.subscribe() if self.safety is not None else None; line 780: asyncio tasks |
| arbiter/web/dashboard.js WS handler | state.safety.killSwitch | else if branch for message.type === "kill_switch" | VERIFIED | Line 1136 |
| arbiter/execution/adapters/kalshi.py | arbiter/utils/retry.py::RateLimiter.acquire | await self.rate_limiter.acquire() before HTTP I/O | VERIFIED | 5 acquire() calls; place_fok calls it in _post_order at line 223; cancel_order at 241; get_order at 519; list_open_orders_by_client_id at 587; cancel_all per-chunk at 310 |
| arbiter/execution/adapters/kalshi.py | RateLimiter.apply_retry_after | on resp.status == 429 | VERIFIED | Lines 130-134: 429 branch with apply_retry_after; reason="kalshi_429" |
| arbiter/api.py::_rate_limit_broadcast_loop | arbiter/web/dashboard.js state.safety.rateLimits | rate_limit_state WS event every 2 seconds | VERIFIED | Loop at line 826; dashboard.js line 1139 captures payload into state.safety.rateLimits |
| arbiter/api.py::_build_system_snapshot | /api/system JSON response | adds rate_limits top-level key | VERIFIED | Lines 907-913: rate_limits dict comprehension over engine.adapters |
| arbiter/main.py::run_shutdown_sequence | SafetySupervisor.prepare_shutdown → trip_kill | asyncio.wait_for(prepare_shutdown(), timeout=5.0) before task.cancel | VERIFIED | Line 131: await asyncio.wait_for(safety.prepare_shutdown(), timeout=timeout) |
| arbiter/safety/supervisor.py::trip_kill | adapter.cancel_all across all adapters | asyncio.gather with per-adapter wait_for(5.0) | VERIFIED | Lines 437-441: asyncio.gather; _cancel_one wraps wait_for(adapter.cancel_all(), timeout=5.0) |
| arbiter/api.py::handle_market_mapping_action | update_market_mapping with resolution_criteria kwarg | resolution_criteria kwarg passed through; mapping_state broadcast on success | VERIFIED | Lines 361-438: resolution_criteria extracted from payload; update_kwargs["resolution_criteria"] assigned; _broadcast_json mapping_state |
| arbiter/web/dashboard.js WS handler | state.mappingUpdates | else if branch for message.type === "mapping_state" | VERIFIED | Lines 1145-1147 |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|-------------------|--------|
| dashboard.js renderSafetyPanel | state.safety.killSwitch | kill_switch WS event → SafetySupervisor._publish → api._broadcast_loop | Yes — SafetyState.to_dict() populated from real trip_kill state machine | FLOWING |
| dashboard.js renderRateLimitBadges | state.safety.rateLimits | rate_limit_state WS event → _rate_limit_broadcast_loop → RateLimiter.stats | Yes — stats reads live token bucket state | FLOWING |
| dashboard.js renderOneLegAlert | state.oneLegExposures | one_leg_exposure WS event → handle_one_leg_exposure → _publish | Yes — populated from real incident metadata when naked position detected | FLOWING |
| dashboard.js renderShutdownBanner | state.shutdown | shutdown_state WS event → prepare_shutdown → _publish | Yes — real shutdown phases from process signal handler | FLOWING |
| api.py _build_system_snapshot / safety key | self.safety._state.to_dict() | SafetyState populated by trip_kill/reset_kill | Yes — live SafetyState object | FLOWING |
| api.py _build_system_snapshot / rate_limits key | adapter.rate_limiter.stats | RateLimiter instances on KalshiAdapter/PolymarketAdapter | Yes — live token bucket state | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Python imports (safety module) | `python -c "from arbiter.safety import SafetySupervisor, SafetyState, SafetyAlertTemplates, SafetyEventStore; from arbiter.config.settings import SafetyConfig; print('OK')"` | imports OK | PASS |
| main.py syntax valid | `python -c "import ast; ast.parse(open('arbiter/main.py').read())"` | exits 0 | PASS |
| Safety unit tests | `pytest arbiter/safety/ -x -q` | 13 passed, 1 skipped | PASS |
| API safety tests | `pytest arbiter/test_api_safety.py -x -q` | 4 passed | PASS |
| Adapter tests (rate limiting + cancel_all) | `pytest arbiter/execution/ -x -q` | 113 passed, 2 skipped | PASS |
| Shutdown sequence test | `pytest arbiter/test_main_shutdown.py -x -q` | 3 passed (5.5s — includes 5s timeout test) | PASS |
| Config loading (resolution_criteria) | `pytest arbiter/test_config_loading.py -k resolution_criteria -q` | 3 passed | PASS |
| Dashboard JS syntax | `node --check arbiter/web/dashboard.js` | exits 0 | PASS |
| Vitest view-model helpers | `npx vitest run arbiter/web/dashboard-view-model.test.js` | 11/12 passed (1 pre-existing failure: buildMetricCards label drift — not a Phase 3 item; documented in deferred-items.md) | PASS (Phase 3 tests) |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| SAFE-01 | 03-01 | Kill switch cancels open orders, halts execution, Telegram alert, dashboard + programmatic trigger | SATISFIED | SafetySupervisor + POST /api/kill-switch + safety_events table + WS kill_switch event all present and tested |
| SAFE-02 | 03-02 (inferred from engine.py) | Position limits enforced per-platform and per-market before order submission | PARTIAL | check_trade enforces per-platform ceiling correctly at pre-trade time; order_rejected incidents emitted. GAP: record_trade only fires on 'filled' not 'submitted' in live execution — in-flight submitted orders escape exposure accounting |
| SAFE-03 | 03-03 (inferred from engine.py, supervisor.py) | One-leg recovery detects naked positions and executes automated or operator-assisted unwind | SATISFIED | _recover_one_leg_risk emits structured incident; handle_one_leg_exposure fires Telegram + WS; dashboard hero alert present |
| SAFE-04 | 03-04 | Per-platform API rate limiting prevents throttling/bans; Kalshi 10 writes/sec | SATISFIED | RateLimiter.acquire() in all outbound methods; 429 handling with apply_retry_after; _rate_limit_broadcast_loop; /api/system rate_limits key |
| SAFE-05 | 03-05 | Graceful shutdown cancels all open orders before process exit (SIGINT/SIGTERM) | SATISFIED | run_shutdown_sequence; prepare_shutdown publishes shutdown_state before trip_kill; KalshiAdapter cancel_all chunked; PolymarketAdapter cancel_all via SDK; second-signal os._exit(1) |
| SAFE-06 | 03-06 | Market mapping resolution criteria comparison — operator must verify both platforms resolve identically | SATISFIED | MarketMappingRecord + MarketMapping both have resolution_criteria; ALTER TABLE migration; POST handler accepts and broadcasts; mapping_state WS event; dashboard state captures it |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| arbiter/execution/engine.py | 815 | record_trade guarded by `if status == 'filled':` only — submitted live orders bypass per-platform exposure accumulation | Blocker (SAFE-02) | In live mode, two rapid arb trades on the same platform can both pass check_trade if the first is in 'submitted' state when the second pre-trade check runs. In dry-run mode this is not an issue (simulate path calls record_trade unconditionally). |
| arbiter/web/dashboard-view-model.test.js | 123 | Pre-existing test expects 'Validator progress' but buildMetricCards returns 'Validator state' | Warning | 1 vitest failure on every run; pre-dates Phase 3 and is documented in deferred-items.md. Not blocking. |
| arbiter/web/styles.css | — | .mapping-compare-grid CSS selector not found | Info | Plan 03-07 PLAN.md specifies the selector but the styles.css implementation appears to use different class names (possibly .mapping-compare-column and grid via inline rendering). Dashboard visually correct per operator approval signal. |

---

### Human Verification Required

### 1. Kill-Switch ARM/RESET End-to-End

**Test:** Open index.html in browser, sign in as operator. Click "ARM KILL SWITCH" button, confirm modal, enter reason. Verify badge flips to ARMED (red), cooldown label appears. Wait 30s, verify Reset enables. Click Reset, confirm; verify badge flips to Disarmed (green).
**Expected:** Full ARM→cooldown→RESET cycle completes without errors. Backend /api/safety/status reflects state changes. Telegram alert fired (if token configured).
**Why human:** Browser confirm/prompt dialogs + WebSocket round-trip + visual badge state not assertable in pytest.

### 2. Shutdown Banner Sequence

**Test:** Start `python -m arbiter.main --dry-run` (or `--api-only`), open dashboard in browser, send Ctrl+C. Observe dashboard before WebSocket closes.
**Expected:** `#shutdownBanner` becomes visible with "Server shutting down — cancelling open orders" text BEFORE the WS close event fires. After phase=complete the banner text updates to "Server shutdown complete" and the dashboard does NOT auto-reconnect.
**Why human:** Requires running server + live browser session; timing assertion across WS events not automatable.

### 3. Rate-Limit Pill Color Transitions

**Test:** Run system under high load to trigger rate-limiter penalty state. Observe rate-limit pill colors on dashboard.
**Expected:** Pills transition from green (.ok) to amber (.warn) as penalty_seconds > 0; return to green when penalty clears.
**Why human:** Requires generating real throttle state by hammering venue API limits; not producible in unit tests.

---

### Gaps Summary

**1 gap blocking SAFE-02 full satisfaction (live-mode per-platform exposure tracking):**

The per-platform exposure ceiling check runs correctly at pre-trade time in `RiskManager.check_trade` — both legs' dollar exposure is compared against `SafetyConfig.max_platform_exposure_usd` before any order is placed. The `order_rejected` incident is emitted correctly when the check fails.

However, `record_trade` (which accumulates `_platform_exposures` for subsequent checks) only fires when `status == "filled"` inside `_live_execution`. A trade that reaches `status == "submitted"` (both orders dispatched and awaiting fill confirmation) does not register its exposure. During the window between submission and fill confirmation — typically seconds for FOK orders but potentially longer under latency — a concurrent opportunity on the same platform can pass the check_trade guard with stale exposure data.

**Risk assessment:** For the current small-capital phase ($300 max per platform), FOK orders fill or cancel quickly. The window is narrow. However it is a real gap that should be addressed before higher volume operation.

**Fix:** Add `record_trade` call in `_live_execution` for the `submitted` state (using the same leg-split kwargs), or alternatively register exposure at order-submission time in `_place_order_for_leg` and release on cancellation.

---

_Verified: 2026-04-16T21:25:00Z_
_Verifier: Claude (gsd-verifier)_
