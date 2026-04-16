---
phase: 01-api-integration-fixes
plan: 03
subsystem: workflow
tags: [cleanup, dead-code-removal, predictit]
dependency_graph:
  requires: [01-02]
  provides: [clean-workflow-package, no-predictit-execution]
  affects: [arbiter/main.py, arbiter/api.py, arbiter/execution/engine.py, arbiter/workflow/]
tech_stack:
  added: []
  patterns: [dead-code-removal, clean-break]
key_files:
  created: []
  modified:
    - arbiter/workflow/__init__.py
    - arbiter/main.py
    - arbiter/api.py
    - arbiter/execution/engine.py
    - arbiter/execution/test_engine.py
  deleted:
    - arbiter/workflow/predictit_workflow.py
    - arbiter/workflow/test_predictit_workflow.py
decisions:
  - Kept workflow/ directory as empty package (api.py unwind endpoint may be repurposed)
  - Changed workflow_manager type annotation to Optional[object] for future flexibility
  - Replaced _parse_unwind_reason to return plain string since UnwindReason enum was deleted
metrics:
  duration: 4m 51s
  completed: 2026-04-16T08:46:52Z
  tasks_completed: 3
  tasks_total: 3
  files_modified: 5
  files_deleted: 2
  lines_removed: ~709
  lines_added: ~5
---

# Phase 01 Plan 03: Remove PredictIt Execution/Workflow Code Summary

Deleted PredictIt workflow module (431 lines) and test file, cleaned all PredictIt execution references from main.py, api.py, and engine.py while preserving the read-only collector and fee functions.

## What Was Done

### Task 1: Delete PredictIt workflow files and clean workflow package
- **Commit:** f955289
- Deleted `arbiter/workflow/predictit_workflow.py` (431 lines - PredictItWorkflowManager, UnwindReason, UnwindInstruction, ReminderAlert, CloseResult)
- Deleted `arbiter/workflow/test_predictit_workflow.py` (test file for deleted module)
- Replaced `arbiter/workflow/__init__.py` with empty package docstring
- Verified: `import arbiter.workflow` loads cleanly, `PredictItWorkflowManager` no longer exported

### Task 2: Remove PredictIt workflow references from main.py and api.py
- **Commit:** b415a4f
- Removed `from .workflow import PredictItWorkflowManager` import from main.py
- Removed `workflow = PredictItWorkflowManager(config.alerts)` instantiation from main.py
- Changed `workflow_manager=workflow` to `workflow_manager=None` in create_api_server call
- Removed `asyncio.create_task(workflow.run(...), name="predictit-workflow")` from task list
- Removed `await workflow.stop()` from shutdown sequence
- Removed `from .workflow import PredictItWorkflowManager, UnwindReason` import from api.py
- Changed type annotation to `Optional[object]` in api.py
- Replaced `_parse_unwind_reason` method to return plain string instead of UnwindReason enum
- Verified: both `from arbiter.main import main` and `from arbiter.api import create_api_server` import cleanly

### Task 3: Clean PredictIt strings in engine.py and update test file
- **Commit:** 3738562
- Changed "Confirm the PredictIt leg" to "Confirm the manual leg" in user-facing instruction
- Changed `notes=["PredictIt/manual workflow queued"]` to `notes=["Manual workflow queued"]`
- Renamed test from `test_manual_predictit_opportunity_creates_manual_position` to `test_manual_opportunity_creates_manual_position`
- Updated test description from "Manual PredictIt opportunity" to "Manual opportunity"
- Verified: all 11 engine tests pass

## What Was NOT Changed (by design)

- `arbiter/collectors/predictit.py` -- read-only collector still feeds price data (per D-13)
- `arbiter/scanner/arbitrage.py` -- PredictIt fee functions still needed for scanner math
- `arbiter/config/settings.py` -- PredictIt fee config and `predictit_cap` still used by MathAuditor
- `arbiter/execution/engine.py` `_queue_manual_execution` method -- still needed for any manual workflow pair
- Test data using `yes_platform="predictit"` -- PredictIt remains a valid platform for price signals

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

| Check | Result |
|-------|--------|
| `python -c "from arbiter.main import main"` | PASS |
| `python -c "from arbiter.api import create_api_server"` | PASS |
| `python -c "import arbiter.workflow"` | PASS |
| `python -m pytest arbiter/execution/test_engine.py -x` | 11 passed |
| `arbiter/collectors/predictit.py` unchanged | PASS |
| No "PredictItWorkflowManager" in main.py or api.py | PASS |

## Commits

| Task | Hash | Message |
|------|------|---------|
| 1 | f955289 | feat(01-03): delete PredictIt workflow files and clean workflow package |
| 2 | b415a4f | feat(01-03): remove PredictIt workflow references from main.py and api.py |
| 3 | 3738562 | feat(01-03): clean PredictIt strings from engine.py and rename test |

## Self-Check: PASSED

All files exist (or confirmed deleted as expected). All 3 commit hashes verified in git log.
