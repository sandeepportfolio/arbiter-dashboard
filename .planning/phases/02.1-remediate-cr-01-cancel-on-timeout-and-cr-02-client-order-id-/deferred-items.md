# Phase 02.1 — Deferred Items

Out-of-scope discoveries during plan 02.1-01 execution. Per
GSD scope-boundary rules, these are tracked here and NOT
fixed in this plan.

---

## Pre-existing flaky test

**File:** `arbiter/test_api_integration.py::test_api_and_dashboard_contracts`

**Symptom:** `AssertionError: Server on port {N} did not become ready` —
the integration test launches a subprocess server, polls
`/api/health` for 15 seconds, and times out. Reproducible on
clean working tree at commit `1634928` (verified by
`git stash && pytest arbiter/test_api_integration.py`).

**Root cause:** Unrelated to CR-01/CR-02. Likely a Windows-specific
subprocess startup latency issue or port-binding race.

**Disposition:** Not in scope for plan 02.1-01 (`files_modified` does
not include `arbiter/test_api_integration.py` or `arbiter/api.py`).
Should be triaged in a separate small fix or in Phase 4.

**Status:** Acknowledged, not fixed.
