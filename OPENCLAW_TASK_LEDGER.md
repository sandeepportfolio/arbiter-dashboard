# OPENCLAW_TASK_LEDGER

Updated: 2026-04-21 02:43 PDT

| ID | Priority | Status | Task | Success criteria |
|---|---|---|---|---|
| T01 | P0 | completed | Restore a reproducible Python verification path | Clean/bootstrap flow documented and full `pytest -q` runs on supported Python, or failures are proven code-level and captured precisely |
| T02 | P0 | completed | Reconcile stale repo instructions and runbooks | `GOLIVE.md`, `STATUS.md`, `HANDOFF.md`, and any active repo-native instructions no longer contradict the Polymarket US pivot or current phase state |
| T03 | P1 | in_progress | Audit code TODOs and incomplete operational paths | Every source TODO is either fixed, downgraded to non-critical, or documented in risk register with owner/next action |
| T04 | P1 | completed | Verify build/test surface end to end | TypeScript build/typecheck, JS tests, Python tests, and any repo quick-verify scripts are run or blocked with explicit external causes |
| T05 | P1 | completed | Update durable handoff for the next cycle | `OPENCLAW_STATUS.md`, `OPENCLAW_HANDOVER.md`, and `OPENCLAW_RISK_REGISTER.md` reflect current truth and exact resume commands |

## Current evidence
- `make verify-full` is green on the repo `.venv` (`478 passed, 87 skipped` in Python, TypeScript clean, Vitest `40` tests green, API smoke + UI smoke + static smoke green).
- Repo instruction drift has been reduced: `GOLIVE.md`, `STATUS.md`, `HANDOFF.md`, and the OpenClaw handoff files now reflect the operator settings surface and current verification state.
- Remaining focus has shifted from bootstrap to TODO/risk audit and final non-secret go-live prep.
