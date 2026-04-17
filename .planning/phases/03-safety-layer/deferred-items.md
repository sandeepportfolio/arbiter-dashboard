# Phase 03 Deferred Items

Out-of-scope discoveries logged during plan execution. These are NOT fixed in the current plan because they pre-date the current task or belong to a different owner.

## From Plan 03-02 Execution

### `test_complete_stub_satisfies_protocol` fails on base commit — RESOLVED in plan 03-05

- **File:** `arbiter/execution/adapters/test_protocol_conformance.py::test_complete_stub_satisfies_protocol`
- **Symptom (was):** `isinstance(_StubAdapter(), PlatformAdapter)` returned False even though the stub declared every Protocol method.
- **Actual root cause:** The `_StubAdapter` in the test file was missing `async def cancel_all` after plan 03-01 added `cancel_all` to the `PlatformAdapter` Protocol. (Not a Python 3.13 typing issue — the Protocol runtime check worked correctly and flagged a genuinely incomplete stub.)
- **Resolution (plan 03-05):** Added `async def cancel_all(self) -> list[str]: return []` to `_StubAdapter` and `_MissingAttributeAdapter`. Extended `test_protocol_lists_expected_methods` to include `cancel_all` in the expected-methods set. All 4 protocol conformance tests now pass.
- **Commit:** See plan 03-05 Task 1 commit.

## From Plan 03-07 Execution

### `buildMetricCards` labels drifted from test expectations

- **File:** `arbiter/web/dashboard-view-model.test.js` → `builds the compact financial strip for the overview layout`
- **Symptom:** Pre-existing test expects `cards[2].label === "Validator progress"` and `cards[3].label === "Trade throughput"` but the current `buildMetricCards` implementation returns `"Validator state"` and `"Execution flow"`.
- **Scope:** Pre-existing on base commit 62220e2 (before 03-07 started). Not caused by plan 03-07 changes. Label drift belongs to earlier dashboard polish work.
- **Decision:** Left unmodified per scope boundary in executor rules. Either the test should be updated or `buildMetricCards` should be reverted to produce `"Validator progress"` / `"Trade throughput"`. Whichever is canonical, it is a separate fix.
- **Verification:** `git stash && npx vitest run arbiter/web/dashboard-view-model.test.js` at 62220e2 reproduces the same failure.

## From Plan 03-04 Execution

### `test_api_and_dashboard_contracts` flakes in sandboxed environments

- **File:** `arbiter/test_api_integration.py::test_api_and_dashboard_contracts`
- **Symptom:** `AssertionError: Server on port <N> did not become ready` after the 15s subprocess-bootstrap timeout.
- **Root cause:** The test spawns `python -m arbiter.main --api-only --port <N>` as a subprocess and polls `/api/health`. Windows/containerized environments where socket binding is restricted or Python startup is slow (>15s for heavy imports) timeout before the aiohttp server is accepting connections. Verified pre-existing by `git stash && pytest ... -x` at the 03-04 base commit.
- **Not related to plan 03-04:** The two new SAFE-04 tests (`test_rate_limit_ws_event_shape`, `test_system_endpoint_includes_rate_limits`) use in-process `aiohttp.test_utils.TestServer` + `TestClient` — no subprocess — and both pass reliably.
- **Scope:** Environmental / CI test-harness robustness; consider raising the subprocess timeout or replacing the subprocess bootstrap with in-process `TestServer`.
