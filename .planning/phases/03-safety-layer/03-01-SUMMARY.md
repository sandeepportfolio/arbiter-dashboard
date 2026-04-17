---
phase: 03-safety-layer
plan: 01
subsystem: safety
tags: [kill-switch, safety-supervisor, trade-gate, websocket, postgres-audit]
requires:
  - arbiter.execution.engine.ExecutionEngine
  - arbiter.execution.adapters.base.PlatformAdapter
  - arbiter.monitor.balance.TelegramNotifier
  - arbiter.execution.store.ExecutionStore
  - arbiter.readiness.OperationalReadiness
provides:
  - arbiter.safety.SafetySupervisor
  - arbiter.safety.SafetyState
  - arbiter.safety.SafetyAlertTemplates
  - arbiter.safety.SafetyEventStore
  - arbiter.safety.RedisStateShim
  - arbiter.config.settings.SafetyConfig
  - POST /api/kill-switch
  - GET /api/safety/status
  - GET /api/safety/events
  - WebSocket event types: kill_switch, shutdown_state
affects:
  - arbiter.execution.adapters.base.PlatformAdapter (Protocol extended with cancel_all)
  - arbiter.execution.adapters.KalshiAdapter (cancel_all stub added)
  - arbiter.execution.adapters.PolymarketAdapter (cancel_all stub added)
  - arbiter.execution.engine.ExecutionEngine (new safety= kwarg + self._safety)
  - arbiter.main.run_system (chained readiness→safety trade gate)
  - arbiter.api.ArbiterAPI (new safety= ctor arg, kill-switch routes, WS fanout)
  - arbiter.web.dashboard.js (WS handler tolerates 4 new event types)
tech-stack:
  added: []
  patterns:
    - "Asyncio.Lock-serialized state machine for concurrent-safe kill-switch trip/reset"
    - "asyncio.gather with per-adapter timeout for parallel cancel_all"
    - "Structlog bind_contextvars/clear_contextvars in try/finally for scoped log context"
    - "asyncpg parameterized INSERT-only audit (threat T-3-01-D)"
    - "aiohttp.test_utils.TestServer for in-process API integration tests"
key-files:
  created:
    - arbiter/safety/__init__.py
    - arbiter/safety/supervisor.py
    - arbiter/safety/alerts.py
    - arbiter/safety/persistence.py
    - arbiter/safety/conftest.py
    - arbiter/safety/test_supervisor.py
    - arbiter/safety/test_alerts.py
    - arbiter/safety/test_persistence.py
    - arbiter/sql/safety_events.sql
    - arbiter/test_api_safety.py
  modified:
    - arbiter/config/settings.py
    - arbiter/execution/engine.py
    - arbiter/execution/adapters/base.py
    - arbiter/execution/adapters/kalshi.py
    - arbiter/execution/adapters/polymarket.py
    - arbiter/main.py
    - arbiter/api.py
    - arbiter/web/dashboard.js
decisions:
  - "SafetySupervisor.allow_execution is async and returns 3-tuple (bool, reason, context) so it matches ExecutionEngine._check_trade_gate protocol; readiness (sync, also 3-tuple) is wrapped by chained_gate in main.py"
  - "TelegramNotifier is NOT reconstructed — supervisor reuses monitor.notifier; send failures are swallowed so trip_kill always completes (threat T-3-01-J)"
  - "safety_events table is INSERT-only — SafetyEventStore exposes no UPDATE/DELETE methods (threat T-3-01-D)"
  - "cancel_all on Kalshi/Polymarket adapters land as no-op stubs; full batched impl is scheduled for plan 03-05"
  - "GET /api/safety/status is unauth'd read-only (operator email only, no secrets) so dashboard can render state without a session"
  - "Dashboard JS change is state-mutation only — renderSafety/renderOneLegAlert DOM code is owned by plan 03-07"
metrics:
  duration: "~25min"
  completed: 2026-04-16
---

# Phase 03 Plan 01: SafetySupervisor (SAFE-01) Summary

## One-liner

Standalone SafetySupervisor module owning kill-switch state, chained trade-gate (readiness → safety), append-only Postgres audit, WebSocket event fanout, and POST /api/kill-switch with operator auth.

## What Shipped

- **New `arbiter/safety/` package** (4 modules + 3 test files + conftest):
  - `supervisor.py` — `SafetySupervisor` class + `SafetyState` dataclass. asyncio.Lock-serialized `trip_kill`/`reset_kill`, `allow_execution` trade gate, `subscribe()`/Queue fanout, parallel adapter `cancel_all` with 5s per-adapter timeout, Telegram-failure tolerance.
  - `alerts.py` — `SafetyAlertTemplates.kill_armed` / `kill_reset` static methods (HTML strings; delegates egress to the shared `TelegramNotifier`).
  - `persistence.py` — `SafetyEventStore` (asyncpg INSERT-only + paginated `list_events`) and `RedisStateShim` (no-op when client is None).
  - `__init__.py` — package exports.
  - `conftest.py` — `fake_notifier` + `fake_adapter_factory` fixtures.
- **New `arbiter/sql/safety_events.sql`** migration (append-only table + `idx_safety_events_created_at DESC` index). Auto-applied in `main.py` when Postgres pool is available.
- **`SafetyConfig` dataclass** added to `arbiter/config/settings.py`; registered on `ArbiterConfig.safety`. Holds `min_cooldown_seconds=30.0`, `max_platform_exposure_usd=300.0`, `rate_limits` defaults, and `enable_redis_state` env flag.
- **`PlatformAdapter` Protocol** extended with `async def cancel_all(self) -> list[str]`. Stub implementations land in `KalshiAdapter` and `PolymarketAdapter` (log warning, return `[]`); full batched impl deferred to plan 03-05.
- **`ExecutionEngine`** accepts new keyword-only `safety: Optional[SafetySupervisor] = None` arg and stores as `self._safety` for plan 03-03's one-leg hook.
- **`arbiter/main.py`** constructs `SafetyEventStore(pool=store._pool)` + `SafetySupervisor`, late-injects `engine._safety = safety`, runs `safety_events.sql` DDL against the live pool, and replaces the single-gate wiring with `chained_gate` (readiness → safety). `safety=safety` kwarg threaded through `create_api_server`.
- **`arbiter/api.py`**:
  - New ctor kwarg `safety`, stores `self.safety` + `self.safety_store`.
  - Routes: `POST /api/kill-switch`, `GET /api/safety/status`, `GET /api/safety/events`.
  - Handler `handle_kill_switch` uses `await require_auth(request)` as line 1, validates arm/reset body, maps `ValueError` (cooldown) to 400, unknown actions to 400.
  - `_broadcast_loop` subscribes `safety_queue` when supervisor is wired; broadcasts verbatim any dict with `type in {"kill_switch", "shutdown_state"}`.
  - `_build_system_snapshot` includes top-level `"safety"` key.
- **`arbiter/web/dashboard.js`** WS handler else-if chain extended with four new tolerance branches (`kill_switch`, `rate_limit_state`, `one_leg_exposure`, `shutdown_state`) — state mutation only, zero render changes.

## Tests Moved from SKIP to PASS

| Test                                           | File                                     | Verifies                          |
|------------------------------------------------|------------------------------------------|-----------------------------------|
| `test_trip_kill_cancels_all`                   | `arbiter/safety/test_supervisor.py`      | All adapters `cancel_all` awaited within 5s |
| `test_allow_execution_armed`                   | `arbiter/safety/test_supervisor.py`      | Armed gate returns `(False, "Kill switch armed: ...", dict)` |
| `test_reset_respects_cooldown`                 | `arbiter/safety/test_supervisor.py`      | Immediate reset raises `ValueError` starting "Kill switch cooldown"; reset succeeds after cooldown |
| `test_trip_kill_publishes_event`               | `arbiter/safety/test_supervisor.py`      | Subscriber receives `{"type":"kill_switch","payload":{armed:True,...}}` |
| `test_concurrent_arm_serializes`               | `arbiter/safety/test_supervisor.py`      | 10 parallel trips → exactly 1 cancel_all call |
| `test_telegram_failure_does_not_abort_trip`    | `arbiter/safety/test_supervisor.py`      | `notifier.send` raises → trip still sets armed=True |
| `test_subscribe_delivers_kill_switch_event`    | `arbiter/safety/test_supervisor.py`      | Direct subscribe flow verified |
| `test_kill_armed_template_html`                | `arbiter/safety/test_alerts.py`          | HTML contains "KILL SWITCH ARMED", "kalshi:3", "polymarket:2" |
| `test_kill_reset_template`                     | `arbiter/safety/test_alerts.py`          | Contains "Kill switch RESET" and actor string |
| `test_redis_optional_no_op_when_client_none`   | `arbiter/safety/test_persistence.py`     | RedisStateShim(None) — no-op, no raise |
| `test_safety_event_store_none_pool_is_noop`    | `arbiter/safety/test_persistence.py`     | SafetyEventStore(None) — logs warning, no raise |
| `test_kill_switch_requires_auth`               | `arbiter/test_api_safety.py`             | POST without session → 401 |
| `test_kill_switch_arm_with_auth`               | `arbiter/test_api_safety.py`             | Authenticated arm → 200 with `armed=True` |
| `test_kill_switch_reset_cooldown_denies`       | `arbiter/test_api_safety.py`             | Reset during cooldown → 400 with "cooldown" in error |
| `test_kill_switch_unknown_action_rejected`     | `arbiter/test_api_safety.py`             | `action:"frobnicate"` → 400 "Unsupported kill-switch action" |

**Total:** 15 passing / 2 skipped (intentional — one-leg template for plan 03-03, Postgres-integration write for a live-DB fixture).

## New WebSocket Event Types

| Event type       | Emitted by                                           | Consumer                                                                                       |
|------------------|------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| `kill_switch`    | `SafetySupervisor._publish` on `trip_kill`/`reset_kill` | `arbiter/web/dashboard.js` WS handler → `state.safety.killSwitch` (no render; plan 03-07 adds renderer) |
| `shutdown_state` | (reserved) — plan 03-05                              | `arbiter/web/dashboard.js` WS handler → `state.shutdown`                                       |

Dashboard WS handler also tolerates (state-only, no render) `rate_limit_state` (plan 03-04) and `one_leg_exposure` (plan 03-03) so later plans only need to wire the sender side.

## Deferred to Later Plans

- **Full `cancel_all` implementations** for Kalshi (`DELETE /portfolio/orders/batched` chunked 20/call + `apply_retry_after`) and Polymarket (`client.cancel_all` via run_in_executor) → **plan 03-05**. Stubs here log a warning and return `[]`.
- **One-leg exposure hook** (engine emits `one_leg_exposure` event; supervisor pipes to dashboard + Telegram) → **plan 03-03**.
- **Rate-limit broadcast loop** (`rate_limit_state` periodic emission from `adapter.rate_limiter.stats`) → **plan 03-04**.
- **Per-platform exposure limit** extension to `RiskManager.check_trade` using `SafetyConfig.max_platform_exposure_usd` → **plan 03-02**.
- **Graceful shutdown re-ordering** in `main.handle_shutdown` (trip kill before task-cancel) → **plan 03-05**.
- **Market-mapping `resolution_criteria`** field → **plan 03-06**.
- **Safety panel, rate-limit pills, one-leg alert, shutdown banner** UI → **plan 03-07**.

## Deviations from Plan

**None.** Plan executed exactly as written with two small, deliberate additions:

1. **`api.safety_store` initialization** — The plan says `self.safety_store = getattr(safety, "_safety_store", None)`; implemented verbatim. This works because `SafetySupervisor.__init__` stores its `safety_store` arg on `self._safety_store`. Matches plan's instruction.
2. **`RedisStateShim` exported from `arbiter/safety/__init__.py`** — The plan's acceptance-criteria import line only lists `SafetySupervisor, SafetyState, SafetyConfig, SafetyAlertTemplates, SafetyEventStore`. RedisStateShim is also exported to make it discoverable for plan 03-05's Redis wiring. No breakage.

## Authentication Gates

**None encountered.** All 15 passing tests use in-process `aiohttp.test_utils.TestServer` + monkeypatched `UI_ALLOWED_USERS`. No live credentials, Telegram bots, or Postgres required.

## Threat Flags

No new attack surface beyond what the plan's `<threat_model>` already covered. `POST /api/kill-switch` is guarded by `require_auth` (T-3-01-A), concurrent trips serialized by `asyncio.Lock` (T-3-01-C), audit table INSERT-only (T-3-01-D), Telegram-failure tolerance (T-3-01-J), and bypass of `allow_execution` remains unaccessible from outside the engine (T-3-01-G).

## Commits

| Task | Message                                                                                                     | Commit    |
|------|-------------------------------------------------------------------------------------------------------------|-----------|
| 0    | test(03-01): scaffold SafetySupervisor/alerts/persistence/api test stubs                                    | `d09dde3` |
| 1    | feat(03-01): implement SafetySupervisor kill-switch module                                                  | `460055a` |
| 2    | feat(03-01): wire SafetySupervisor in main.py with chained readiness+safety gate                            | `daf9502` |
| 3    | feat(03-01): POST /api/kill-switch routes + WS broadcast + dashboard handler                                | `781ff6f` |

## SAFE-01 Observable Truths — all met

- [x] `SafetySupervisor.trip_kill()` cancels all open orders across adapters within 5 seconds and emits a WebSocket `kill_switch` event (`test_trip_kill_cancels_all`, `test_trip_kill_publishes_event`).
- [x] Once armed, `ExecutionEngine.execute_opportunity` rejects every new opportunity until reset — gate returns `(False, "Kill switch armed: ...", dict)` (`test_allow_execution_armed`).
- [x] `POST /api/kill-switch action=arm` requires operator auth (401 unauth, `test_kill_switch_requires_auth`) and a reason body field (400 when empty — handler returns `{"error":"reason required"}`).
- [x] `POST /api/kill-switch action=reset` respects `SafetyConfig.min_cooldown_seconds` — 400 while cooldown not elapsed (`test_kill_switch_reset_cooldown_denies`).
- [x] Telegram notifier sends `🛑 KILL SWITCH ARMED` HTML on `trip_kill` success; send failures do NOT abort `trip_kill` (`test_telegram_failure_does_not_abort_trip`, `test_kill_armed_template_html`).
- [x] `safety_events` Postgres table captures one INSERT per arm/reset — table is append-only (SafetyEventStore exposes no UPDATE/DELETE; SQL migration in `arbiter/sql/safety_events.sql`).
- [x] Concurrent arm attempts — supervisor's `asyncio.Lock` serializes so exactly one trip goes through while others see `armed` and become no-ops (`test_concurrent_arm_serializes`).
- [x] Dashboard WebSocket client receives `kill_switch` payload; existing handler does not throw on the new type (graceful fallthrough — unknown types already ignored; new branches explicitly handle).

## Self-Check: PASSED

- **Files created (10):** all verified present on disk.
- **Files modified (8):** all show `git diff` against base.
- **Commits (4):** all visible in `git log --oneline` — `d09dde3`, `460055a`, `daf9502`, `781ff6f`.
- **Test suite:** `pytest arbiter/safety/ arbiter/test_api_safety.py` → 15 passed, 2 skipped (0 failed, 0 errors).
- **Regression suite:** `pytest arbiter/execution/test_engine.py arbiter/test_api_auth.py arbiter/test_config_loading.py arbiter/test_pnl_reconciler.py arbiter/test_readiness.py arbiter/test_sentry_integration.py arbiter/test_telegram.py` → 47 passed, 0 failed.
- **JS syntax:** `node --check arbiter/web/dashboard.js` → exit 0.
- **Existing vitest:** `npx vitest run arbiter/web/dashboard-view-model.test.js` → 3/3 pass.
