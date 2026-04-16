---
phase: 2
slug: execution-operational-hardening
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-16
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.x (existing, per requirements.txt) |
| **Config file** | conftest.py (existing at repo root) |
| **Quick run command** | `pytest arbiter/tests -q -x` |
| **Full suite command** | `pytest arbiter/tests` |
| **Estimated runtime** | ~30 seconds (quick), ~90 seconds (full, excluding integration) |

---

## Sampling Rate

- **After every task commit:** Run `pytest arbiter/tests -q -x`
- **After every plan wave:** Run full suite
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 90 seconds

---

## Per-Task Verification Map

*To be filled in by planner — one row per task. Source-of-truth is each PLAN.md's `<automated>` blocks.*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 2-XX-XX | XX   | N    | REQ-XX      | T-2-XX / — | {behavior}      | unit      | `{command}`       | ❌ W0       | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Per RESEARCH.md §Validation Architecture (Wave 0 gaps):

- [ ] `arbiter/tests/__init__.py` — ensure tests package initialized
- [ ] `arbiter/tests/conftest.py` — shared asyncpg/Redis/test-DB fixtures
- [ ] `arbiter/tests/test_execution_store.py` — stubs for EXEC-02 (persistence)
- [ ] `arbiter/tests/test_adapters_fok.py` — stubs for EXEC-01 (FOK)
- [ ] `arbiter/tests/test_retry_policy.py` — stubs for EXEC-04 (retry/backoff)
- [ ] `arbiter/tests/test_structured_logging.py` — stubs for OPS-01 (JSON logs)
- [ ] `arbiter/tests/test_restart_recovery.py` — stubs for OPS-02 (restart survives)
- [ ] `arbiter/tests/test_adapter_contract.py` — Protocol conformance per adapter (EXEC-05)
- [ ] `arbiter/sql/migrations/` — migration directory for new execution tables
- [ ] `requirements.txt` — add `structlog`, `tenacity`, `sentry-sdk[aiohttp]` deps
- [ ] Install verified: `python -c "import structlog, tenacity, sentry_sdk"`

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| FOK rejects entire order on insufficient depth (Kalshi sandbox) | EXEC-01 | Needs live sandbox API credentials; cannot run in CI | See Phase 4 sandbox task — place FOK for qty > book depth, confirm status=cancelled with 0 fills |
| FOK rejects entire order on insufficient depth (Polymarket) | EXEC-01 | Needs live API + USDC test wallet | See Phase 4 sandbox task — same as above |
| Process restart recovers open orders from DB | OPS-02 | Requires kill -9 mid-execution; not automatable in CI without fragile timing | Start service, place order, kill -9, restart, confirm open orders appear in /api/orders |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (structlog/tenacity/sentry install, migrations dir, test stubs)
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter (planner sets after populating verification map)

**Approval:** pending
