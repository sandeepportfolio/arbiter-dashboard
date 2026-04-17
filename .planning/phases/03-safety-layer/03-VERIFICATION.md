---
phase: 03-safety-layer
verified: 2026-04-16T23:00:00Z
status: human_needed
score: 18/18
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 17/18
  gaps_closed:
    - "Per-platform exposure tracking fires on filled status only — SAFE-02 live-mode gap closed by plan 03-08"
  gaps_remaining: []
  regressions: []
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
notes:
  - id: UI-CONTRACT-DRIFT
    severity: warning
    description: "test_api_integration.py::test_api_and_dashboard_contracts FAILs because the test asserts 'ARBITER LIVE' in the /ops page HTML body, but commit e4c3411 (plan 03-07 UI consolidation) restructured dashboard.html so the literal 'ARBITER LIVE' no longer appears in the ops variant. The test was written (commit 26ef529) when that string existed in the pre-03-07 dashboard. This failure is pre-existing as of plan 03-07 and is NOT caused by plan 03-08. It is NOT a SAFE-01..SAFE-06 regression — it is a cross-phase UI contract drift between the test suite and the consolidated dashboard markup."
    affected_file: "arbiter/test_api_integration.py"
    affected_test: "test_api_and_dashboard_contracts (line 82)"
    introduced_by_commit: "e4c3411 feat(03-07): consolidate Phase 3 safety layer into operator dashboard UI"
    recommended_fix: "Update the assertion on line 82 of arbiter/test_api_integration.py to match the actual heading text present in the /ops HTML (e.g., change to assert 'ARBITER' in ops_html or assert 'Live Desk' in ops_html to match the <title> 'ARBITER Live Desk')."
---

# Phase 3: Safety Layer — Verification Report (Re-verification)

**Phase Goal:** The system cannot lose money due to runaway execution, naked positions, rate limit bans, or uncontrolled shutdown — every dangerous scenario has a safety mechanism.
**Verified:** 2026-04-16T23:00:00Z
**Status:** human_needed
**Re-verification:** Yes — after gap closure (plan 03-08 closed SAFE-02 submitted-state gap)

---

## Re-verification Summary

Previous verification (2026-04-16T21:25:00Z): **gaps_found** (17/18 — SAFE-02 partial)

Plan 03-08 closed the SAFE-02 gap by:
- Removing the `if status == "filled":` inner guard in `_live_execution` so `record_trade` fires on both `"submitted"` and `"filled"` states.
- Adding a `elif status == "recovering":` branch that records only the surviving leg's exposure.
- Adding `release_trade` in `_recover_one_leg_risk` after a successful cancel, freeing the per-platform reservation.
- Adding 4 new pytest tests that prove the burst-window is closed, recovering-only-surviving-leg accounting, and release-on-cancel symmetry.

Current test suite: **137 passed, 3 skipped** (`arbiter/execution/ arbiter/safety/ arbiter/test_api_safety.py arbiter/test_main_shutdown.py`).

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | SafetySupervisor.trip_kill() cancels all open orders across adapters within 5 seconds and emits kill_switch WS event | VERIFIED | supervisor.py asyncio.gather with wait_for(5.0) per adapter; _publish({"type": "kill_switch"}) at end of trip_kill; all supervisor tests passing |
| 2 | Once armed, ExecutionEngine.execute_opportunity rejects every new opportunity until reset | VERIFIED | allow_execution returns (False, "Kill switch armed: ...", state.to_dict()) when _state.armed; chained_gate wired in main.py; test_allow_execution_armed passes |
| 3 | POST /api/kill-switch with action=arm requires operator auth (401 unauthenticated) and a reason body field (400 when empty) | VERIFIED | api.py: await require_auth(request) as first call; returns 400 when reason empty; test_api_safety.py 4/4 passing |
| 4 | POST /api/kill-switch with action=reset respects min_cooldown_seconds — 400 while cooldown not elapsed | VERIFIED | supervisor.py reset_kill: raises ValueError("Kill switch cooldown: ...") when now < cooldown_until; api.py except ValueError returns 400 |
| 5 | Telegram notifier sends kill_armed message when trip_kill succeeds; send failures do NOT abort trip_kill | VERIFIED | supervisor.py: try/except around notifier.send; test_telegram_failure_does_not_abort_trip passes |
| 6 | safety_events Postgres table captures one INSERT per arm/reset (append-only, never UPDATE/DELETE) | VERIFIED | safety_events.sql CREATE TABLE confirmed; persistence.py exposes only insert_safety_event + list_events (no UPDATE/DELETE) |
| 7 | Concurrent arm attempts — exactly one trip goes through; others see armed state and no-op | VERIFIED | supervisor.py asyncio.Lock serializes trip_kill; if self._state.armed: return early; test_concurrent_arm_serializes passes |
| 8 | Dashboard WS client receives kill_switch payload and does not throw on the new type | VERIFIED | dashboard.js: else if (message.type === "kill_switch") { state.safety = {...}; }; node --check passes |
| 9 | Per-platform exposure limits: check_trade enforces both legs before submission; record_trade tracks in-flight submitted orders; order_rejected incident emitted on rejection | VERIFIED | check_trade lines 248-267 checks both legs pre-trade; _emit_rejection_incident fires on rejection. SAFE-02 gap CLOSED by plan 03-08: inner `if status == "filled":` guard removed (grep confirms NO MATCHES); `elif status == "recovering":` branch added (exactly 1 match at line 847); release_trade added in _recover_one_leg_risk (3 release_trade calls total in engine.py); 4 new tests pass: test_live_burst_submitted_rejected_at_per_platform_ceiling, test_live_recovering_records_only_surviving_leg, test_live_recovery_cancellation_releases_reservation, test_dry_run_record_trade_unchanged_after_fix |
| 10 | One-leg exposure: when exactly one leg fills, structured one_leg_exposure incident emitted + Telegram + dedicated WS event | VERIFIED | engine.py _recover_one_leg_risk emits incident with event_type="one_leg_exposure" metadata; self._safety.handle_one_leg_exposure called; supervisor.py fires Telegram + _publish({"type": "one_leg_exposure"}) |
| 11 | Every outbound call from KalshiAdapter and PolymarketAdapter acquires rate_limiter token before HTTP/SDK I/O | VERIFIED | kalshi.py: 5 acquire() calls across all outbound methods; polymarket.py: 4 acquire() calls; 137 tests passing |
| 12 | On HTTP 429, adapter invokes apply_retry_after + records circuit failure + returns FAILED Order; FOK never retries | VERIFIED | kalshi.py: 429 branch with apply_retry_after; circuit.record_failure(); returns _failed_order("rate_limited"); polymarket.py: same pattern; no retry for FOK |
| 13 | api.py runs periodic _rate_limit_broadcast_loop emitting rate_limit_state WS every 2 seconds; stats expose available_tokens/max_requests/remaining_penalty_seconds | VERIFIED | api.py _rate_limit_broadcast_loop method exists; task created; retry.py: all three stats fields present |
| 14 | On SIGINT/SIGTERM, main.py calls safety.trip_kill BEFORE any task.cancel(); second SIGINT triggers os._exit(1) | VERIFIED | main.py: await run_shutdown_sequence(safety, tasks); run_shutdown_sequence: prepare_shutdown before task.cancel loop; os._exit(1) on second signal; test_graceful_shutdown_cancels_orders_before_tasks passes (3/3) |
| 15 | KalshiAdapter.cancel_all chunks in 20-sized slices with one rate-limit token per chunk; PolymarketAdapter.cancel_all uses run_in_executor(client.cancel_all) | VERIFIED | kalshi.py: CANCEL_ALL_CHUNK_SIZE=20; per-chunk acquire(); batched DELETE; polymarket.py: run_in_executor(client.cancel_all()) with acquire() before SDK call |
| 16 | safety.prepare_shutdown() broadcasts shutdown_state with phase='shutting_down' BEFORE trip_kill; phase='complete' in finally | VERIFIED | supervisor.py: _publish shutdown_state before trip_kill; finally block publishes phase=complete; test_prepare_shutdown_broadcasts_before_trip passes |
| 17 | MARKET_MAP schema accepts optional resolution_criteria dict; MarketMapping dataclass exposes it; GET + POST /api/market-mappings handles it; idempotent SQL migration; mapping_state WS event fires on update; dashboard state captures event | VERIFIED | settings.py: resolution_criteria field on MarketMappingRecord; market_map.py: resolution_criteria_json + resolution_match_status; init.sql: ADD COLUMN IF NOT EXISTS; api.py: _broadcast_json mapping_state on update; dashboard.js: else if (message.type === "mapping_state") { state.mappingUpdates... } |
| 18 | Dashboard renders kill-switch panel (ARM/RESET/cooldown/badge) + rate-limit pills + one-leg alert + shutdown banner + mapping resolution comparison; index.html and dashboard.html in parity; textContent XSS guard | VERIFIED | dashboard.html (committed HEAD): safetySection, killSwitchArm, oneLegAlertPanel, shutdownBanner all present (grep confirmed 1 each); index.html: same selectors present (grep confirmed 1 each); dashboard.js: renderSafetyPanel, renderRateLimitBadges, renderOneLegAlert, renderShutdownBanner all defined; buildSafetyView/buildRateLimitView/buildMappingComparison exported; node --check dashboard.js exits 0 |

**Score:** 18/18 truths verified

**Note on working tree:** The working tree has uncommitted modifications to arbiter/web/dashboard.html, dashboard.js, styles.css, and index.html that are NOT part of any committed plan. These WIP changes are ignored — verification is against committed HEAD (the 03-08 gap-closure commit 57a6234 + 577bd97 and the 03-07 UI commit e4c3411).

---

### Deferred Items

None — all items addressed within Phase 3 scope.

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `arbiter/safety/__init__.py` | Safety package namespace; exports SafetySupervisor, SafetyState, SafetyAlertTemplates, SafetyEventStore | VERIFIED | Exists; imports confirmed working |
| `arbiter/safety/supervisor.py` | SafetySupervisor + SafetyState + SafetyConfig wiring; all safety methods including prepare_shutdown + handle_one_leg_exposure | VERIFIED | All required methods present |
| `arbiter/safety/alerts.py` | SafetyAlertTemplates with kill_armed/kill_reset/one_leg_exposure static methods | VERIFIED | All three static methods; NAKED POSITION string present |
| `arbiter/safety/persistence.py` | SafetyEventStore (asyncpg INSERT-only) + RedisStateShim | VERIFIED | INSERT-only confirmed; no UPDATE/DELETE methods |
| `arbiter/sql/safety_events.sql` | CREATE TABLE safety_events (append-only) + index | VERIFIED | CREATE TABLE IF NOT EXISTS safety_events + CREATE INDEX confirmed |
| `arbiter/execution/adapters/base.py` | PlatformAdapter Protocol with async def cancel_all | VERIFIED | cancel_all in Protocol with docstring |
| `arbiter/execution/adapters/kalshi.py` | Full cancel_all with chunking + rate_limiter.acquire per chunk | VERIFIED | CANCEL_ALL_CHUNK_SIZE=20; per-chunk acquire; 429 handling |
| `arbiter/execution/adapters/polymarket.py` | Full cancel_all via run_in_executor(client.cancel_all) | VERIFIED | acquire before SDK call; rate-limit error detection |
| `arbiter/utils/retry.py` | RateLimiter.stats exposes available_tokens/max_requests/remaining_penalty_seconds | VERIFIED | All three fields confirmed |
| `arbiter/api.py` | POST /api/kill-switch + GET /api/safety/status + GET /api/safety/events + _rate_limit_broadcast_loop + mapping_state broadcast + rate_limits in _build_system_snapshot | VERIFIED | All routes registered; loop present; snapshot includes safety + rate_limits keys |
| `arbiter/main.py` | run_shutdown_sequence helper + chained_gate + os._exit(1) | VERIFIED | All three confirmed present |
| `arbiter/sql/init.sql` | ALTER TABLE market_mappings ADD COLUMN IF NOT EXISTS resolution_criteria JSONB + resolution_match_status | VERIFIED | Both ADD COLUMN IF NOT EXISTS lines confirmed |
| `arbiter/web/dashboard.html` | safetySection + shutdownBanner + oneLegAlertPanel markup | VERIFIED | All selectors present in committed HEAD |
| `arbiter/web/dashboard.js` | renderSafetyPanel + renderRateLimitBadges + renderOneLegAlert + renderShutdownBanner + all 5 WS tolerance branches + click handlers | VERIFIED | All render functions present; node --check passes |
| `arbiter/web/dashboard-view-model.js` | buildSafetyView + buildRateLimitView + buildMappingComparison exported | VERIFIED | All three exported functions confirmed |
| `arbiter/web/styles.css` | kill-switch-controls, rate-limit-pill (.ok/.warn/.crit), one-leg-pulse animation, shutdown-banner, criteria-chip | VERIFIED | All selectors present in committed HEAD |
| `index.html` | Safety markup parity with dashboard.html | VERIFIED | safetySection/killSwitchArm/shutdownBanner/oneLegAlertPanel all confirmed present in committed HEAD |
| `arbiter/execution/engine.py` | SAFE-02 gap closed: no inner `if status == "filled":` guard; elif status == "recovering": branch; release_trade in _recover_one_leg_risk | VERIFIED | grep confirms: 0 matches for `if status == "filled":`, exactly 1 match for `elif status == "recovering":`, 3 matches for `self.risk.release_trade` |
| `arbiter/execution/test_engine.py` | 4 new SAFE-02 gap tests | VERIFIED | All 4 tests present and passing: test_live_burst_submitted_rejected_at_per_platform_ceiling, test_live_recovering_records_only_surviving_leg, test_live_recovery_cancellation_releases_reservation, test_dry_run_record_trade_unchanged_after_fix |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| arbiter/main.py | SafetySupervisor.allow_execution | chained_gate wiring engine.set_trade_gate(chained_gate) | VERIFIED | chained_gate defined and registered in main.py |
| arbiter/execution/engine.py::execute_opportunity | SafetySupervisor.allow_execution | existing _check_trade_gate hook | VERIFIED | _check_trade_gate returns gate_allowed, gate_reason, gate_context |
| arbiter/api.py::_broadcast_loop | SafetySupervisor.subscribe() | safety_queue task in asyncio.wait | VERIFIED | safety_queue = self.safety.subscribe() if self.safety is not None else None |
| arbiter/web/dashboard.js WS handler | state.safety.killSwitch | else if branch for message.type === "kill_switch" | VERIFIED | Line present; node --check passes |
| arbiter/execution/adapters/kalshi.py | RateLimiter.acquire | await self.rate_limiter.acquire() before HTTP I/O | VERIFIED | 5 acquire() calls in outbound methods |
| arbiter/execution/adapters/kalshi.py | RateLimiter.apply_retry_after | on resp.status == 429 | VERIFIED | 429 branch with apply_retry_after; reason="kalshi_429" |
| arbiter/api.py::_rate_limit_broadcast_loop | dashboard.js state.safety.rateLimits | rate_limit_state WS event every 2 seconds | VERIFIED | Loop present; dashboard.js captures payload |
| arbiter/api.py::_build_system_snapshot | /api/system JSON response | adds rate_limits top-level key | VERIFIED | rate_limits dict comprehension present |
| arbiter/main.py::run_shutdown_sequence | SafetySupervisor.prepare_shutdown → trip_kill | asyncio.wait_for(prepare_shutdown(), timeout=5.0) before task.cancel | VERIFIED | wait_for(safety.prepare_shutdown(), timeout=timeout) before task.cancel loop |
| arbiter/safety/supervisor.py::trip_kill | adapter.cancel_all across all adapters | asyncio.gather with per-adapter wait_for(5.0) | VERIFIED | asyncio.gather; _cancel_one wraps wait_for(adapter.cancel_all(), timeout=5.0) |
| arbiter/api.py::handle_market_mapping_action | update_market_mapping with resolution_criteria kwarg | resolution_criteria kwarg passed through; mapping_state broadcast on success | VERIFIED | resolution_criteria extracted from payload; _broadcast_json mapping_state |
| arbiter/web/dashboard.js WS handler | state.mappingUpdates | else if branch for message.type === "mapping_state" | VERIFIED | Present in committed HEAD |
| arbiter/execution/engine.py::_live_execution | RiskManager.record_trade (submitted + filled branches) | `if status in {"submitted", "filled"}:` — no inner guard | VERIFIED (plan 03-08) | grep: 0 matches for inner `if status == "filled":` guard; record_trade now fires for both branches |
| arbiter/execution/engine.py::_live_execution | RiskManager.record_trade (recovering branch — surviving leg only) | `elif status == "recovering":` with surviving_platform logic | VERIFIED (plan 03-08) | Exactly 1 match at line 847 |
| arbiter/execution/engine.py::_recover_one_leg_risk | RiskManager.release_trade | after successful _cancel_order for SUBMITTED/PARTIAL leg | VERIFIED (plan 03-08) | release_trade call confirmed inside _recover_one_leg_risk |

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
| RiskManager._platform_exposures | submitted/filled/recovering exposure | _live_execution record_trade (plan 03-08 fix) | Yes — fires on submission, not just fill confirmation | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Python imports (safety + execution modules) | `python -c "from arbiter.safety import SafetySupervisor, SafetyState, SafetyAlertTemplates, SafetyEventStore; from arbiter.config.settings import SafetyConfig; from arbiter.execution.engine import RiskManager, ExecutionEngine; print('OK')"` | OK | PASS |
| Full Phase 3 test suite | `pytest arbiter/execution/ arbiter/safety/ arbiter/test_api_safety.py arbiter/test_main_shutdown.py -q` | 137 passed, 3 skipped | PASS |
| SAFE-02 gap closed — inner guard removed | `grep -nE 'if status == "filled":' arbiter/execution/engine.py` | NO OUTPUT (0 matches) | PASS |
| SAFE-02 recovering branch added | `grep -nE 'elif status == "recovering":' arbiter/execution/engine.py` | 1 match at line 847 | PASS |
| SAFE-02 release_trade in recovery path | `grep -nE 'self\.risk\.release_trade' arbiter/execution/engine.py` | 3 matches (line 1132 in recovery loop + lines 1497/1507 in update_manual_position) | PASS |
| Dashboard JS syntax | `node --check arbiter/web/dashboard.js` | exits 0 | PASS |
| API integration test | `pytest arbiter/test_api_integration.py -q` | 6 passed, 1 FAILED (test_api_and_dashboard_contracts — pre-existing UI-contract drift, see notes section) | FAIL (pre-existing) |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| SAFE-01 | 03-01 | Kill switch cancels open orders, halts execution, Telegram alert, dashboard + programmatic trigger | SATISFIED | SafetySupervisor + POST /api/kill-switch + safety_events table + WS kill_switch event all present and tested; 137 tests passing |
| SAFE-02 | 03-02 + 03-08 | Position limits enforced per-platform and per-market before order submission; in-flight submitted orders register exposure immediately | SATISFIED | check_trade enforces per-platform ceiling at pre-trade time; order_rejected incidents emitted; plan 03-08 closed the live-mode gap — record_trade fires on submitted + filled (not just filled); recovery cancel releases reservation; 4 new tests prove all invariants |
| SAFE-03 | 03-03 | One-leg recovery detects naked directional positions and executes automated or operator-assisted unwind | SATISFIED | _recover_one_leg_risk emits structured incident; handle_one_leg_exposure fires Telegram + dedicated WS event; dashboard hero alert renders |
| SAFE-04 | 03-04 | Per-platform API rate limiting prevents throttling/bans | SATISFIED | RateLimiter.acquire() in all outbound methods; 429 handling with apply_retry_after; _rate_limit_broadcast_loop; /api/system rate_limits key |
| SAFE-05 | 03-05 | Graceful shutdown cancels all open orders before process exit (SIGINT/SIGTERM) | SATISFIED | run_shutdown_sequence; prepare_shutdown publishes shutdown_state before trip_kill; KalshiAdapter cancel_all chunked (CHUNK_SIZE=20); PolymarketAdapter cancel_all via SDK; second-signal os._exit(1) |
| SAFE-06 | 03-06 | Market mapping resolution criteria comparison — operator must verify both platforms resolve identically | SATISFIED | MarketMappingRecord + MarketMapping both have resolution_criteria; ALTER TABLE migration; POST handler accepts and broadcasts; mapping_state WS event; dashboard state captures it |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| arbiter/test_api_integration.py | 82 | `assert "ARBITER LIVE" in ops_html` — literal no longer present in /ops dashboard.html after plan 03-07 UI consolidation (commit e4c3411) | Warning | 1 integration test failure on every run (pre-existing, introduced by 03-07); not a SAFE-* regression; see notes section for recommended fix |
| arbiter/web/* (working tree) | — | Uncommitted WIP modifications to dashboard.html/js/css and index.html | Info | Not part of any committed plan; ignored for this verification; carry forward to whatever phase these changes belong to |

---

### Human Verification Required

These 3 items were deferred from the initial verification and remain pending operator UAT. They cannot be automated with pytest and require a running server + browser session.

### 1. Kill-Switch ARM/RESET End-to-End

**Test:** Open index.html in browser, sign in as operator. Click "ARM KILL SWITCH" button, confirm modal, enter reason. Verify badge flips to ARMED (red), cooldown label appears. Wait 30 seconds, verify Reset button enables. Click Reset, confirm; verify badge flips to Disarmed (green).
**Expected:** Full ARM → cooldown → RESET cycle completes without errors. Backend /api/safety/status reflects state changes. Telegram alert fired (if token configured) or logs "Telegram disabled, would send..." otherwise.
**Why human:** Browser confirm/prompt dialogs + WebSocket round-trip + visual badge state not assertable in pytest.

### 2. Shutdown Banner Sequence

**Test:** Start `python -m arbiter.main --dry-run` (or `--api-only`), open dashboard in browser, send Ctrl+C. Observe dashboard before WebSocket closes.
**Expected:** `#shutdownBanner` becomes visible with "Server shutting down — cancelling open orders" text BEFORE the WS close event fires. After phase=complete the banner text updates to "Server shutdown complete" and the dashboard does NOT auto-reconnect.
**Why human:** Requires running server + live browser session; timing assertion across WS events not automatable.

### 3. Rate-Limit Pill Color Transitions

**Test:** Run system under high load (or mock throttle state) to trigger rate-limiter penalty state. Observe rate-limit pill colors on dashboard.
**Expected:** Pills transition from green (.ok) to amber (.warn) as remaining_penalty_seconds > 0; return to green when penalty clears.
**Why human:** Requires generating real throttle state; cannot assert color CSS class without a browser session.

---

### Cross-Phase Note: UI Contract Drift in test_api_integration.py

**This is a separate finding, not a SAFE-* gap.**

`arbiter/test_api_integration.py::test_api_and_dashboard_contracts` FAILs with:
```
AssertionError: assert 'ARBITER LIVE' in '...' (the /ops page HTML)
```

**Root cause:** The test at line 82 asserts the literal string `"ARBITER LIVE"` in the `/ops` page HTML. This assertion was written (commit `26ef529`) when the string existed in the dashboard. Plan 03-07 (commit `e4c3411`) restructured `dashboard.html` during UI consolidation; the resulting `/ops` page title is `"ARBITER Live Desk"` (not `"ARBITER LIVE"`). The 6 other tests in `test_api_integration.py` pass.

**Status:** Pre-existing regression as of plan 03-07. NOT caused by plan 03-08. NOT a SAFE-01..SAFE-06 failure. The safety layer goals are unaffected.

**Recommended fix (not part of this phase):** Update `arbiter/test_api_integration.py` line 82 to match the actual heading text:
```python
# Before (broken):
assert "ARBITER LIVE" in ops_html
# After (fixed — matches <title>ARBITER Live Desk</title>):
assert "ARBITER Live Desk" in ops_html
```
This is a 1-line change in the test file, no production code change required. Assign to whoever owns the next planned UI-related task or create a quick-fix plan in Phase 4.

---

_Verified: 2026-04-16T23:00:00Z_
_Verifier: Claude (gsd-verifier) — Re-verification after plan 03-08 gap closure_
