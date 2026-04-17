# Phase 03 Deferred Items

Out-of-scope discoveries logged during plan execution. These are NOT fixed in the current plan because they pre-date the current task or belong to a different owner.

## From Plan 03-02 Execution

### `test_complete_stub_satisfies_protocol` fails on base commit

- **File:** `arbiter/execution/adapters/test_protocol_conformance.py::test_complete_stub_satisfies_protocol`
- **Symptom:** `isinstance(_StubAdapter(), PlatformAdapter)` returns False even though the stub declares every Protocol method.
- **Root cause (suspected):** After plan 03-01 added `cancel_all` to `PlatformAdapter`, the test's `_StubAdapter` was updated but the `@runtime_checkable` Protocol metadata disagreement with Python 3.13 typing semantics causes `isinstance` to fail. This is a Protocol-conformance plumbing issue, not a plan-03-02 regression.
- **Verification it's pre-existing:** Reproduced by running `pytest arbiter/execution/adapters/test_protocol_conformance.py::test_complete_stub_satisfies_protocol` after `git stash` of the plan 03-02 worktree changes.
- **Scope:** Belongs to plan 03-05 (cancel_all full implementation) or a targeted protocol-conformance fix plan.

## From Plan 03-04 Execution

### `test_api_and_dashboard_contracts` flakes in sandboxed environments

- **File:** `arbiter/test_api_integration.py::test_api_and_dashboard_contracts`
- **Symptom:** `AssertionError: Server on port <N> did not become ready` after the 15s subprocess-bootstrap timeout.
- **Root cause:** The test spawns `python -m arbiter.main --api-only --port <N>` as a subprocess and polls `/api/health`. Windows/containerized environments where socket binding is restricted or Python startup is slow (>15s for heavy imports) timeout before the aiohttp server is accepting connections. Verified pre-existing by `git stash && pytest ... -x` at the 03-04 base commit.
- **Not related to plan 03-04:** The two new SAFE-04 tests (`test_rate_limit_ws_event_shape`, `test_system_endpoint_includes_rate_limits`) use in-process `aiohttp.test_utils.TestServer` + `TestClient` — no subprocess — and both pass reliably.
- **Scope:** Environmental / CI test-harness robustness; consider raising the subprocess timeout or replacing the subprocess bootstrap with in-process `TestServer`.
