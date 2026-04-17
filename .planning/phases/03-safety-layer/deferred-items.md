# Phase 03 Deferred Items

Out-of-scope discoveries logged during plan execution. These are NOT fixed in the current plan because they pre-date the current task or belong to a different owner.

## From Plan 03-02 Execution

### `test_complete_stub_satisfies_protocol` fails on base commit

- **File:** `arbiter/execution/adapters/test_protocol_conformance.py::test_complete_stub_satisfies_protocol`
- **Symptom:** `isinstance(_StubAdapter(), PlatformAdapter)` returns False even though the stub declares every Protocol method.
- **Root cause (suspected):** After plan 03-01 added `cancel_all` to `PlatformAdapter`, the test's `_StubAdapter` was updated but the `@runtime_checkable` Protocol metadata disagreement with Python 3.13 typing semantics causes `isinstance` to fail. This is a Protocol-conformance plumbing issue, not a plan-03-02 regression.
- **Verification it's pre-existing:** Reproduced by running `pytest arbiter/execution/adapters/test_protocol_conformance.py::test_complete_stub_satisfies_protocol` after `git stash` of the plan 03-02 worktree changes.
- **Scope:** Belongs to plan 03-05 (cancel_all full implementation) or a targeted protocol-conformance fix plan.
