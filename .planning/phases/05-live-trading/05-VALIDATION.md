---
phase: 5
slug: live-trading
status: pending_live_fire        # Plan 05-01 + 05-02 code-side complete; awaiting operator live-fire
phase_gate_status: PENDING       # flips to PASS only after first live trade reconciles ±$0.01 (or auto-aborts correctly) AND operator attests
nyquist_compliant: true
wave_0_complete: true            # Plan 05-01 landed 2026-04-20; Plan 05-02 Task 3a landed 2026-04-20
created: 2026-04-20
tolerance_usd: 0.01              # D-17 reused from Phase 4 — same tolerance bar for live
total_scenarios_expected: 1      # single first-live-trade scenario
total_scenarios_observed: 0
scenarios_passed: 0
scenarios_failed: 0
scenarios_missing: 1
---

# Phase 5 — Validation Strategy (D-19-analog Live Trading Gate)

> Per-phase validation contract. Phase 5 is a thin go-live layer wrapping phases 1-4.
> The gate flips to PASS only after the first real cross-platform arbitrage trade either
> (a) reconciles within ±$0.01 on both fee and PnL, OR (b) triggers the auto-abort path
> correctly on a reconcile breach — AND the operator manually attests the evidence.

**Phase Goal:** The first real cross-platform arbitrage trade executes successfully with small capital under operator supervision, proving the system works end-to-end with real money.
**Hard Gate Rule:** First live trade must reconcile within ±$0.01 fee AND ±$0.01 PnL on BOTH legs, OR demonstrate the auto-abort path (reconcile breach -> trip_kill fires within <5s, all orders cancelled, Telegram alert received). Either outcome PASSES TEST-05; a reconcile breach without auto-abort FAILS.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.0+ + pytest-asyncio (root conftest async dispatch) |
| **Config file** | `arbiter/live/conftest.py` (Plan 05-01) |
| **Quick run command** | `pytest arbiter/live/ -v` (non-live unit tests, ~5s) |
| **Full suite command** | `pytest -m live --live arbiter/live/ -v -s` (single live-fire scenario; requires operator) |
| **Preflight command** | `python -m arbiter.live.preflight` (15-item checklist runner) |
| **Estimated runtime** | <10s for unit tests; 5-15 minutes for live-fire (includes 60s settlement wait + operator-paced pre-trade pause) |

---

## Sampling Rate

- **After every task commit (Plan 05-01):** `pytest arbiter/live/test_reconcile.py arbiter/live/test_preflight.py arbiter/execution/adapters/test_phase5_hardlock.py -v` (~5s)
- **After every task commit (Plan 05-02):** `pytest arbiter/live/test_auto_abort.py arbiter/live/test_live_fire_helpers.py -v` (~5s; includes B-2 + B-3 helper unit tests)
- **After Plan 05-01 wave merge:** `pytest arbiter/live/ arbiter/sandbox/ -v` (~15s; includes regression check against Phase 4)
- **Phase gate (Plan 05-02):** `pytest -m live --live arbiter/live/test_first_live_trade.py -v -s` + operator manual attestation in this file
- **Before `/gsd-verify-work`:** Full unit suite green (Plan 05-02 can commit scaffolding before live-fire run; verify-work is gated by operator attestation)
- **Max feedback latency (unit tests):** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 5-01-01 | 05-01 | 1 | TEST-05 | T-5-01-01, T-5-01-02, T-5-01-08 | PHASE5_MAX_ORDER_USD hard-lock enforced at PolymarketAdapter.place_fok, KalshiAdapter.place_fok (new gap closure), KalshiAdapter.place_resting_limit; both PHASE4 and PHASE5 run in sequence | unit | `pytest arbiter/execution/adapters/test_phase5_hardlock.py -v` | ✅ created 2026-04-20 (Plan 05-01) | ✅ green (18/18 tests) |
| 5-01-02 | 05-01 | 1 | TEST-05 | T-5-01-03, T-5-01-04, T-5-01-10 | arbiter/live/ harness mirrors arbiter/sandbox/ with production guard-rails (KALSHI_BASE_URL NOT demo, DATABASE_URL arbiter_live, PHASE5_MAX_ORDER_USD ≤ 10); preflight runner implements 15-item checklist; .env.production.template includes MAX_POSITION_USD=10 (B-5) + PHASE5_BOOTSTRAP_TRADES=1 (B-1 Q6); preflight #9 polarity fixed (W-2) | unit + integration | `pytest arbiter/live/ -v && python -m arbiter.live.preflight` | ✅ created 2026-04-20 (Plan 05-01) | ✅ green (41/41 tests) |
| 5-01-03 | 05-01 | 1 | TEST-05 | T-5-01-11, T-5-01-12 | readiness PHASE5_BOOTSTRAP_TRADES override (B-1 Q6 chicken-and-egg) + SafetySupervisor.is_armed/armed_by public properties (W-5); unset env var = existing behavior unchanged; set to int in [1,5] bypasses collecting_evidence block for first N live trades | unit | `pytest arbiter/tests/test_readiness_bootstrap.py -v` | ✅ created 2026-04-20 (Plan 05-01) | ✅ green (9/9 tests) |
| 5-02-01 | 05-02 | 2 | TEST-05 | T-5-01-05 | Operator pre-flight checkpoint pass (15-item preflight clean + Telegram dry test + mapping confirmed + arbiter_live DB ready) | manual | (checkpoint — no automated command) | N/A | ⬜ pending (awaiting operator) |
| 5-02-02 | 05-02 | 2 | TEST-05 | T-5-02-01, T-5-02-02 | wire_auto_abort_on_reconcile calls supervisor.trip_kill on reconcile breach OR reconcile exception (fail-closed); does not double-fire (supervisor handles idempotency) | unit | `pytest arbiter/live/test_auto_abort.py -v` | ✅ created 2026-04-20 (Plan 05-02 Task 2) | ✅ green (5/5 tests) |
| 5-02-03a | 05-02 | 2 | TEST-05 | T-5-02-09, T-5-02-10 | live_fire_helpers.py (B-2 fee fetchers + B-3 opportunity builder) as FIRST-CLASS deliverables — no stub bodies; test_first_live_trade.py scaffold with W-3 (pre_trade_requote.json), W-5 (supervisor.is_armed public API), W-6 (60s abort window) | unit + scaffolding | `pytest arbiter/live/test_live_fire_helpers.py -v && ! grep -q NotImplementedError arbiter/live/live_fire_helpers.py && ! grep -q NotImplementedError arbiter/live/test_first_live_trade.py && ! grep -q "_state\.armed" arbiter/live/test_first_live_trade.py` | ✅ created 2026-04-20 (Plan 05-02 Task 3a) | ✅ green (11/11 helper tests; grep invariants clean) |
| 5-02-03b | 05-02 | 2 | TEST-05 | T-5-02-03, T-5-02-04, T-5-02-05, T-5-02-06, T-5-02-07, T-5-02-08 | First real cross-platform arbitrage executes: preflight PASS -> opportunity detected -> 60s operator-abort window (W-6) -> engine.execute -> both legs terminal -> 60s Polygon settlement wait -> reconcile -> auto-abort-on-breach OR clean-PASS -> evidence captured (incl. pre_trade_requote.json per W-3) | integration + live + manual | `pytest -m live --live arbiter/live/test_first_live_trade.py -v -s` + operator attestation in this file | ✅ scaffold created 2026-04-20 (Plan 05-02 Task 3a); live-fire ⬜ pending operator | ⬜ pending (awaiting operator) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

**Plan 05-01 deliverables (Wave 0 — completed 2026-04-20):**
- [x] `arbiter/execution/adapters/test_phase5_hardlock.py` — 18 tests (5 × 3 call sites + 3 combination tests)
- [x] `arbiter/safety/supervisor.py` — W-5: public `is_armed: bool` + `armed_by: Optional[str]` `@property` accessors
- [x] `arbiter/readiness.py` — B-1 Q6: `PHASE5_BOOTSTRAP_TRADES` env-var override in `_check_profitability`
- [x] `arbiter/tests/test_readiness_bootstrap.py` — 9 tests (unset / set+zero / set+count / invalid range / non-numeric / validated+bootstrap / blocked+bootstrap)
- [x] `arbiter/live/__init__.py`
- [x] `arbiter/live/conftest.py` — @pytest.mark.live opt-in + pytest_plugins for fixtures subpackage; no re-registration of --live flag
- [x] `arbiter/live/fixtures/__init__.py`
- [x] `arbiter/live/fixtures/production_db.py` — asserts DATABASE_URL contains 'arbiter_live' (not sandbox/dev)
- [x] `arbiter/live/fixtures/kalshi_production.py` — asserts KALSHI_BASE_URL does not contain 'demo'; key path does not contain 'demo'; key file exists on disk
- [x] `arbiter/live/fixtures/polymarket_production.py` — asserts PHASE5_MAX_ORDER_USD set and ≤ 10; POLY_PRIVATE_KEY + POLY_FUNDER set
- [x] `arbiter/live/evidence.py` — re-exports dump_execution_tables + write_balances from arbiter.sandbox.evidence
- [x] `arbiter/live/reconcile.py` — re-exports from arbiter.sandbox.reconcile + new `reconcile_post_trade(execution, adapters, tolerance, fee_fetcher)`
- [x] `arbiter/live/preflight.py` — PreflightReport + 15 _check_* functions + run_preflight + CLI main
- [x] `arbiter/live/test_reconcile.py` — 4 non-live unit tests for reconcile_post_trade
- [x] `arbiter/live/test_preflight.py` — 15+3 non-live unit tests for each _check_* function
- [x] `arbiter/live/README.md` — operator runbook (Prerequisites, Setup, Go-Live, Abort, Rollback, Troubleshooting, Success Criteria; ≥120 lines)
- [x] `.env.production.template` — DRY_RUN=false, arbiter_live DB, production Kalshi URL, PHASE5_MAX_ORDER_USD=10
- [x] `.gitignore` edit — `.env.production` + `evidence/05/`

**Plan 05-02 Wave 0 deliverables (code-side — completed 2026-04-20 Task 3a):**
- [x] `arbiter/live/auto_abort.py` — `wire_auto_abort_on_reconcile` (fail-closed)
- [x] `arbiter/live/test_auto_abort.py` — 5 unit tests
- [x] `arbiter/live/live_fire_helpers.py` — B-2 + B-3: real build_opportunity_from_quotes + fetch_kalshi_platform_fee + fetch_polymarket_platform_fee + write_pre_trade_requote + module constants PRE_EXECUTION_OPERATOR_ABORT_SECONDS=60.0 (W-6) + POLYGON_SETTLEMENT_WAIT_SECONDS=60.0
- [x] `arbiter/live/test_live_fire_helpers.py` — 11 unit tests for helpers (AsyncMock/MagicMock; no network I/O)
- [x] `arbiter/live/test_first_live_trade.py` — single @pytest.mark.live scenario using helpers (grep invariants clean: 0 NotImplementedError, 0 `_state.armed` access), 60s operator-abort window (W-6), writes pre_trade_requote.json (W-3). **Live-fire execution still PENDING (operator-gated).**
- [x] `.planning/phases/05-live-trading/05-VALIDATION.md` — THIS FILE, populated with D-19-analog gate. `phase_gate_status` remains PENDING until operator attests after successful live-fire.

**Plan 05-02 Task 3b — DEFERRED (operator-gated):**
- [ ] Operator runs `pytest -m live --live arbiter/live/test_first_live_trade.py -v -s` against production Kalshi + Polymarket with a tradable arb available
- [ ] Operator archives manual evidence (kalshi_ui_screenshot.png, polymarket_ui_screenshot.png, operator_notes.md) under `evidence/05/first_live_trade_<ts>/manual/`
- [ ] Operator fills the Operator Attestation block below
- [ ] Operator flips `phase_gate_status: PENDING` → `phase_gate_status: PASS` in this file's frontmatter

Wave 0 COMPLETE (both plans landed). Live-fire run awaits operator provisioning (funded accounts + arbiter_live DB + preflight clean).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Operator archives Kalshi platform UI screenshot showing fill | TEST-05 | UI rendering is human-visual; no programmatic screenshot API | Log into kalshi.com -> Portfolio -> Trades; screenshot the row matching the arb's order_id; save to `evidence/05/first_live_trade_<ts>/manual/kalshi_ui_screenshot.png` |
| Operator archives Polymarket platform UI screenshot (or polygonscan tx page) | TEST-05 | On-chain confirmation is human-visual | Log into polymarket.com OR visit polygonscan.com with POLY_FUNDER address; screenshot the trade OR the USDC transfer; save to `evidence/05/first_live_trade_<ts>/manual/polymarket_ui_screenshot.png` |
| Operator writes `operator_notes.md` capturing qualitative observations | TEST-05 | Qualitative evidence (edge size at scan vs fill, slippage observed, any anomalies) | Write-up stored at `evidence/05/first_live_trade_<ts>/manual/operator_notes.md` |
| Operator attests D-19 flip (phase_gate_status: PASS) | TEST-05 | Reconcile-within-tolerance alone is necessary-not-sufficient — operator must confirm they observed the entire trade lifecycle and did not intervene to paper-over a bug | Edit this file's frontmatter: `phase_gate_status: PASS`; fill the Operator Attestation block below; commit |
| Telegram kill-switch ARM notification received | SAFE-01 (re-verified under production) | Telegram delivery is out-of-band — requires human to confirm device received the push | After any ARM event during live-fire (including auto-abort on reconcile breach), operator confirms Telegram chat received the `[ARBITER] Kill switch armed` message; note in operator_notes.md |
| Polymarket April 2026 migration compatibility verified | TEST-05 (Pitfall 7) | Requires reading external docs + checking wallet state | Before Plan 05-02 Task 1 checkpoint, operator verifies wallet holds the correct collateral (USDC.e vs Polymarket USD) + py-clob-client version supports it. Document evidence in operator_notes.md |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify OR manual-only with explicit rationale
- [ ] Sampling continuity: all 4 unit-test task rows have automated commands (< 5s each)
- [ ] Wave 0 covers all MISSING references (flips to [x] after Plan 05-01 lands)
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s for unit tests; ~15 min for live-fire
- [x] `nyquist_compliant: true` set in frontmatter
- [ ] **phase_gate_status flip attested by operator** — flip from PENDING to PASS here after successful live-fire run + archived manual evidence

**Approval:** pending (awaiting Plan 05-01 + 05-02 code completion followed by live-fire execution + operator attestation)

---

## Dependency Gate (from ROADMAP)

Phase 5 cannot execute (live-fire run) until:
- [ ] `.planning/phases/04-sandbox-validation/04-VALIDATION.md` frontmatter shows `phase_gate_status: PASS`

As of 2026-04-20, Phase 4 D-19 status is PENDING (0/9 scenarios observed; operator has not yet provisioned `.env.sandbox`). Phase 5 PLANNING can proceed (code scaffolding in Plan 05-01 is safe — unit tests only; no live-fire until Phase 4 D-19 flips).

The live-fire step in Plan 05-02 MUST wait until Phase 4 D-19 flips to PASS. The Plan 05-02 Task 1 operator checkpoint includes this as the very first verification item.

---

## Operator Attestation (leave blank until live-fire passes)

```
Date of first live trade:      ____________________
Arb ID:                        ____________________
Evidence directory:            evidence/05/first_live_trade_____________/
Target canonical_id:           ____________________
Market resolution status:      identical (must be)

Yes leg:
  Platform:                    ____
  Status:                      ____________________   (FILLED / CANCELLED / FAILED)
  Fill price:                  $______
  Fill qty:                    ______
  Notional:                    $______ (must be <= $10 per PHASE5_MAX_ORDER_USD)

No leg:
  Platform:                    ____
  Status:                      ____________________
  Fill price:                  $______
  Fill qty:                    ______
  Notional:                    $______ (must be <= $10 per PHASE5_MAX_ORDER_USD)

Expected edge (pre-trade):     ______¢
Realized P&L (post-fee):       $______
Fee reconciliation:            [ ] Kalshi within ±$0.01    [ ] Polymarket within ±$0.01
PnL reconciliation:            [ ] Kalshi within ±$0.01    [ ] Polymarket within ±$0.01

Outcome:
  [ ] CLEAN-PASS: both legs reconciled within ±$0.01, positive P&L observed
  [ ] AUTO-ABORT-FIRED: reconcile breach caught by wire_auto_abort_on_reconcile;
      kill-switch armed within <5s; Telegram alert received; all open orders cancelled
  [ ] MIXED: one leg reconciled, other did not; auto-abort fired correctly; operator reviewed

Operator name:                 ____________________
Operator signature/date:       ____________________
Operator comments:             ____________________
                               ____________________
                               ____________________

After completing this block, flip phase_gate_status: PENDING -> PASS at the top of this file
and commit with message: `docs(05-02): attest TEST-05 PASS after first live trade`
```

---

## Notes

- Real-tagged scenarios expected: 1 (first live trade)
- Unit tests expected: 5-01-01 (18 hardlock tests), 5-01-02 (live harness ~20 tests), 5-01-03 (9 bootstrap + supervisor property tests), 5-02-02 (5 auto_abort tests), 5-02-03a (10 live_fire_helpers tests) = ~62 unit tests total
- Tolerance: ±$0.01 (D-17, inherited from Phase 4)
- Hard-gate rule: D-19-analog — any real breach without correct auto-abort path fails TEST-05
- To refresh this file after a live-fire run: `pytest -m live --live arbiter/live/test_first_live_trade.py -v -s` (evidence directory created) followed by manual operator attestation block fill + phase_gate_status flip
