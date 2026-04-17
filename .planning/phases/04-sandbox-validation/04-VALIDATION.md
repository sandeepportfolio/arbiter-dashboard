---
phase: 4
slug: sandbox-validation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-17
---

# Phase 4 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.x (project default via conftest.py with custom asyncio dispatcher) |
| **Config file** | `conftest.py` at project root (already configured) |
| **Quick run command** | `pytest -m 'not live' arbiter/sandbox/` |
| **Full suite command** | `pytest arbiter/sandbox/` (unit only) + `pytest -m live arbiter/sandbox/` (opt-in live) |
| **Estimated runtime** | ~5s for unit, ~60–180s for live (opt-in, manual trigger only) |

---

## Sampling Rate

- **After every task commit:** Run `pytest arbiter/sandbox/ -m 'not live'` (unit tests for the harness itself)
- **After every plan wave:** Run `pytest arbiter/sandbox/ -m 'not live'` full unit sweep
- **Before `/gsd-verify-work`:** Operator opts into `pytest -m live` at least once per scenario and archives evidence under `evidence/04/<scenario>/`
- **Max feedback latency:** 10s for unit path; live path is manual and latency-unbounded

---

## Per-Task Verification Map

*Populated by planner during task decomposition. Each task in each PLAN.md MUST map here.*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD     | TBD  | TBD  | TEST-01..04 | —          | FOK invariant holds; no partial fills; cancel-on-timeout works | integration | `pytest -m live arbiter/sandbox/test_<scenario>.py` | ❌ W0 | ⬜ pending |

---

## Wave 0 Requirements

- [ ] `arbiter/sandbox/__init__.py` — package marker
- [ ] `arbiter/sandbox/conftest.py` — fixtures (sandbox DB session, demo Kalshi client, Polymarket test wallet, pre/post balance snapshot, evidence directory)
- [ ] `arbiter/sandbox/markers.py` or `conftest.py` addition — register `@pytest.mark.live` marker with `pytest_collection_modifyitems` opt-in logic (skip unless `--live` flag or `-m live`)
- [ ] `.env.sandbox.template` — sandbox credential template (`KALSHI_BASE_URL`, `KALSHI_DEMO_API_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY_PATH`, `POLY_PRIVATE_KEY` [test wallet], `DATABASE_URL` [arbiter_sandbox], `PHASE4_MAX_ORDER_USD=5`)
- [ ] `docker-compose.yml` — `POSTGRES_MULTIPLE_DATABASES=arbiter_dev,arbiter_sandbox` env var + init script in `docker/postgres/init-multiple-dbs.sh`
- [ ] `arbiter/sandbox/README.md` — operator-facing bootstrap instructions (credential setup, DB init, how to run each scenario)
- [ ] `arbiter/execution/adapters/polymarket.py` — `PHASE4_MAX_ORDER_USD` hard-lock check in `place_fok` (blocks `notional > env cap` before any HTTP call)
- [ ] `arbiter/config/settings.py:365,376` — source `base_url`/`clob_url` defaults from env vars `KALSHI_BASE_URL` and `POLY_CLOB_URL`

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Kalshi demo account funding | TEST-01 bootstrap | Demo funding is a manual test-card flow on Kalshi's demo site; no API | Follow `arbiter/sandbox/README.md` §Demo Account Setup; confirm balance via `BalanceMonitor.fetch_balance()` returns >$0 |
| Polymarket USDC bridge to test wallet | TEST-02 bootstrap | Bridging real USDC to a Polygon wallet is an off-API action | Bridge ~$10 USDC to `POLY_PRIVATE_KEY` wallet via official Polymarket deposit flow; confirm via on-chain explorer or `BalanceMonitor` |
| SIGINT graceful-shutdown cancel-all (SAFE-05) | Phase 3 SAFE-05 live | Requires real process + real signal — cannot run in-process within pytest | Subprocess test: launch `python -m arbiter.main` against sandbox, place Kalshi demo order, send SIGINT, assert cancel-all logged and resting order gone via `list_open_orders_by_client_id` |
| Dashboard UI kill-switch ARM/RESET (3-HUMAN-UAT Test 1) | Phase 3 SAFE-01 UAT | Browser + operator confirm dialog; no headless replay | Operator runs through Phase 3 `03-HUMAN-UAT.md` Test 1 against sandbox server, captures screenshot evidence |
| Shutdown banner visibility (3-HUMAN-UAT Test 2) | Phase 3 SAFE-05 UAT | Requires browser observation before WS close | Same as SAFE-05 subprocess test — additionally observe `#shutdownBanner` in browser during cancel-all window |
| Rate-limit pill color transition (3-HUMAN-UAT Test 3) | Phase 3 SAFE-04 UAT | Requires browser observation during burst | Trigger rate-limit burst scenario with dashboard open; observe green→amber transition in rate-limit pills |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (sandbox package, conftest, docker-compose multi-DB, .env.sandbox.template)
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s for unit path (live path is manual opt-in — unbounded)
- [ ] `nyquist_compliant: true` set in frontmatter after planner maps every task to this file

**Approval:** pending
