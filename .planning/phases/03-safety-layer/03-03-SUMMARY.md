---
phase: 03-safety-layer
plan: 03
subsystem: safety
tags: [one-leg-exposure, naked-position, safety-supervisor, telegram-alert, websocket-event, safe-03]
requires:
  - arbiter.execution.engine.ExecutionEngine
  - arbiter.execution.engine.Order
  - arbiter.execution.engine.OrderStatus
  - arbiter.execution.engine._record_incident
  - arbiter.execution.engine.ExecutionIncident
  - arbiter.safety.supervisor.SafetySupervisor
  - arbiter.safety.alerts.SafetyAlertTemplates.one_leg_exposure
  - arbiter.monitor.balance.TelegramNotifier
provides:
  - "SafetySupervisor.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)"
  - "ExecutionIncident metadata contract for event_type='one_leg_exposure'"
  - "WebSocket event type: one_leg_exposure (payload is ExecutionIncident.to_dict())"
  - "Telegram NAKED POSITION template wiring (template existed in plan 03-01, now called in production path)"
affects:
  - arbiter.execution.engine.ExecutionEngine._recover_one_leg_risk (naked-position branch added)
  - arbiter.safety.supervisor.SafetySupervisor (handle_one_leg_exposure method added)
  - arbiter.api.ArbiterAPI._broadcast_loop (dedicated one_leg_exposure re-emit branch added)
tech-stack:
  added: []
  patterns:
    - "Three-channel operator notification fanout: incident queue + Telegram + dedicated WS event — each channel independent so failure of any one does not silence the operator"
    - "Supervisor hook guarded by `self._safety is not None` (late-injected attribute pattern from plan 03-01) so engine tests without supervisor wiring still pass"
    - "Telegram failure swallowed inside handle_one_leg_exposure (threat T-3-03-C DoS mitigation)"
    - "Structured ExecutionIncident.metadata as a stable contract for the plan 03-07 UI hero banner — 9 required keys enforced by unit test"
key-files:
  created: []
  modified:
    - arbiter/safety/supervisor.py
    - arbiter/safety/test_alerts.py
    - arbiter/safety/test_supervisor.py
    - arbiter/execution/engine.py
    - arbiter/execution/test_engine.py
    - arbiter/api.py
decisions:
  - "Replace generic 'Partial fill or one-leg risk' incident with structured event_type=one_leg_exposure incident ONLY when exactly one leg is FILLED; preserve generic fallback for both-filled/both-failed/partial cases so the recovery path remains visible in ops logs"
  - "Exposure math uses float(fill_qty) * float(fill_price) — same formula in both the engine metadata and the supervisor hook fallback so a synthetic incident without pre-computed exposure still renders correctly"
  - "handle_one_leg_exposure reads exposure_usd from incident.metadata FIRST (authoritative value the engine computed), falls back to filled_leg attributes — ensures payload consistency across the three channels"
  - "WebSocket re-emit uses incident.to_dict() verbatim (same shape as the generic 'incident' event) so the dashboard's oneLegExposures state already captures everything without any payload transform"
  - "Dashboard JS explicitly NOT modified — the state.oneLegExposures tolerance branch from plan 03-01 is sufficient; hero-banner render lands in plan 03-07"
metrics:
  duration: "~4min"
  completed: 2026-04-17
---

# Phase 03 Plan 03: One-Leg Exposure Surfacing (SAFE-03) Summary

## One-liner

`_recover_one_leg_risk` now emits a structured `event_type=one_leg_exposure` critical incident with explicit unwind recommendation, hands off to `SafetySupervisor.handle_one_leg_exposure` which fires the Telegram NAKED POSITION template AND publishes a dedicated `one_leg_exposure` WebSocket event — three independent notification channels so a naked position always reaches the operator.

## What Shipped

### Engine — `arbiter/execution/engine.py::_recover_one_leg_risk`

The function now classifies legs at entry:

- **Naked position branch** (`yes_filled ^ no_filled`): emits a single structured incident with `severity="critical"` and all nine metadata keys (event_type, filled_platform, filled_side, filled_qty, filled_price, exposure_usd, failed_platform, failed_reason, recommended_unwind), then calls `self._safety.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)` when the supervisor attribute is wired. Hook exceptions are caught so the cancel-still-open loop runs regardless.
- **Fallback branch** (both filled / both failed / partials): preserves the pre-existing generic `"Partial fill or one-leg risk detected"` incident so the recovery path stays visible when the naked-position classifier doesn't match.
- **Cancel loop at tail**: unchanged — still best-effort cancels any SUBMITTED/PENDING/PARTIAL leg after the incident fanout.

### Supervisor — `arbiter/safety/supervisor.py::SafetySupervisor.handle_one_leg_exposure`

New method with the following contract:

```python
async def handle_one_leg_exposure(
    self, incident, filled_leg, failed_leg, opp
) -> None: ...
```

Reads `incident.metadata.exposure_usd` (authoritative, set by the engine) with a `filled_leg.fill_qty * filled_leg.fill_price` fallback; formats the Telegram body via `SafetyAlertTemplates.one_leg_exposure(...)`; wraps `notifier.send` in try/except so a Telegram outage does not block. Publishes `{"type": "one_leg_exposure", "payload": incident.to_dict() or {canonical_id, metadata, incident_id}}` to every subscriber queue. Binds structlog contextvars with `event=safety.one_leg_exposure` and clears them on exit. Logs at WARNING level with exposure USD for audit trail.

### API — `arbiter/api.py::_broadcast_loop`

The existing `elif isinstance(result, ExecutionIncident):` branch still broadcasts the generic `{"type": "incident", "payload": ...}` event for ALL incidents, then conditionally re-emits `{"type": "one_leg_exposure", "payload": result.to_dict()}` when `result.metadata.get("event_type") == "one_leg_exposure"`. The two events are independent: a dashboard consumer subscribed only to `incident` still sees the full incident; a consumer listening for the dedicated `one_leg_exposure` channel gets it without scanning incident metadata.

### Telegram template — `arbiter/safety/alerts.py`

**Unchanged.** The `SafetyAlertTemplates.one_leg_exposure(...)` static method was already shipped in plan 03-01 as a placeholder. Plan 03-03 unskipped its test (`test_one_leg_template_contains_required_parts`) and wired it into the production path via the supervisor hook.

## Three Notification Channels — composition & independence

| Channel | Emitter | Consumer | Independence guarantee |
|---------|---------|----------|-------------------------|
| **Incident queue** (generic) | `ExecutionEngine.record_incident` → `_incident_subscribers` deque + Postgres `insert_incident` | `api.py::_broadcast_loop` → WS `type=incident` event; dashboard `renderIncidentQueue` | Runs even when supervisor is None (engine tests exercise this path); persists even when WS clients are absent |
| **Telegram** | `SafetySupervisor.handle_one_leg_exposure` → `SafetyAlertTemplates.one_leg_exposure` → `notifier.send(html)` | Configured operator Telegram chat | Wrapped in try/except inside supervisor; Telegram outage does NOT block the engine cancel loop (threat T-3-03-C) |
| **Dedicated WS event** | `SafetySupervisor._publish({"type":"one_leg_exposure",...})` AND (independently) `api.py::_broadcast_loop` re-emit on metadata match | Dashboard `state.oneLegExposures` ring buffer (plan 03-01 state-tolerance branch) | Fires even if supervisor is None — the api.py re-emit branch triggers purely on incident metadata, so a supervisor-less engine still surfaces naked positions to WS consumers |

**Key design point:** the dedicated WS event fires from TWO independent places — the supervisor's `_publish` (when supervisor is wired) and the `api.py::_broadcast_loop` re-emit (always, so long as an incident with the right metadata is broadcast). Dashboard consumers will see the event whether or not `SafetySupervisor` is attached to the engine, as long as `_record_incident` runs.

## Required `one_leg_exposure` Incident Metadata Keys (contract for plan 03-07 UI)

Every `event_type=one_leg_exposure` incident carries these keys (enforced by `test_one_leg_exposure_surfaces_structured_metadata`):

| Key | Type | Semantics |
|-----|------|-----------|
| `event_type` | str | Constant `"one_leg_exposure"` — primary dispatch field |
| `filled_platform` | str | Lowercase platform name of the FILLED leg (e.g. `"kalshi"`) |
| `filled_side` | str | `"yes"` or `"no"` — which side got filled |
| `filled_qty` | int | Filled quantity in contracts |
| `filled_price` | float | Per-contract fill price (0..1 range) |
| `exposure_usd` | float | `filled_qty * filled_price` — dollar exposure |
| `failed_platform` | str | Lowercase platform name of the FAILED/non-filled leg |
| `failed_reason` | str | `failed_leg.error` if present, else `str(failed_leg.status)` |
| `recommended_unwind` | str | Non-empty human-readable instruction: `"Sell {qty} {side} on {platform} at market to close exposure"` |

Plan 03-07's UI hero banner consumes this shape directly from the dashboard's `state.oneLegExposures[0]` entry.

## Tests

Added to the existing test files (all green):

| Test | File | Verifies |
|------|------|----------|
| `test_one_leg_template_contains_required_parts` | `arbiter/safety/test_alerts.py` | Template output contains `NAKED POSITION`, canonical_id, platform (case-insensitive), qty, `$56.00`, unwind instruction |
| `test_handle_one_leg_exposure_sends_telegram_and_publishes` | `arbiter/safety/test_supervisor.py` | Supervisor hook fires Telegram once with `NAKED POSITION`; publishes dedicated `one_leg_exposure` event whose payload carries `canonical_id` |
| `test_one_leg_exposure_surfaces_structured_metadata` | `arbiter/execution/test_engine.py` | `_recover_one_leg_risk` with one FILLED + one FAILED leg emits exactly one incident with all nine required metadata keys at expected values (`event_type=one_leg_exposure`, `exposure_usd≈56.0`, `"Sell 100 YES" in recommended_unwind`) |
| `test_one_leg_exposure_invokes_supervisor_hook` | `arbiter/execution/test_engine.py` | When `engine._safety = AsyncMock()` is set, `_recover_one_leg_risk` calls `_safety.handle_one_leg_exposure` exactly once with (incident, filled_leg=leg_yes, failed_leg=leg_no, opp) |

**Overall suite:**
- `pytest arbiter/safety/test_alerts.py arbiter/safety/test_supervisor.py arbiter/execution/test_engine.py -x` → **38 passed**
- `pytest arbiter/safety/ arbiter/execution/test_engine.py -x` → **40 passed, 1 skipped** (skipped = plan 03-01's Postgres-integration write)
- Broader regression: `pytest arbiter/test_api_safety.py arbiter/execution/ arbiter/safety/` → **119 passed, 3 skipped, 1 pre-existing failure** (the pre-existing `test_complete_stub_satisfies_protocol` documented in `deferred-items.md` by plan 03-02 — unchanged)

## Acceptance-Criteria Greps (all met)

| Expected | Actual |
|----------|--------|
| `grep -c "one_leg_exposure" arbiter/safety/alerts.py` ≥ 1 | 1 |
| `grep -c "NAKED POSITION" arbiter/safety/alerts.py` = 1 | 1 |
| `grep -c "async def handle_one_leg_exposure" arbiter/safety/supervisor.py` = 1 | 1 |
| `grep -c "event_type.*one_leg_exposure" arbiter/execution/engine.py` ≥ 1 | 1 |
| `grep -c "if self._safety is not None" arbiter/execution/engine.py` ≥ 1 | 1 |
| `grep -c "one_leg_exposure" arbiter/api.py` ≥ 1 | 3 |
| `grep -c "state.oneLegExposures" arbiter/web/dashboard.js` = 1 | 1 |

Template smoke (with UTF-8 encoding): `from arbiter.safety.alerts import SafetyAlertTemplates; SafetyAlertTemplates.one_leg_exposure(canonical_id='X', filled_platform='kalshi', filled_side='yes', fill_qty=1, exposure_usd=1.0, unwind_instruction='...')` → returns 85-char string containing `NAKED POSITION`, `X`, `KALSHI`, `$1.00`. (Direct `python -c` call failed at stdout emit on cp1252 Windows console due to the 🚨 emoji — the TELEGRAM wire format is UTF-8 so this is purely a console rendering quirk, not a runtime bug. Confirmed via `PYTHONIOENCODING=utf-8` roundtrip.)

## Dashboard JS — deliberately unchanged

`arbiter/web/dashboard.js` was NOT modified in this plan. The state-capture branch added in plan 03-01 (line 1076):

```javascript
} else if (message.type === "one_leg_exposure") {
  state.oneLegExposures = [message.payload, ...(state.oneLegExposures || [])].slice(0, 8);
}
```

is sufficient to accumulate incoming `one_leg_exposure` WebSocket payloads into a ring buffer. The hero-level banner render lives in plan 03-07 Task 2. Success criteria verified by grep:  `grep -c "state.oneLegExposures" arbiter/web/dashboard.js` = 1.

## Known Limitation

The best-effort `_cancel_order` call on the still-open leg (tail of `_recover_one_leg_risk`) is **unchanged** — if the cancel RPC fails (platform rate-limit, network partition, adapter bug), the still-open leg remains open on the venue. The mitigating factor is that the operator sees the alert via all three channels (incident queue + Telegram + dedicated WS) and can unwind manually via the existing `/api/portfolio/unwind/{position_id}` endpoint. A future plan (beyond 03-07) could add automated retry of the cancel with back-off, but that's out of scope here — SAFE-03 is about notification reliability, not auto-recovery.

## Deviations from Plan

**None.** Plan executed exactly as written. All tasks followed the `<behavior>` block verbatim:
- Engine branch structure (yes_filled ^ no_filled) matches the plan's specified classifier.
- Supervisor method signature, metadata extraction, Telegram try/except, `_publish` call, and logging all match the `<behavior>` pseudocode.
- API `_broadcast_loop` re-emit added immediately after the generic incident broadcast per plan's position specification.
- Tests precisely mirror the `<action>` block (4 tests across 3 files; all pass after Task 1 lands).

## Authentication Gates

**None encountered.** All tests run in-process with AsyncMock adapters and no external service dependencies.

## Threat Flags

No new attack surface beyond what the plan's `<threat_model>` already covered:
- **T-3-03-A** (Info Disclosure via Telegram): accepted — template includes canonical_id + counts + severity, no raw order_ids or bot tokens. Operator chat compromise is out of scope for ASVS L1.
- **T-3-03-B** (Forged WS event): mitigated — `_broadcast_loop` only fans out events sourced from internal publishers; the re-emit branch uses `result.to_dict()` on an `isinstance(result, ExecutionIncident)` check so external payloads cannot inject.
- **T-3-03-C** (Telegram hang blocks engine): mitigated — `notifier.send` is wrapped in try/except inside `handle_one_leg_exposure`; engine's `try/except` around the hook call is a second safety net.
- **T-3-03-D** (False one_leg_exposure scare): mitigated — only reachable via `_recover_one_leg_risk`; no external RPC can trigger the emit.
- **T-3-03-E** (Fill price disclosure via WS): accepted — fill price is public market data.
- **T-3-03-F** (Bot token leaked via log): mitigated — existing `TelegramNotifier` logs truncated previews (`{message[:80]}...`); token held in `self._token`, never logged.

## Commits

| Task | Message | Commit |
|------|---------|--------|
| 0    | `test(03-03): add red tests for one-leg exposure metadata + supervisor hook + NAKED template` | `fde3169` |
| 1    | `feat(03-03): one-leg exposure surfaces structured incident + Telegram + dedicated WS event` | `506f9a3` |

## SAFE-03 Observable Truths — all met

- [x] `ExecutionEngine._recover_one_leg_risk` with one FILLED + one FAILED leg emits ExecutionIncident with `severity='critical'` and `metadata.event_type='one_leg_exposure'` within the same scan cycle (`test_one_leg_exposure_surfaces_structured_metadata`).
- [x] Incident metadata contains all nine required keys with correct values (`filled_platform`, `filled_side`, `filled_qty`, `filled_price`, `exposure_usd`, `failed_platform`, `failed_reason`, `recommended_unwind` non-empty, `event_type`) — enforced per-key by test assertion.
- [x] `SafetySupervisor.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)` fires the Telegram NAKED POSITION template and publishes a `one_leg_exposure` WebSocket event (`test_handle_one_leg_exposure_sends_telegram_and_publishes`).
- [x] `arbiter/api.py::_broadcast_loop` detects `metadata.event_type == 'one_leg_exposure'` on incoming incidents and re-emits as a dedicated `one_leg_exposure` WebSocket event in addition to the generic `incident` event (new branch; grep confirms).
- [x] Dashboard JS tolerance branch from plan 03-01 captures `one_leg_exposure` payloads into `state.oneLegExposures` — confirmed unchanged; render lands in plan 03-07.
- [x] Unit tests assert exactly-one-incident emission AND exactly-one supervisor hook invocation (`test_one_leg_exposure_invokes_supervisor_hook` uses `assert_called_once`).

## Self-Check: PASSED

- **Files modified (6):**
  - `arbiter/safety/supervisor.py` — present, contains `async def handle_one_leg_exposure`
  - `arbiter/safety/test_alerts.py` — present, `test_one_leg_template_contains_required_parts` collects
  - `arbiter/safety/test_supervisor.py` — present, `test_handle_one_leg_exposure_sends_telegram_and_publishes` collects
  - `arbiter/execution/engine.py` — present, contains `event_type` + `one_leg_exposure` + `if self._safety is not None`
  - `arbiter/execution/test_engine.py` — present, both new tests collect
  - `arbiter/api.py` — present, contains dedicated `one_leg_exposure` broadcast branch
- **Commits (2):** `fde3169`, `506f9a3` — both visible via `git log --oneline`
- **Pytest gate:** `pytest arbiter/safety/ arbiter/execution/test_engine.py -x` → 40 passed, 1 skipped, 0 failed
- **Template smoke:** `SafetyAlertTemplates.one_leg_exposure(...)` returns an 85-char UTF-8 string containing `NAKED POSITION` and all required tokens
- **JS regression:** `node --check arbiter/web/dashboard.js` → exit 0 (file unchanged by this plan)
