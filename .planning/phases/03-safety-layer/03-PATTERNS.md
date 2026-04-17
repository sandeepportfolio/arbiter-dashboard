# Phase 3: Safety Layer - Pattern Map

**Mapped:** 2026-04-16
**Files analyzed:** 22 new/modified (6 new safety module files, 1 SQL migration, 1 config extension, 1 engine extension, 2 adapter extensions, 1 main.py restructure, 1 API extension, 4 dashboard frontend files, 6 new/extended tests)
**Analogs found:** 22 / 22

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `arbiter/safety/__init__.py` | config | — | `arbiter/execution/__init__.py` | exact |
| `arbiter/safety/supervisor.py` (NEW) | service | event-driven + pub-sub | `arbiter/execution/engine.py` (ExecutionEngine + RiskManager) | exact |
| `arbiter/safety/alerts.py` (NEW) | service | request-response (HTTP) | `arbiter/monitor/balance.py` (TelegramNotifier) | exact |
| `arbiter/safety/persistence.py` (NEW) | service | CRUD | `arbiter/execution/store.py` (ExecutionStore) | exact |
| `arbiter/safety/test_supervisor.py` (NEW) | test | — | `arbiter/execution/test_engine.py` | exact |
| `arbiter/safety/test_alerts.py` (NEW) | test | — | `arbiter/execution/test_engine.py` | role-match |
| `arbiter/safety/test_persistence.py` (NEW) | test | — | `arbiter/execution/test_store.py` | exact |
| `arbiter/sql/safety_events.sql` (NEW) | migration | — | `arbiter/sql/init.sql` | exact |
| `arbiter/execution/engine.py` (MOD) | service | event-driven | self (extend existing `RiskManager.check_trade` + `_recover_one_leg_risk`) | n/a — modifying |
| `arbiter/execution/adapters/base.py` (MOD) | protocol | — | self (extend Protocol with `cancel_all`) | n/a |
| `arbiter/execution/adapters/kalshi.py` (MOD) | service | request-response (HTTP) | self (add `cancel_all`, wire `apply_retry_after` on 429) | n/a |
| `arbiter/execution/adapters/polymarket.py` (MOD) | service | request-response (SDK) | self (add `cancel_all`, wire `apply_retry_after`) | n/a |
| `arbiter/main.py` (MOD) | entry-point | event-driven | self (restructure `handle_shutdown` at lines 297-312) | n/a |
| `arbiter/api.py` (MOD) | controller | request-response + pub-sub | self (`handle_incident_action` for POST auth, `_broadcast_loop` for WS) | n/a |
| `arbiter/config/settings.py` (MOD) | config | — | self (add `SafetyConfig` dataclass at line 374 neighborhood; extend `MARKET_MAP`) | n/a |
| `arbiter/mapping/market_map.py` (MOD) | model | CRUD | self (extend `MarketMapping` dataclass + schema) | n/a |
| `arbiter/web/dashboard.html` (MOD) | component | — | self (`<section id="opsSection">` at line 256; `<section id="infraSection">` at 341) | n/a |
| `arbiter/web/dashboard.js` (MOD) | controller | event-driven | self (WS handler 1044-1074; click handler 1964-2050; `renderIncidentQueue` 1501) | n/a |
| `arbiter/web/dashboard-view-model.js` (MOD) | utility | transform | self (`buildDeskOverview` at 102, `countCooldowns` at 59) | n/a |
| `arbiter/web/styles.css` (MOD) | config | — | self (existing `.stack-item`, `.panel`, `.status-badge`) | n/a |
| `arbiter/test_api_safety.py` (NEW) | test | — | `arbiter/test_api_integration.py` | role-match |
| `arbiter/test_main_shutdown.py` (NEW) | test | — | `arbiter/execution/test_engine.py` | role-match |

## Pattern Assignments

### `arbiter/safety/supervisor.py` (NEW — service, event-driven + pub-sub)

**Analog:** `arbiter/execution/engine.py` (ExecutionEngine lines 247-299 + RiskManager lines 200-245 + `_record_incident` lines 897-928)

**Imports pattern** (engine.py lines 1-29):
```python
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from structlog.contextvars import bind_contextvars, clear_contextvars

from ..config.settings import ArbiterConfig
from ..monitor.balance import TelegramNotifier

if TYPE_CHECKING:
    from ..execution.engine import ExecutionEngine, ExecutionIncident
    from ..execution.adapters.base import PlatformAdapter
    from ..execution.store import ExecutionStore

logger = logging.getLogger("arbiter.safety")
```

**Dataclass pattern** (engine.py lines 43-95, `Order.to_dict()`):
```python
@dataclass
class SafetyState:
    armed: bool = False
    armed_by: Optional[str] = None
    armed_at: float = 0.0
    armed_reason: str = ""
    cooldown_until: float = 0.0
    last_reset_at: float = 0.0
    last_reset_by: str = ""

    def to_dict(self) -> dict:
        return {
            "armed": self.armed,
            "armed_by": self.armed_by,
            "armed_at": self.armed_at,
            "armed_reason": self.armed_reason,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining": max(self.cooldown_until - time.time(), 0.0),
            "last_reset_at": self.last_reset_at,
            "last_reset_by": self.last_reset_by,
        }
```

**Subscriber/queue fanout pattern** (engine.py lines 270-299, 890-921):
```python
# Constructor — parallel to ExecutionEngine
self._subscribers: List[asyncio.Queue] = []
self._incident_subscribers: List[asyncio.Queue] = []

def subscribe(self) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    self._subscribers.append(queue)
    return queue

async def _publish(self, event: dict) -> None:
    # Same try/put_nowait/QueueFull pattern as _publish_execution (line 890)
    for subscriber in list(self._subscribers):
        try:
            subscriber.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("Skipping slow safety subscriber")
```

**Trade-gate callable pattern** (engine.py lines 288-289, 1087-1101):
```python
# Engine's existing _check_trade_gate contract — supervisor's allow_execution
# must return Tuple[bool, str, Dict[str, Any]] exactly.
# Engine handles tuple[2] (legacy) and tuple[3] (new):
#     if isinstance(result, tuple):
#         if len(result) == 3:
#             allowed, reason, context = result
#             return bool(allowed), str(reason), dict(context or {})

async def allow_execution(self, opp) -> Tuple[bool, str, Dict[str, Any]]:
    if self._state.armed:
        return False, f"Kill switch armed: {self._state.armed_reason}", self._state.to_dict()
    return True, "safety supervisor approved", {"kill_switch": False}
```

**Concurrency-safe state transition pattern** (engine.py uses `clear_contextvars` / `bind_contextvars`; borrow the `asyncio.Lock()` pattern from Pitfall "Race condition: concurrent trip+reset" in RESEARCH.md):
```python
self._state_lock = asyncio.Lock()

async def trip_kill(self, by: str, reason: str) -> SafetyState:
    async with self._state_lock:   # serialize trip/reset
        ...
```

**Parallel fanout pattern** (NEW; pattern analog is `asyncio.gather` used in `engine.py` `_live_execution` lines 652+):
```python
# Fanout cancel_all across adapters with per-platform timeout
async def _cancel_platform(platform: str, adapter):
    try:
        return platform, await asyncio.wait_for(adapter.cancel_all(), timeout=5.0)
    except Exception as exc:
        logger.error("Kill cancel failed platform=%s err=%s", platform, exc)
        return platform, []

results = await asyncio.gather(
    *[_cancel_platform(p, a) for p, a in self.adapters.items()],
    return_exceptions=True,
)
```

---

### `arbiter/safety/supervisor.py` — RiskManager extension (pre-trade per-platform limit)

**Analog:** `arbiter/execution/engine.py:200-245` (`RiskManager.check_trade`)

**Current per-market-only check** (lines 224-230):
```python
exposure = opp.suggested_qty * (opp.yes_price + opp.no_price)
existing = self._open_positions.get(opp.canonical_id, 0.0)
if existing + exposure > self.config.max_position_usd:
    return False, "Per-market exposure limit exceeded"
total_exposure = sum(self._open_positions.values()) + exposure
if total_exposure > self._max_total_exposure:
    return False, "Total exposure limit exceeded"
```

**Extension pattern for per-platform aggregation** (apply IN `RiskManager.check_trade`, not supervisor — Phase 3 needs this running synchronously at submission):
```python
# Add _platform_exposures: Dict[str, float] = {} to __init__
# Modify record_trade/release_trade to split exposure across platforms
# Add check after line 227:
for plat in (opp.yes_platform, opp.no_platform):
    leg_exposure = opp.suggested_qty * (
        opp.yes_price if plat == opp.yes_platform else opp.no_price
    )
    current = self._platform_exposures.get(plat, 0.0)
    if current + leg_exposure > self.config.max_platform_exposure_usd:
        return False, f"Per-platform exposure limit exceeded on {plat}"
```

---

### `arbiter/safety/alerts.py` (NEW — service, request-response HTTP)

**Analog:** `arbiter/monitor/balance.py:28-72` (TelegramNotifier class)

**Imports pattern** (balance.py lines 1-17):
```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

import aiohttp

from ..config.settings import AlertConfig

logger = logging.getLogger("arbiter.safety.alerts")
```

**Send-via-aiohttp pattern** (balance.py lines 42-67) — do NOT introduce a new HTTP client; compose messages and delegate to existing `TelegramNotifier.send()`:
```python
# Safety alert templates compose messages, delegate sending.
# TelegramNotifier.send() signature (balance.py:42):
#     async def send(self, message: str, parse_mode: str = "HTML") -> bool

class SafetyAlertTemplates:
    @staticmethod
    def kill_armed(by: str, reason: str, cancelled_counts: dict[str, int]) -> str:
        counts = " | ".join(f"{p}:{n}" for p, n in cancelled_counts.items())
        return (
            f"🛑 <b>KILL SWITCH ARMED</b>\n"
            f"By: {by}\n"
            f"Reason: {reason}\n"
            f"Cancelled: {counts}\n"
            f"Manual reset required."
        )

    @staticmethod
    def kill_reset(by: str, note: str) -> str:
        return f"🟢 <b>Kill switch RESET</b>\nBy: {by}\nNote: {note}"

    @staticmethod
    def one_leg_exposure(canonical_id: str, filled_platform: str,
                         filled_side: str, fill_qty: int,
                         exposure_usd: float, unwind_instruction: str) -> str:
        return (
            f"🚨 <b>NAKED POSITION</b>\n"
            f"Market: {canonical_id}\n"
            f"Filled: {fill_qty} {filled_side.upper()} on {filled_platform.upper()}\n"
            f"Exposure: ${exposure_usd:.2f}\n"
            f"Unwind: {unwind_instruction}"
        )
```

**Disabled-fallback pattern** (balance.py lines 35, 44-46) — copy verbatim:
```python
self._enabled = bool(bot_token and chat_id)
# ...
async def send(self, message: str, parse_mode: str = "HTML") -> bool:
    if not self._enabled:
        logger.debug(f"Telegram disabled, would send: {message[:80]}...")
        return False
```

---

### `arbiter/safety/persistence.py` (NEW — service, CRUD)

**Analog:** `arbiter/execution/store.py:197-222` (`ExecutionStore.insert_incident`)

**Connect/pool pattern** (store.py — reuse the exact pattern):
```python
# Uses asyncpg pool, same as ExecutionStore
class SafetyEventStore:
    def __init__(self, pool: Optional[asyncpg.Pool] = None):
        self._pool = pool

    async def insert_safety_event(
        self,
        *,
        event_type: str,          # "arm" | "reset" | "shutdown_trip"
        actor: str,               # "operator:sparx@..." | "system:shutdown"
        reason: str,
        state: dict,              # SafetyState.to_dict()
        cancelled_counts: dict[str, int] | None = None,
    ) -> None:
        if self._pool is None:
            await self.connect()
        state_json = json.dumps(state, default=str)
        counts_json = json.dumps(cancelled_counts or {}, default=str)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO safety_events (
                    event_id, event_type, actor, reason,
                    state_json, cancelled_counts_json, created_at
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, NOW())
                """,
                f"SE-{uuid.uuid4().hex[:8]}",
                event_type,
                actor,
                reason,
                state_json,
                counts_json,
            )
```

**Redis live-state pattern** — no existing analog in repo for Redis SET/GET of kill state; closest is `arbiter/utils/price_store.py` which accepts `redis_client=None`. Follow the same optional-client contract: accept `redis_client: Optional[redis.asyncio.Redis] = None`, no-op on None.

---

### `arbiter/sql/safety_events.sql` (NEW — migration)

**Analog:** `arbiter/sql/init.sql:3-49` (CREATE TABLE trades / alerts)

**Schema pattern** (init.sql lines 41-48 for alerts is the closest structural match — event log with type, message, timestamp):
```sql
CREATE TABLE IF NOT EXISTS safety_events (
    event_id VARCHAR(20) PRIMARY KEY,
    event_type VARCHAR(30) NOT NULL,    -- 'arm' | 'reset' | 'shutdown_trip' | 'cooldown_denied'
    actor VARCHAR(200) NOT NULL,        -- 'operator:<email>' | 'system:shutdown'
    reason TEXT NOT NULL,
    state_json JSONB NOT NULL,
    cancelled_counts_json JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_safety_events_created_at
    ON safety_events (created_at DESC);
```

**Append-only constraint** (per RESEARCH Security section): this table is INSERT-only; never UPDATE or DELETE. No ON CONFLICT clause (unlike `execution_incidents` at store.py:208-211). Mirrors `alerts` table in init.sql which has no update path.

---

### `arbiter/execution/engine.py` — MOD: one-leg exposure surfacing

**Analog:** self — extend `_recover_one_leg_risk` at line 852

**Current structure** (lines 852-867):
```python
async def _recover_one_leg_risk(self, arb_id, opp, leg_yes, leg_no) -> List[str]:
    self._recovery_count += 1
    notes: List[str] = []
    await self._record_incident(
        arb_id, opp, "critical",
        "Partial fill or one-leg risk detected, starting recovery",
        metadata={"leg_yes": leg_yes.to_dict(), "leg_no": leg_no.to_dict()},
    )
    for leg in (leg_yes, leg_no):
        if leg.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL}:
            cancelled = await self._cancel_order(leg)
            notes.append(f"cancel-{leg.side}:{'ok' if cancelled else 'failed'}")
    return notes
```

**Extension pattern** — enrich metadata with structured event_type + recommended unwind (Phase 3 RESEARCH Pattern 4, lines 441-476):
```python
# Determine exposure side
filled_leg = leg_yes if leg_yes.status == OrderStatus.FILLED else leg_no
failed_leg = leg_no if leg_yes.status == OrderStatus.FILLED else leg_yes
exposure_usd = filled_leg.fill_qty * filled_leg.fill_price

incident = await self._record_incident(
    arb_id, opp, "critical",
    "One-leg exposure detected — naked position requires unwind",
    metadata={
        "event_type": "one_leg_exposure",              # NEW structured type
        "filled_platform": filled_leg.platform,
        "filled_side": filled_leg.side,
        "filled_qty": filled_leg.fill_qty,
        "filled_price": filled_leg.fill_price,
        "exposure_usd": exposure_usd,
        "failed_platform": failed_leg.platform,
        "failed_reason": failed_leg.error,
        "recommended_unwind": (
            f"Sell {filled_leg.fill_qty} {filled_leg.side.upper()} on "
            f"{filled_leg.platform.upper()} at market to close exposure"
        ),
    },
)
if self._safety is not None:     # supervisor is late-injected
    await self._safety.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)
```

**WS-event routing** (downstream in `api.py:_broadcast_loop`): subscribe to incidents, filter `metadata.event_type == "one_leg_exposure"`, re-emit as dedicated `one_leg_exposure` WS event in addition to the generic `incident` event.

---

### `arbiter/execution/adapters/base.py` — MOD: add `cancel_all` to Protocol

**Analog:** self (existing Protocol methods at lines 31-88)

**Existing method signature pattern** (lines 68-70):
```python
async def cancel_order(self, order: Order) -> bool:
    """Best-effort cancel — used for EXEC-05 timeout path and one-leg recovery."""
    ...
```

**New method signature** (mirror docstring style, same return shape as existing cancel):
```python
async def cancel_all(self) -> list[str]:
    """Cancel every open order on this platform in a single batched operation.

    Returns list of cancelled order_ids (best-effort — empty list on adapter
    error, never raises). Used by SafetySupervisor.trip_kill() (SAFE-01) and
    graceful shutdown (SAFE-05).

    Kalshi:     DELETE /portfolio/orders/batched (20 orders per call, chunked).
    Polymarket: client.cancel_all() single SDK call.
    """
    ...
```

---

### `arbiter/execution/adapters/kalshi.py` — MOD: `cancel_all` + 429 `apply_retry_after`

**Analog:** self — `cancel_order` at lines 210-225 shows delete pattern; `_post_order` at lines 192-206 shows rate_limiter + HTTP pattern.

**Existing delete pattern to copy from** (lines 219-225):
```python
@transient_retry()
async def _delete_order(self, order_id: str) -> bool:
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    url = f"{self.config.kalshi.base_url}/portfolio/orders/{order_id}"
    headers = self.auth.get_headers("DELETE", path)
    async with self.session.delete(url, headers=headers) as response:
        return response.status in (200, 204)
```

**New `cancel_all` pattern** (batch-delete with chunking, rate-limit acquire per chunk):
```python
async def cancel_all(self) -> list[str]:
    if not self.auth or not getattr(self.auth, "is_authenticated", False):
        return []
    try:
        open_orders = await self._list_all_open_orders()  # use existing Kalshi list endpoint
    except Exception as exc:
        log.error("kalshi.cancel_all.list_failed", err=str(exc))
        return []
    if not open_orders:
        return []

    cancelled_ids: list[str] = []
    CHUNK_SIZE = 20    # Kalshi batch-cancel limit
    path = "/trade-api/v2/portfolio/orders/batched"
    for i in range(0, len(open_orders), CHUNK_SIZE):
        chunk = open_orders[i:i + CHUNK_SIZE]
        await self.rate_limiter.acquire()
        headers = self.auth.get_headers("DELETE", path)
        payload = {"ids": [o.order_id for o in chunk]}
        try:
            async with self.session.delete(
                f"{self.config.kalshi.base_url}/portfolio/orders/batched",
                json=payload, headers=headers,
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    for entry in body:
                        if not entry.get("error"):
                            cancelled_ids.append(entry["order_id"])
                else:
                    text = await resp.text()
                    log.warning("kalshi.cancel_all.chunk_failed",
                                status=resp.status, body=text[:200])
        except Exception as exc:
            log.error("kalshi.cancel_all.raised", err=str(exc))
    return cancelled_ids
```

**429 retry-after pattern** — extend existing `_post_order` at lines 192-206:
```python
# After the async with block, BEFORE returning:
if response.status == 429:
    retry_after = response.headers.get("Retry-After", "1")
    delay = self.rate_limiter.apply_retry_after(
        retry_after, fallback_delay=2.0, reason="kalshi_429",
    )
    log.warning("kalshi.rate_limited", penalty_seconds=delay)
    self.circuit.record_failure()
    # FOK semantics — do NOT retry, return 429 status so place_fok marks FAILED
```

---

### `arbiter/execution/adapters/polymarket.py` — MOD: `cancel_all` via SDK

**Analog:** self — `cancel_order` at line 330

**New `cancel_all` pattern** (use existing `run_in_executor` pattern already used elsewhere for sync SDK calls):
```python
async def cancel_all(self) -> list[str]:
    client = self.clob_client_factory()
    if client is None:
        return []
    await self.rate_limiter.acquire()
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: client.cancel_all())
        if isinstance(result, dict):
            return list(result.get("canceled", []))
        return []
    except Exception as exc:
        log.error("polymarket.cancel_all.raised", err=str(exc))
        return []
```

---

### `arbiter/main.py` — MOD: graceful shutdown re-ordering (SAFE-05)

**Analog:** self — `handle_shutdown` at lines 297-312

**Current (broken — cancels tasks BEFORE orders)**:
```python
shutdown_event = asyncio.Event()
def handle_shutdown(sig):
    logger.info(f"Received {sig.name}, shutting down...")
    shutdown_event.set()

for sig in (signal.SIGINT, signal.SIGTERM):
    asyncio.get_event_loop().add_signal_handler(sig, handle_shutdown, sig)

await shutdown_event.wait()
# Cancel all tasks
for task in tasks:
    task.cancel()
await asyncio.gather(*tasks, return_exceptions=True)
```

**Fix pattern** (RESEARCH Pattern 3, lines 397-431):
```python
shutdown_event = asyncio.Event()
shutting_down = False

def handle_shutdown(sig):
    nonlocal shutting_down
    if shutting_down:
        logger.warning(f"Received {sig.name} again, forcing immediate exit")
        os._exit(1)
    shutting_down = True
    logger.info(f"Received {sig.name}, shutting down...")
    shutdown_event.set()

for sig in (signal.SIGINT, signal.SIGTERM):
    asyncio.get_event_loop().add_signal_handler(sig, handle_shutdown, sig)

await shutdown_event.wait()

# ─── NEW ordering: cancel ORDERS first, THEN tasks ────────────
logger.info("Tripping safety kill-switch for graceful shutdown...")
try:
    await asyncio.wait_for(
        safety.trip_kill(by="system:shutdown", reason="Process shutdown signal"),
        timeout=5.0,
    )
except asyncio.TimeoutError:
    logger.error("Kill-switch trip exceeded 5s — some orders may remain open")

# NOW cancel background tasks (previously at line 309)
logger.info("Stopping all components...")
for task in tasks:
    task.cancel()
await asyncio.gather(*tasks, return_exceptions=True)
```

**Supervisor construction pattern** (insert between lines 210-212 where existing `readiness` is constructed):
```python
# After line 211 (engine.set_trade_gate(readiness.allow_execution)):
from .safety.supervisor import SafetySupervisor, SafetyConfig

safety_config = SafetyConfig(
    min_cooldown_seconds=30.0,
    max_platform_exposure_usd=300.0,
    rate_limits={
        "kalshi": {"write_rps": 10, "read_rps": 100},
        "polymarket": {"write_rps": 5, "read_rps": 50},
    },
)
safety = SafetySupervisor(
    safety_config, engine=engine, adapters=adapters,
    notifier=monitor.notifier,
    redis=None, store=store,
)

# Chain gates — readiness FIRST, safety SECOND
async def chained_gate(opp):
    # readiness.allow_execution signature matches existing single-gate at line 211
    readiness_res = readiness.allow_execution(opp)
    if asyncio.iscoroutine(readiness_res):
        readiness_res = await readiness_res
    if isinstance(readiness_res, tuple):
        if len(readiness_res) >= 1 and not readiness_res[0]:
            return readiness_res
    elif not readiness_res:
        return (False, "readiness denied", {})
    return await safety.allow_execution(opp)

engine.set_trade_gate(chained_gate)
# Also: engine._safety = safety  (late-inject for one-leg hook)
```

---

### `arbiter/api.py` — MOD: POST /api/kill-switch + GET /api/safety/* + new WS events

**Analog:** self — multiple patterns

**POST route + auth pattern** (lines 191, 316-355, `handle_market_mapping_action`):
```python
# Route registration (add in serve() around line 191):
app.router.add_post("/api/kill-switch", self.handle_kill_switch)
app.router.add_get("/api/safety/status", self.handle_safety_status)
app.router.add_get("/api/safety/events", self.handle_safety_events)

# Handler — copy the structure of handle_market_mapping_action (lines 316-355):
async def handle_kill_switch(self, request):
    await require_auth(request)                      # ← auth gate (line 317 pattern)
    payload = await self._read_json_body(request)
    action = str(payload.get("action", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    note = str(payload.get("note", "")).strip()

    try:
        if action == "arm":
            if not reason:
                return web.json_response({"error": "reason required"}, status=400)
            email = await get_current_user(request) or "unknown"
            state = await self.safety.trip_kill(
                by=f"operator:{email}", reason=reason,
            )
            return web.json_response(state.to_dict())
        elif action == "reset":
            email = await get_current_user(request) or "unknown"
            state = await self.safety.reset_kill(
                by=f"operator:{email}", note=note or "operator reset",
            )
            return web.json_response(state.to_dict())
        else:
            return web.json_response(
                {"error": f"Unsupported kill-switch action: {action or 'unknown'}"},
                status=400,
            )
    except ValueError as exc:
        # e.g. cooldown not elapsed
        return web.json_response({"error": str(exc)}, status=400)
```

**Authentication pattern** (lines 131-136):
```python
async def require_auth(request: web.Request) -> str:
    user = await get_current_user(request)
    if not user:
        raise HTTPUnauthorized(reason="Authentication required")
    return user
```

**WebSocket broadcast pattern** (lines 596-648):
```python
# Current _broadcast_loop — extend by adding new queues + new event types
async def _broadcast_loop(self):
    price_queue = self.store.subscribe()
    opp_queue = self.scanner.subscribe()
    execution_queue = self.engine.subscribe()
    incident_queue = self.engine.subscribe_incidents()
    safety_queue = self.safety.subscribe()            # NEW

    while True:
        if not self._ws_clients:
            await asyncio.sleep(1.0)
            continue
        try:
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(price_queue.get()),
                    asyncio.create_task(opp_queue.get()),
                    asyncio.create_task(execution_queue.get()),
                    asyncio.create_task(incident_queue.get()),
                    asyncio.create_task(safety_queue.get()),           # NEW
                    asyncio.create_task(asyncio.sleep(2.0)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                if task.cancelled():
                    continue
                result = task.result()
                if isinstance(result, PricePoint):
                    await self._broadcast_json({"type": "quote", "payload": result.to_dict()})
                elif isinstance(result, ArbitrageOpportunity):
                    await self._broadcast_json({"type": "opportunity", "payload": result.to_dict()})
                elif isinstance(result, ArbExecution):
                    await self._broadcast_json({"type": "execution", "payload": result.to_dict()})
                elif isinstance(result, ExecutionIncident):
                    # Standard incident broadcast
                    await self._broadcast_json({"type": "incident", "payload": result.to_dict()})
                    # NEW: if metadata flags one-leg, re-emit as dedicated event
                    if result.metadata.get("event_type") == "one_leg_exposure":
                        await self._broadcast_json({
                            "type": "one_leg_exposure", "payload": result.to_dict(),
                        })
                elif isinstance(result, dict) and result.get("type") in (
                    "kill_switch", "rate_limit_state", "shutdown_state",
                ):
                    # Supervisor emits pre-shaped {"type": ..., "payload": ...} dicts
                    await self._broadcast_json(result)
                elif result is None:
                    continue
                else:
                    await self._broadcast_json({"type": "system", "payload": await self._build_system_snapshot()})
        except Exception as exc:
            logger.error("Broadcast error: %s", exc)
            await asyncio.sleep(1.0)
```

**Periodic rate_limit_state emission** — add a separate `_rate_limit_broadcast_loop` task; pattern analog is `engine.polymarket_heartbeat_loop()` (engine.py:978):
```python
async def _rate_limit_broadcast_loop(self):
    while True:
        await asyncio.sleep(2.0)
        if not self._ws_clients:
            continue
        snapshot = {
            platform: adapter.rate_limiter.stats
            for platform, adapter in self.engine.adapters.items()
        }
        await self._broadcast_json({"type": "rate_limit_state", "payload": snapshot})
```

**Extend `_build_system_snapshot`** (line 650-690): add `"safety"` and `"rate_limits"` keys so HTTP GET `/api/system` serves the same shape as the WS bootstrap:
```python
return {
    "timestamp": time.time(),
    # ... existing fields ...
    "safety": self.safety._state.to_dict() if self.safety else {"armed": False},
    "rate_limits": {
        p: a.rate_limiter.stats for p, a in self.engine.adapters.items()
    },
    # ...
}
```

**GET /api/safety/events pagination** — follow the snapshot pattern (handle_portfolio_summary at line 428):
```python
async def handle_safety_events(self, request):
    # Query string: ?limit=50&offset=0
    limit = min(int(request.query.get("limit", 50)), 500)
    offset = max(int(request.query.get("offset", 0)), 0)
    rows = await self.safety_store.list_events(limit=limit, offset=offset)
    return web.json_response({"events": rows, "limit": limit, "offset": offset})
```

---

### `arbiter/config/settings.py` — MOD: SafetyConfig + MARKET_MAP `resolution_criteria`

**Analog:** self — existing `ScannerConfig` at lines 374-385, `MARKET_MAP` at 292-295

**SafetyConfig dataclass pattern** (mirror `ScannerConfig` structure):
```python
# Insert after ScannerConfig (around line 386)
@dataclass
class SafetyConfig:
    min_cooldown_seconds: float = 30.0
    max_platform_exposure_usd: float = 300.0
    rate_limits: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {
            "kalshi": {"write_rps": 10.0, "read_rps": 100.0},
            "polymarket": {"write_rps": 5.0, "read_rps": 50.0},
        }
    )
    enable_redis_state: bool = field(
        default_factory=lambda: os.getenv("SAFETY_REDIS_STATE", "false").lower() == "true"
    )

# Then register on ArbiterConfig (line 405-413):
@dataclass
class ArbiterConfig:
    # ... existing fields ...
    safety: SafetyConfig = field(default_factory=SafetyConfig)
```

**MARKET_MAP `resolution_criteria` extension** (RESEARCH Pattern 5, lines 484-510). `MarketMappingRecord` is a frozen dataclass at line 169 — add optional field:
```python
@dataclass(frozen=True)
class MarketMappingRecord:
    # ... existing fields ...
    resolution_criteria: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        d = {..., "resolution_criteria": self.resolution_criteria or None}
        # Optional — None means "pending operator review"
        return d
```

`update_market_mapping` at lines 302-320 gets a new optional kwarg:
```python
def update_market_mapping(
    canonical_id: str,
    *,
    status: str | None = None,
    note: str | None = None,
    allow_auto_trade: bool | None = None,
    resolution_criteria: dict | None = None,     # NEW
) -> dict | None:
    mapping = MARKET_MAP.get(canonical_id)
    if not mapping:
        return None
    # ... existing assignments ...
    if resolution_criteria is not None:
        mapping["resolution_criteria"] = resolution_criteria
    mapping["updated_at"] = time.time()
    return mapping
```

**Pitfall guard** (RESEARCH Pitfall 6): `resolution_criteria` MUST remain optional — every consumer uses `.get("resolution_criteria")` or `mapping.get("...", default)`. Never add a required schema key that breaks `MARKET_MAP` fixture seeds.

---

### `arbiter/mapping/market_map.py` — MOD: extend MarketMapping dataclass + schema

**Analog:** self — `MarketMapping` dataclass at line 33

**Extension pattern** (mirror existing optional fields like `notes: str = ""`):
```python
@dataclass
class MarketMapping:
    # ... existing fields (lines 35-53) ...
    resolution_criteria_json: str = ""        # JSONB-serialized payload
    resolution_match_status: str = "pending_operator_review"
    # Values: "identical" | "similar" | "divergent" | "pending_operator_review"

    def to_dict(self) -> dict:
        d = {...}  # existing keys
        d["resolution_criteria"] = (
            json.loads(self.resolution_criteria_json)
            if self.resolution_criteria_json else None
        )
        d["resolution_match_status"] = self.resolution_match_status
        return d
```

**SQL migration for existing `market_mappings` table** (line 100 CREATE TABLE):
```sql
ALTER TABLE market_mappings
    ADD COLUMN IF NOT EXISTS resolution_criteria JSONB,
    ADD COLUMN IF NOT EXISTS resolution_match_status VARCHAR(40)
        DEFAULT 'pending_operator_review';
```

---

### `arbiter/web/dashboard.html` — MOD: new `<section id="safetySection">`

**Analog:** self — existing `opsSection` at line 256, `riskSection` at line 230

**Placement + markup pattern** (mirrors `opsSection` structure line 256-280):
```html
<!-- Insert BEFORE commandCenter (line 178) so operators see kill-switch first -->
<section id="safetySection" class="panel-grid panel-grid-tight ops-only"
         data-ops-only="true" aria-label="Safety controls">
  <article class="panel safety-panel" data-kill-switch-panel>
    <div class="panel-header">
      <div>
        <p class="panel-kicker">Safety</p>
        <h2>Kill switch</h2>
      </div>
      <span id="killSwitchBadge" class="panel-badge status-badge">Disarmed</span>
    </div>
    <p id="killSwitchSummary" class="panel-copy compact-copy">
      Armed state halts all new order submission and cancels open orders across venues.
    </p>
    <div class="kill-switch-controls">
      <button id="killSwitchArm" type="button"
              class="btn btn-danger kill-switch-arm">ARM KILL SWITCH</button>
      <button id="killSwitchReset" type="button"
              class="btn btn-secondary kill-switch-reset hidden">Reset kill switch</button>
      <span id="killSwitchCooldown" class="kill-switch-cooldown hidden">Cooldown: 00:30</span>
    </div>
    <div id="rateLimitIndicators" class="rate-limit-grid"></div>
  </article>

  <article class="panel one-leg-alert-panel hidden" data-one-leg-alert>
    <div class="panel-header">
      <div>
        <p class="panel-kicker">Naked position</p>
        <h2>One-leg exposure</h2>
      </div>
      <span id="oneLegBadge" class="panel-badge status-badge status-critical">Critical</span>
    </div>
    <div id="oneLegAlertBody" class="stack-list"></div>
  </article>
</section>

<!-- Shutdown banner lives inside the utility-bar (line 58), toggles .hidden -->
<div id="shutdownBanner" class="shutdown-banner hidden" role="status" aria-live="polite">
  <span id="shutdownBannerText">Server shutting down…</span>
</div>
```

**Operator-gated visibility pattern** — class `ops-only` + `data-ops-only="true"` are already wired (see `opsSection` lines 256 and `renderChrome` handling at `dashboard.js:1108`). No new visibility logic required.

---

### `arbiter/web/dashboard.js` — MOD: WS event handling + render + click actions

**Analog:** self — WS handler at lines 1044-1074, click dispatcher at 1964-2050, `renderIncidentQueue` at 1501, `postJson` at 957-967

**WS event handler extension** (insert in the else-if chain at line 1070, right before `heartbeat`):
```javascript
// Add to socket.addEventListener("message", ...) — after the existing "incident" branch
} else if (message.type === "kill_switch") {
  state.safety = { ...(state.safety || {}), killSwitch: message.payload };
} else if (message.type === "rate_limit_state") {
  state.safety = { ...(state.safety || {}), rateLimits: message.payload };
} else if (message.type === "one_leg_exposure") {
  state.oneLegExposures = [message.payload, ...(state.oneLegExposures || [])].slice(0, 8);
} else if (message.type === "shutdown_state") {
  state.shutdown = message.payload;
}
// existing "heartbeat" branch follows
```

**Render registration** — append to `render()` at line 1086:
```javascript
function render() {
  // ... existing renders ...
  renderSafetyPanel();        // NEW
  renderOneLegAlert();        // NEW
  renderShutdownBanner();     // NEW
}
```

**Render pattern for Safety panel** — copy the structure of `renderIncidentQueue` at lines 1501-1533:
```javascript
function renderSafetyPanel() {
  const ks = state.safety?.killSwitch || { armed: false };
  const badge = document.getElementById("killSwitchBadge");
  const summary = document.getElementById("killSwitchSummary");
  const armBtn = document.getElementById("killSwitchArm");
  const resetBtn = document.getElementById("killSwitchReset");
  const cooldownEl = document.getElementById("killSwitchCooldown");
  if (!badge) return;

  if (ks.armed) {
    badge.textContent = "ARMED";
    badge.className = "panel-badge status-badge status-critical";
    summary.textContent =
      `Armed by ${ks.armed_by || "unknown"} at ` +
      `${new Date((ks.armed_at || 0) * 1000).toLocaleTimeString()}. ` +
      `Reason: ${ks.armed_reason || "n/a"}`;
    armBtn.classList.add("hidden");
    resetBtn.classList.remove("hidden");
    const remaining = Number(ks.cooldown_remaining || 0);
    if (remaining > 0) {
      cooldownEl.classList.remove("hidden");
      const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
      const ss = String(Math.floor(remaining % 60)).padStart(2, "0");
      cooldownEl.textContent = `Cooldown: ${mm}:${ss}`;
      resetBtn.disabled = true;
    } else {
      cooldownEl.classList.add("hidden");
      resetBtn.disabled = false;
    }
  } else {
    badge.textContent = "Disarmed";
    badge.className = "panel-badge status-badge status-ok";
    summary.textContent =
      "Armed state halts all new order submission and cancels open orders across venues.";
    armBtn.classList.remove("hidden");
    resetBtn.classList.add("hidden");
    cooldownEl.classList.add("hidden");
  }
  renderRateLimitBadges();
}
```

**Click handler pattern for kill-switch** — copy the shape of incident-action block at lines 1997-2008:
```javascript
// Insert inside document.addEventListener("click", ...) — before the apiConfigButtonEl block
const killArm = event.target.closest("#killSwitchArm");
if (killArm) {
  if (!hasOperatorAccess()) {
    showAuthOverlay("Sign in to arm the kill switch.");
    return;
  }
  if (!window.confirm(
    "ARM the kill switch? This will cancel ALL open orders and halt new execution."
  )) return;
  const reason = window.prompt("Reason for arming?", "Operator manual") || "Operator manual";
  void runAction(killArm, () =>
    postJson("/api/kill-switch", { action: "arm", reason }),
  );
  return;
}

const killReset = event.target.closest("#killSwitchReset");
if (killReset) {
  if (!hasOperatorAccess()) {
    showAuthOverlay("Sign in to reset the kill switch.");
    return;
  }
  if (!window.confirm("Reset the kill switch? New orders will resume immediately.")) return;
  void runAction(killReset, () =>
    postJson("/api/kill-switch", { action: "reset", note: "Operator reset" }),
  );
  return;
}
```

**Close-handler shutdown distinction** — extend existing WS close handler at lines 1076-1081:
```javascript
socket.addEventListener("close", () => {
  if (state.websocket !== socket) return;
  state.wsConnected = false;
  if (state.shutdown?.phase === "shutting_down" || state.shutdown?.phase === "complete") {
    setWsLabel("Server shutdown complete", true);
    return;                                    // don't auto-reconnect after intentional shutdown
  }
  setWsLabel(state.system ? "Polling" : "Reconnecting", true);
  window.setTimeout(connectWebSocket, 1500);
});
```

---

### `arbiter/web/dashboard-view-model.js` — MOD: `buildSafetyView` + `buildRateLimitView`

**Analog:** self — `buildDeskOverview` at line 102, `countCooldowns` at line 59

**Pure-function transform pattern** (view-model.js is purely pure transforms — no DOM, no fetch):
```javascript
// Insert after buildMetricCards (line 204)
export function buildSafetyView(state, options = {}) {
  const now = Number(options.nowTimestamp || Date.now() / 1000);
  const ks = state.safety?.killSwitch || { armed: false };
  const cooldownRemaining = Math.max(0, Number(ks.cooldown_until || 0) - now);
  return {
    armed: Boolean(ks.armed),
    badgeLabel: ks.armed ? "ARMED" : "Disarmed",
    badgeClass: ks.armed ? "status-critical" : "status-ok",
    summary: ks.armed
      ? `Armed by ${ks.armed_by || "unknown"} — ${ks.armed_reason || "no reason"}`
      : "Kill switch disarmed. Armed state cancels open orders.",
    cooldownRemainingSeconds: cooldownRemaining,
    canReset: ks.armed && cooldownRemaining <= 0,
  };
}

export function buildRateLimitView(state) {
  const limits = state.safety?.rateLimits || {};
  return Object.entries(limits).map(([platform, stats]) => {
    const remainingPenalty = Number(stats?.remaining_penalty_seconds || 0);
    const available = Number(stats?.available_tokens || 0);
    const max = Number(stats?.max_requests || 0);
    return {
      platform,
      platformLabel: platform === "kalshi" ? "Kalshi"
                   : platform === "polymarket" ? "Polymarket" : platform,
      tokensLabel: `${available}/${max}`,
      tone: remainingPenalty > 0 ? "warn" : "ok",
      cooldownLabel: remainingPenalty > 0 ? `${remainingPenalty.toFixed(1)}s cooldown` : "idle",
    };
  });
}
```

---

### `arbiter/web/styles.css` — MOD: safety styles

**Analog:** self — existing `.stack-item`, `.panel`, `.status-badge`, `.status-critical`, `.status-ok`, `.btn`, `.hidden`

**Extension scope** — only add **new** selectors; never modify existing panel/stack styles. Use existing tone utilities (`.status-critical`, `.status-ok`) for color consistency.

**New selectors required:**
```css
.kill-switch-controls { display: flex; gap: 12px; align-items: center; margin-top: 12px; }
.btn-danger { background: var(--color-critical, #c0392b); color: white; }
.kill-switch-cooldown { font-variant-numeric: tabular-nums; }
.rate-limit-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-top: 12px; }
.rate-limit-pill { padding: 4px 10px; border-radius: 999px; font-size: 12px; }
.rate-limit-pill.warn { background: rgba(255, 168, 0, 0.12); color: #f39c12; }
.rate-limit-pill.ok { background: rgba(80, 200, 120, 0.12); color: #27ae60; }

.one-leg-alert-panel { border: 2px solid var(--color-critical, #c0392b); animation: one-leg-pulse 1.5s infinite; }
@keyframes one-leg-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(192, 57, 43, 0.4); } 50% { box-shadow: 0 0 0 12px rgba(192, 57, 43, 0); } }

.shutdown-banner { position: fixed; top: 0; left: 0; right: 0; padding: 12px 24px;
                   background: var(--color-critical, #c0392b); color: white; font-weight: 600;
                   z-index: 9999; text-align: center; }
```

---

### `arbiter/safety/test_supervisor.py` (NEW — test)

**Analog:** `arbiter/execution/test_engine.py`

**Testing harness pattern** — pytest-asyncio with fixtures for engine, adapters, notifier (use `AsyncMock` from `unittest.mock`).

**Test cases from RESEARCH §Validation Architecture (lines 999-1003):**
- `test_trip_kill_cancels_all` — assert all adapter.cancel_all() awaited in parallel within 5s
- `test_allow_execution_armed` — armed state returns `(False, reason, ctx)`
- `test_reset_respects_cooldown` — raises ValueError when `now < cooldown_until`
- `test_trip_kill_publishes_event` — subscribers receive `{"type": "kill_switch", ...}` dict

---

### `arbiter/test_api_safety.py` (NEW — test)

**Analog:** `arbiter/test_api_integration.py` (existing aiohttp test client pattern)

**Auth-required test pattern** (copy from existing tests that exercise POST /api/auth/login):
- 401 without session cookie
- 200 with valid operator session
- Body validation: `action` must be `"arm"` or `"reset"`, else 400
- Cooldown denial returns 400 with error message

---

### `arbiter/test_main_shutdown.py` (NEW — test)

**Analog:** existing engine tests that use AsyncMock adapters

**Key assertion (SAFE-05):** adapter.cancel_all() is called BEFORE any task.cancel(). Use a spy adapter that records call order:

```python
call_order = []
class SpyAdapter:
    async def cancel_all(self):
        call_order.append("cancel_all")
        return []
# Start a dummy task that also records when cancelled:
async def dummy_task():
    try:
        await asyncio.sleep(999)
    except asyncio.CancelledError:
        call_order.append("task_cancelled")
        raise
# After triggering shutdown, assert:
# assert call_order.index("cancel_all") < call_order.index("task_cancelled")
```

---

## Shared Patterns

### Authentication (operator-gated POST routes)

**Source:** `arbiter/api.py:131-136` (`require_auth`)
**Apply to:** `POST /api/kill-switch`, any future POST on /api/safety/*

```python
async def require_auth(request: web.Request) -> str:
    user = await get_current_user(request)
    if not user:
        raise HTTPUnauthorized(reason="Authentication required")
    return user
```

**Usage at handler top** (line 317):
```python
async def handle_kill_switch(self, request):
    await require_auth(request)           # raises HTTPUnauthorized on failure
    # ... rest of handler
```

**Operator identity in audit writes** — every state-changing action records the email:
```python
email = await get_current_user(request) or "unknown"
await self.safety.trip_kill(by=f"operator:{email}", reason=reason)
```

### Error Handling (backend handlers)

**Source:** `arbiter/api.py:316-385` (handle_market_mapping_action / handle_manual_position_action)
**Apply to:** All new POST handlers

Pattern:
1. `await require_auth(request)` first.
2. Read body via `self._read_json_body(request)`.
3. Validate `action` field — return `web.json_response({"error": ...}, status=400)` for unknown.
4. Call service; catch `ValueError` for user-facing validation → 400.
5. Return 404 if resource not found, 200 with updated state dict otherwise.
6. Never `raise` across the boundary — always `web.json_response({"error": ...})`.

### Subscriber/Publish Pattern (Queue fanout)

**Source:** `arbiter/execution/engine.py:270-299, 890-921`
**Apply to:** `SafetySupervisor._publish`, periodic rate-limit broadcaster

Every publisher:
- Maintains `self._subscribers: List[asyncio.Queue]` with `Queue(maxsize=100)`
- Exposes `subscribe() -> asyncio.Queue` factory
- In publish path: `for s in list(self._subscribers): try: s.put_nowait(event); except asyncio.QueueFull: logger.debug(...)`

Consumer side (api.py:596-640): `asyncio.wait([...queue.get() tasks..., asyncio.sleep(timeout)], return_when=FIRST_COMPLETED)` — drain then re-arm on every loop.

### Incident Emission Pattern

**Source:** `arbiter/execution/engine.py:897-928` (`record_incident`)
**Apply to:** Every safety event that should land in the incident queue AND Postgres audit

```python
incident = ExecutionIncident(
    incident_id=f"INC-{uuid.uuid4().hex[:8]}",
    arb_id=arb_id,
    canonical_id=opp.canonical_id,
    severity="critical" | "warning" | "info",
    message="human-readable summary",
    timestamp=time.time(),
    metadata={"event_type": "one_leg_exposure", ...},   # structured tag
)
self._incidents.appendleft(incident)
# fan out to subscribers (WebSocket)
for sub in list(self._incident_subscribers):
    try: sub.put_nowait(incident)
    except asyncio.QueueFull: ...
# persist (Postgres audit)
if self.store is not None:
    await self.store.insert_incident(incident)
```

### TelegramNotifier Reuse

**Source:** `arbiter/monitor/balance.py:28-72`
**Apply to:** Every Phase 3 alert (kill arm/reset, one-leg exposure, shutdown, test-telegram button)

NEVER construct a second notifier. The existing `monitor.notifier` instance (balance.py line 87) is passed into SafetySupervisor at construction. Alert templates build strings; `notifier.send(html_message)` is the only egress.

**Silence-on-disabled** (line 44-46) is already correct behavior — don't propagate send failures back to the caller of `trip_kill`. Kill trip must complete even if Telegram is down.

### Rate Limiter Wiring

**Source:** `arbiter/utils/retry.py:224-332` (existing `RateLimiter` class) + `arbiter/execution/adapters/kalshi.py:200` (existing call site)
**Apply to:** Every adapter outbound HTTP/SDK call

Pattern:
```python
await self.rate_limiter.acquire()        # FIRST — before any network I/O
async with self.session.<method>(...) as resp:
    if resp.status == 429:
        delay = self.rate_limiter.apply_retry_after(
            resp.headers.get("Retry-After", "1"),
            fallback_delay=2.0,
            reason=f"{platform}_429",
        )
        self.circuit.record_failure()
        # FOK: never retry — return FAILED order with error="rate_limited"
```

Stats surface automatically via `rate_limiter.stats` (line 321-332); no extra wiring for dashboard visibility.

### Dashboard State-to-Render Flow

**Source:** `arbiter/web/dashboard.js` — state mutation in WS handler (1044-1074) → `render()` call (1086) → individual `renderFoo()` functions (e.g. `renderIncidentQueue:1501`, `renderMappings:1673`).

**Apply to:** Every new WS event type. Three steps:
1. Add `else if (message.type === "X")` branch that mutates `state.X`
2. Add `renderX()` to the `render()` function at line 1086
3. Implement `renderX()` reading from `state.X`, writing innerHTML of a fixed `document.getElementById(...)` node.

Never skip the `state` intermediate — view-model.js transforms state into display objects (pure functions); dashboard.js handles DOM writes.

### Operator-Gated Click Actions

**Source:** `arbiter/web/dashboard.js:1984-2021` (manualTarget / incidentTarget / mappingTarget blocks)

**Apply to:** `#killSwitchArm`, `#killSwitchReset`, `#testTelegram` buttons

Every operator action:
```javascript
const target = event.target.closest("#selectorId");
if (target) {
  if (!hasOperatorAccess()) {
    showAuthOverlay("Sign in to ...");
    return;
  }
  // optional: window.confirm() for destructive actions
  void runAction(target, () => postJson("/api/endpoint", { action, ... }));
  return;
}
```

`runAction` (line 1935) handles: disable button, "Working..." label, await operation, reload snapshot, re-enable.

### SQL Persistence (append-only audit)

**Source:** `arbiter/execution/store.py:197-222` (insert_incident)
**Apply to:** `SafetyEventStore.insert_safety_event`

- Use `asyncpg.Pool.acquire()` context manager.
- `INSERT INTO ... VALUES (..., $N::jsonb, ...)` for JSONB payload columns.
- `default=str` in `json.dumps()` for datetime/Decimal safety.
- **Never** write `UPDATE` or `DELETE` against `safety_events` — append-only (per Security analysis).
- `created_at TIMESTAMPTZ DEFAULT NOW()` server-side timestamp (prevents clock skew tampering).

## No Analog Found

None. All 22 files have a concrete in-repo analog.

Notes on "partial" matches (still acceptable, with caveats):

| File | Caveat |
|------|--------|
| `arbiter/safety/persistence.py` Redis half | Repo has `redis[hiredis]` as a dep but no existing async Redis kill-switch analog. Closest is `arbiter/utils/price_store.py` which accepts `redis_client=None` and no-ops — follow the same optional-client idiom. |
| `arbiter/sql/safety_events.sql` | Closest analog `arbiter/sql/init.sql:41-48` (`alerts` table) is simpler; use it for column style (`VARCHAR(30)`, `TEXT`, `TIMESTAMPTZ DEFAULT NOW()`) but extend with JSONB columns like `execution_incidents` uses. |
| `.one-leg-alert-panel` pulse animation | No existing pulse/critical animation in styles.css; new keyframe is greenfield. Align color tokens with existing `--color-critical` if defined; otherwise use `#c0392b` inline and schedule a design-token cleanup later. |

## Metadata

**Analog search scope:**
- `arbiter/execution/` (engine, adapters, store, tests)
- `arbiter/monitor/` (balance, telegram notifier)
- `arbiter/portfolio/` (monitor, config)
- `arbiter/utils/` (retry — RateLimiter + CircuitBreaker)
- `arbiter/api.py` (routes, ws, auth)
- `arbiter/main.py` (shutdown handler, wiring)
- `arbiter/config/settings.py` (dataclasses, MARKET_MAP)
- `arbiter/mapping/market_map.py` (MarketMapping dataclass + SQL schema)
- `arbiter/sql/init.sql` (CREATE TABLE conventions)
- `arbiter/web/dashboard.html` (section/panel structure)
- `arbiter/web/dashboard.js` (WS, render, click handlers)
- `arbiter/web/dashboard-view-model.js` (pure transform functions)

**Files scanned:** ~30 Python + 4 frontend + 1 SQL + 1 HTML

**Pattern extraction date:** 2026-04-16

**Key cross-cutting invariants for planner:**
1. All POST routes requiring operator auth MUST call `await require_auth(request)` as their first line (api.py:131-136 pattern).
2. All queue publishers use `maxsize=100` + `put_nowait` + `except asyncio.QueueFull: logger.debug(...)` (engine.py:270-299 pattern).
3. All persistent audit writes use `asyncpg.Pool.acquire()` + parameterized INSERT (store.py:197-222 pattern).
4. All adapter outbound calls begin with `await self.rate_limiter.acquire()` and handle 429 via `apply_retry_after` (kalshi.py:200 + retry.py:294 pattern).
5. All dashboard operator actions flow through `postJson(path, payload)` + `runAction(button, op)` (dashboard.js:957-967, 1935-1962 pattern).
6. All dataclasses with dashboard-serializable state expose `to_dict()` (engine.py:63-95 pattern).
7. All WebSocket broadcasts shape as `{"type": "<event>", "payload": <dict>}` (api.py:626-637 pattern).
