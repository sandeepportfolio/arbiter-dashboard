---
phase: 3
slug: safety-layer
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-16
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.x (Python) + vitest 4.x (TS CLI) |
| **Config file** | `conftest.py`, `pytest.ini` (if present); `vitest.config.ts` |
| **Quick run command** | `pytest arbiter/tests/test_safety_supervisor.py -x --tb=short` |
| **Full suite command** | `pytest arbiter/tests/ -x --tb=short && npx vitest run` |
| **Estimated runtime** | ~45 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest arbiter/tests/test_safety_supervisor.py -x --tb=short`
- **After every plan wave:** Run `pytest arbiter/tests/ -x --tb=short`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 45 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 3-01-XX | 01 | 1 | SAFE-01 | T-3-01 | Kill-switch trips in ≤5s and blocks new orders until reset | unit | `pytest arbiter/tests/test_safety_supervisor.py -k kill_switch` | ❌ W0 | ⬜ pending |
| 3-02-XX | 02 | 1 | SAFE-02 | T-3-02 | Per-platform exposure check rejects orders exceeding limit | unit | `pytest arbiter/tests/test_risk_manager.py -k per_platform` | ❌ W0 | ⬜ pending |
| 3-03-XX | 03 | 2 | SAFE-03 | T-3-03 | One-leg exposure emits structured event + Telegram within one scan cycle | unit | `pytest arbiter/tests/test_one_leg_detection.py` | ❌ W0 | ⬜ pending |
| 3-04-XX | 04 | 2 | SAFE-04 | T-3-04 | Platform adapter throttles before hitting venue limits | unit | `pytest arbiter/tests/test_rate_limiter.py` | ❌ W0 | ⬜ pending |
| 3-05-XX | 05 | 3 | SAFE-05 | T-3-05 | SIGINT cancels orders BEFORE task cancellation | integration | `pytest arbiter/tests/test_graceful_shutdown.py` | ❌ W0 | ⬜ pending |
| 3-06-XX | 06 | 1 | SAFE-06 | T-3-06 | Resolution criteria schema validates mapping entries | unit | `pytest arbiter/tests/test_market_map.py -k resolution_criteria` | ❌ W0 | ⬜ pending |
| 3-07-XX | 07 | 4 | SAFE-01..06 (UI) | — | Dashboard renders kill-switch + rate-limit pills + one-leg alert + shutdown banner | unit | `npx vitest run arbiter/web/__tests__/safety-view-model.test.js` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `arbiter/tests/test_safety_supervisor.py` — stubs for SAFE-01 (kill switch)
- [ ] `arbiter/tests/test_risk_manager.py` — stubs for SAFE-02 (per-platform limits)
- [ ] `arbiter/tests/test_one_leg_detection.py` — stubs for SAFE-03
- [ ] `arbiter/tests/test_rate_limiter.py` — stubs for SAFE-04
- [ ] `arbiter/tests/test_graceful_shutdown.py` — stubs for SAFE-05
- [ ] `arbiter/tests/test_market_map.py` — extend for SAFE-06 resolution criteria
- [ ] `arbiter/web/__tests__/safety-view-model.test.js` — UI view-model stubs
- [ ] `arbiter/tests/conftest.py` — shared fixtures (fake adapter, mock PriceStore, fake Telegram)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real SIGINT on running process cancels open orders at live venue | SAFE-05 | Requires live platform orders; automated version uses fake adapter | Start `python -m arbiter.main --dry-run`, place open test order, send SIGINT (Ctrl+C), verify venue shows zero open orders within 5s |
| Telegram alert on one-leg exposure actually delivers | SAFE-03 | Requires real Bot API token | Trigger simulated second-leg failure in integration session, verify Telegram bot sends message to configured chat |
| Kill-switch button in dashboard arms/disarms correctly end-to-end | SAFE-01 (UI) | WebSocket + HTTP auth round trip | Open `/index.html`, click Kill Switch, confirm modal, verify backend state via `/api/safety/state` and all adapters return order rejections |
| Rate-limit pills change color under sustained call load | SAFE-04 (UI) | Requires generating real throttle state | Script generating high-frequency collector polls, observe dashboard pill transitions (green → amber → red) |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 45s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
