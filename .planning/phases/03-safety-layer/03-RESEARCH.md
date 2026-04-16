# Phase 3: Safety Layer - Research

**Researched:** 2026-04-16
**Domain:** Async Python safety infrastructure for live prediction-market arbitrage (kill switch, position limits, one-leg recovery, rate limiting, graceful shutdown, Telegram alerting) + dashboard UI surfaces for operator-driven emergency control
**Confidence:** HIGH

## Summary

Phase 3 wraps a safety net around the already-hardened Phase 2 execution engine. Every one of SAFE-01..SAFE-06 maps onto existing code surfaces -- there is nothing architecturally novel here. The heavy lifting is (1) inserting a single global **kill-switch gate** in `ExecutionEngine.execute_opportunity` (which already has a clean `_check_trade_gate` hook from Phase 2), (2) tightening the existing `RiskManager.check_trade` per-platform/per-market limits, (3) promoting `_recover_one_leg_risk` from "log incident + cancel" to "log incident + cancel + Telegram alert + persist unwind recommendation" so the operator sees it, (4) calling the existing `RateLimiter` at every adapter outbound call (it's already instantiated in `main.py` but only partially wired), (5) hardening the existing SIGINT/SIGTERM handler in `main.py` to cancel orders BEFORE cancelling tasks, and (6) adding a resolution-criteria field + dashboard comparison view to the existing market-mapping review workflow.

The biggest risk is not technical -- it's **UI visibility of the safety state**. The current dashboard has rich panels for incidents, manual queue, risk score, and portfolio violations, but **zero operator-controllable kill-switch UI**, **no per-platform rate-limit indicators**, **no one-leg-exposure callout** (naked positions currently surface only as a "critical" incident buried in the incident queue), **no shutdown-in-progress feedback**, and **no resolution-criteria comparison view** for mapping confirmation. The WebSocket protocol needs four new event types (`kill_switch`, `rate_limit_state`, `one_leg_exposure`, `shutdown_state`) and the REST API needs two new endpoints (`POST /api/kill-switch`, `GET /api/safety/status`) to let the operator drive safety from the UI.

**Primary recommendation:** Build a single `SafetySupervisor` module (`arbiter/safety/supervisor.py`) that owns the kill-switch state, subscribes to `ExecutionEngine` incidents, exposes `allow_execution(opp) -> (bool, reason)` as the trade gate, and publishes `SafetyState` snapshots over WebSocket. Keep `RateLimiter` per-adapter (already built, just wire it in adapter `place_fok`/`cancel_order` hot paths). Promote the existing `RiskManager._max_total_exposure` and `_max_daily_loss` from hardcoded defaults to `SafetyConfig` fields. For Telegram alerts, extend the existing `TelegramNotifier` (proven working in Phase 2) with a `send_safety_alert(event)` method -- do NOT introduce a new async framework. For graceful shutdown, the existing `handle_shutdown` pattern in `main.py` is correct structurally -- it just needs to **cancel orders via adapters BEFORE** cancelling background tasks (currently these happen in the wrong order).

## User Constraints (from CONTEXT.md)

CONTEXT.md does not yet exist for Phase 3. Based on ROADMAP.md and REQUIREMENTS.md, the following constraints apply:

### Locked Decisions (from ROADMAP + REQUIREMENTS)

- **D-01:** Kill switch cancels all open/pending orders, halts new execution, alerts via Telegram, and is triggerable from dashboard and programmatic thresholds (SAFE-01)
- **D-02:** Position limits enforced per-platform AND per-market before order submission (SAFE-02)
- **D-03:** One-leg recovery detects naked directional positions and executes automated or operator-assisted unwind (SAFE-03)
- **D-04:** Per-platform API rate limiting prevents throttling/bans (Kalshi 10 writes/sec minimum target, Polymarket per docs) (SAFE-04)
- **D-05:** Graceful shutdown cancels all open orders before process exit on SIGINT/SIGTERM (SAFE-05)
- **D-06:** Market mapping includes resolution criteria comparison -- operator must verify both platforms resolve identically before approving pairs (SAFE-06)

### Claude's Discretion

- Kill-switch persistence layer (Redis / in-memory / Postgres) -- Claude picks
- Kill-switch "manual reset" UX (confirmation modal vs. cooldown timer vs. both) -- Claude picks
- Position-limit source of truth (RiskManager in-memory vs. PortfolioMonitor vs. new module) -- Claude picks
- One-leg-exposure detection mechanism (scanner loop check vs. engine-inline vs. new supervisor task) -- Claude picks
- Rate-limiter scope (per-adapter vs. global token bucket) -- Claude picks
- Telegram alert throttling and severity routing -- Claude picks
- Resolution criteria data shape + MARKET_MAP schema extension -- Claude picks
- Dashboard UI placement (new "Safety" section vs. integrate into existing panels) -- Claude picks

### Deferred Ideas (OUT OF SCOPE per v1 line-item review)

- Telegram `/kill` inbound command handler (OPT-05, v2)
- Automated kill-switch triggers tied to daily-loss threshold / error-rate ceiling (OPT-04, v2)
- WebSocket price feeds (OPT-01, v2)
- Settlement divergence monitoring (MON-01, v2)
- Automated position-size scaling (OUT OF SCOPE per REQUIREMENTS)

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SAFE-01 | Kill switch cancels all orders + halts new execution + Telegram alert, dashboard-triggerable and programmatic | New `SafetySupervisor`; existing `_check_trade_gate` hook in `ExecutionEngine`; batch-cancel via Kalshi DELETE `/portfolio/orders/batched` (20 orders/call) + Polymarket `client.cancel_all()` + existing `TelegramNotifier` |
| SAFE-02 | Position limits per-platform + per-market before order submission | Tighten existing `RiskManager.check_trade`; config already has `max_position_usd` (per-market) -- add `SafetyConfig.max_platform_exposure_usd` (per-platform) |
| SAFE-03 | One-leg recovery detects naked positions, auto or operator-assisted unwind | Existing `_recover_one_leg_risk` in `engine.py:852`; Phase 2 already wrote CR-01 timeout recovery; add Telegram alert + UnwindInstruction persistence + dashboard surface |
| SAFE-04 | Per-platform API rate limiting | Existing `RateLimiter` in `utils/retry.py:224` (token bucket, already supports `apply_retry_after`); Kalshi cap is **10 writes/sec per member, 100 reads/sec** per docs; Polymarket per docs |
| SAFE-05 | Graceful shutdown cancels all orders before exit | Existing `handle_shutdown` in `main.py:297` -- restructure to call `adapter.cancel_all()` BEFORE `task.cancel()` so orders die before the loop does |
| SAFE-06 | Market mapping includes resolution criteria comparison | Extend `MARKET_MAP` schema with `resolution_criteria: {kalshi, polymarket}` fields; dashboard mapping panel gets side-by-side rule comparison UI |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Kill-switch state machine (armed / disarmed / cooling-down) | `SafetySupervisor` (new module) | Redis/memory persistence | One authoritative source; survives engine restarts via persistence |
| Kill-switch trade gate | `ExecutionEngine._check_trade_gate` (existing hook) | `SafetySupervisor.allow_execution` | Engine already has the gate; supervisor implements the policy |
| Batch order cancellation on kill | `PlatformAdapter.cancel_all()` (new method on Protocol) | Engine orchestrates | Each platform has different batch-cancel semantics; adapter is the platform-specific boundary |
| Position limit enforcement | `RiskManager.check_trade` (existing, tighten) | `PortfolioMonitor` (secondary validation) | Pre-trade check runs synchronously at submission; portfolio monitor is eventually-consistent double-check |
| One-leg exposure detection | `ExecutionEngine._live_execution` post-gather branch (existing) | `SafetySupervisor` (alerting side-effect) | Exposure is detected from `leg_yes.status` + `leg_no.status` combo -- engine is where both are visible atomically |
| API rate limiting | `PlatformAdapter` (already has `rate_limiter` field) | `RateLimiter` acquire before each API call | Token bucket per adapter; each outbound HTTP call must `await rate_limiter.acquire()` first |
| Graceful shutdown orchestration | `arbiter/main.py` signal handler | `SafetySupervisor.prepare_shutdown()` | Signal handling must stay in main; shutdown policy is supervisor's concern |
| Resolution criteria capture | `arbiter/config/settings.py::MARKET_MAP` | Dashboard UI (read + operator review) | Criteria are per-mapping data; UI compares side-by-side |
| Telegram alerting | `TelegramNotifier` (existing, extend) | `SafetySupervisor` emits events | Notifier is already wired; supervisor adds new message templates |
| Dashboard kill-switch UI | `arbiter/web/dashboard.html` + `dashboard.js` (new panel) | `api.py` WebSocket broadcast + REST POST | UI is the primary operator surface; backend exposes events + actions |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `structlog` | 25.5.0 | Structured logging with contextvars (already in Phase 2) | Already installed; kill-switch + rate-limit events log with bound context `[VERIFIED: requirements.txt line 9]` |
| Python stdlib `signal` | - | SIGINT/SIGTERM handling | Built-in; `asyncio.get_event_loop().add_signal_handler()` is the canonical asyncio pattern `[VERIFIED: docs.python.org/3/library/signal.html]` |
| Python stdlib `asyncio` | - | `asyncio.wait_for`, `asyncio.shield`, `Event` for kill-switch coordination | Built-in `[VERIFIED]` |
| `aiohttp` | 3.9.0+ | Reused for Telegram HTTP + REST endpoints (already in use) | Already in `TelegramNotifier._get_session` and `api.py` `[VERIFIED: requirements.txt line 2]` |
| Existing `utils.retry.RateLimiter` | in-repo | Token bucket rate limiter with penalty / retry-after support | Already written, tested, used by collectors -- Phase 3 just wires it into adapters `[VERIFIED: arbiter/utils/retry.py:224-332]` |
| Existing `utils.retry.CircuitBreaker` | in-repo | Per-adapter circuit breaker | Already in main.py:160-171 for Kalshi/Polymarket adapters `[VERIFIED: arbiter/main.py:160]` |
| Existing `monitor.balance.TelegramNotifier` | in-repo | HTML-formatted Telegram sends via Bot API | Already proven working in Phase 2 `[VERIFIED: arbiter/monitor/balance.py:28-72]` |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `aiolimiter` | 1.2.1 (Dec 2024) | Alternative leaky-bucket rate limiter (async context manager) | Only if `utils.retry.RateLimiter` proves insufficient for per-endpoint granularity. NOT recommended -- existing `RateLimiter` is sufficient `[CITED: pypi.org/project/aiolimiter/]` |
| `redis[hiredis]` | 5.0.0+ (already installed) | Kill-switch state persistence across restarts | If `SafetySupervisor` stores armed/disarmed state in Redis, it survives process restart without touching Postgres |
| `asyncpg` | 0.29.0+ (already installed) | Alternative kill-switch persistence via `safety_state` table | If Redis is unavailable in this phase's dev mode, fall back to Postgres |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Extending `TelegramNotifier` | `python-telegram-bot` v20+ (async) | `python-telegram-bot` is heavier (~3MB, lots of deps), designed for bots that respond to commands. For outbound-only alerts, the existing 70-line aiohttp-based `TelegramNotifier` is simpler and already debugged `[CITED: pypi.org/project/python-telegram-bot]` |
| Extending `TelegramNotifier` | `aiogram` 3.x | Same argument -- overkill for one-way alerts. Would make sense if OPT-05 (`/kill` inbound command) lands, but that's v2 `[CITED: aiogram.dev]` |
| Custom `RateLimiter` | `aiolimiter` leaky bucket | Repo's existing token-bucket `RateLimiter` already supports `apply_retry_after`, penalty counters, and `stats` for dashboard. Replacement would mean re-testing collectors. Don't touch what works. |
| New `SafetySupervisor` module | Fold logic into `OperationalReadiness` | `OperationalReadiness` (readiness.py) already does startup-gate + trade-gate. Adding kill-switch makes it the god object. Separation: readiness = "may we trade at all?", supervisor = "may we trade THIS opportunity right now?" |
| Redis for kill-switch state | Postgres table | Redis: sub-ms latency, TTL-based cooldown. Postgres: audit trail. Use **both** -- Redis for live state, Postgres `safety_events` table for audit. |
| `asyncio.Event` for kill coordination | `asyncio.Condition` | Event is simpler, sufficient -- kill is a one-way latch until manually reset |

**Installation:**
```bash
# No new deps -- everything needed is already in requirements.txt from Phase 2
# Confirm present:
pip show structlog tenacity redis aiohttp cryptography asyncpg
```

**Version verification (performed 2026-04-16):**

| Package | Status | Notes |
|---------|--------|-------|
| aiohttp | installed 3.9.0+ | Already in requirements.txt `[VERIFIED]` |
| redis | installed 5.0.0+ | Already in requirements.txt `[VERIFIED]` |
| asyncpg | installed 0.29.0+ | Already in requirements.txt `[VERIFIED]` |
| structlog | installed 25.5.0+ | Installed in Phase 2 `[VERIFIED]` |
| aiolimiter | NOT installed | Not recommended -- repo's RateLimiter suffices `[VERIFIED: import failed]` |
| python-telegram-bot | NOT installed | Not recommended -- existing TelegramNotifier suffices `[VERIFIED: import failed]` |

## Architecture Patterns

### System Architecture Diagram

```
                      SafetySupervisor (new module)
                      ─────────────────────────────
                      ├─ state: armed | disarmed | cooling_down
                      ├─ config: SafetyConfig
                      ├─ subscribers: ws clients, engine gate
                      └─ persistence: Redis (live) + PG (audit)
                             ▲          │
                             │          │
                  trip_kill()│          │allow_execution(opp)
                             │          ▼
       ┌─────────────────────┴───────────────────┐
       │                                         │
       │        ExecutionEngine                  │
       │        ────────────────                 │
       │   execute_opportunity(opp)              │
       │   └─ _check_trade_gate (existing hook)  │◀── SafetySupervisor.allow_execution
       │   └─ RiskManager.check_trade (tightened)│
       │         ├─ per-market limit             │
       │         └─ per-platform limit (NEW)     │
       │   └─ _live_execution                    │
       │         └─ _recover_one_leg_risk        │─── emits one_leg_exposure event
       │                                         │
       └─────────────────────────────────────────┘
                        │
                        ▼
       ┌─────────────────────────────────────────┐
       │  PlatformAdapter (kalshi, polymarket)   │
       │  ─────────────────────────────────       │
       │  ├─ rate_limiter.acquire() (wired NOW)  │
       │  ├─ circuit.can_execute()                │
       │  ├─ place_fok(...) ──▶ Kalshi/Polymarket│
       │  ├─ cancel_order(order)                  │
       │  └─ cancel_all()  ◀── NEW method        │
       └─────────────────────────────────────────┘
                        │
                        ▼
                  [Platform APIs]
                  Kalshi: DELETE /portfolio/orders/batched (20/call)
                  Polymarket: client.cancel_all() (L2 auth)

       ┌─────────────────────────────────────────┐
       │  arbiter/main.py                         │
       │  handle_shutdown(sig)                    │
       │  ──────────────────────                  │
       │  1. Set shutdown_event                   │
       │  2. supervisor.prepare_shutdown()        │◀── NEW: trip kill BEFORE task cancel
       │  3. For each adapter: adapter.cancel_all()
       │  4. Wait ≤5s for cancellations           │
       │  5. Cancel all async tasks               │
       │  6. Close sessions, pool                 │
       └─────────────────────────────────────────┘

       ┌─────────────────────────────────────────┐
       │  Dashboard (arbiter/web + api.py)        │
       │  ─────────────────────                   │
       │  WebSocket events (NEW):                 │
       │  ├─ kill_switch        (state delta)     │
       │  ├─ rate_limit_state   (per-adapter)     │
       │  ├─ one_leg_exposure   (naked position)  │
       │  └─ shutdown_state     (in-progress)     │
       │                                          │
       │  REST (NEW):                             │
       │  ├─ POST /api/kill-switch  (arm/disarm)  │
       │  ├─ GET  /api/safety/status              │
       │  └─ GET  /api/safety/events              │
       │                                          │
       │  UI panels (NEW or extended):            │
       │  ├─ SafetySection (top-right, operator)  │
       │  ├─ RateLimitIndicators (infra panel)    │
       │  ├─ OneLegExposureAlert (hero overlay)   │
       │  └─ MappingResolutionCompare (infra)     │
       └─────────────────────────────────────────┘
```

**Key data flows:**
- **Kill-switch trip:** Dashboard button POST → API → `SafetySupervisor.trip_kill()` → fan out: (a) adapters.cancel_all() in parallel, (b) TelegramNotifier.send_safety_alert, (c) WebSocket broadcast kill_switch event, (d) Redis SET `arbiter:kill_switch=armed`, (e) Postgres INSERT safety_event
- **Pre-trade gate:** Opportunity → engine.execute_opportunity → supervisor.allow_execution → if kill_switch=armed: return (False, "kill switch armed"), else: return RiskManager.check_trade result
- **Rate limiting:** Adapter.place_fok → rate_limiter.acquire() awaits until token → actual HTTP call → on 429 → rate_limiter.apply_retry_after(Retry-After header) → penalty countdown visible on dashboard
- **One-leg detection:** engine._live_execution → gather(yes_task, no_task) → if one filled and one failed → _recover_one_leg_risk → incident emitted → supervisor subscribes to incidents → supervisor.handle_one_leg_exposure → Telegram alert + WebSocket `one_leg_exposure` event + unwind recommendation persisted
- **Graceful shutdown:** SIGINT received → handle_shutdown → shutdown_event.set() → supervisor.prepare_shutdown (trips kill + broadcasts shutdown_state) → `await asyncio.gather(*[adapter.cancel_all() for adapter in adapters.values()], return_exceptions=True)` with `asyncio.wait_for(..., timeout=5.0)` → then cancel background tasks → cleanup sessions

### Recommended Project Structure

```
arbiter/
├── safety/                        # NEW package
│   ├── __init__.py
│   ├── supervisor.py              # SafetySupervisor + SafetyState + SafetyConfig
│   ├── alerts.py                  # Telegram message templates for safety events
│   ├── persistence.py             # Redis + Postgres state sync
│   ├── test_supervisor.py
│   └── test_alerts.py
├── execution/
│   ├── engine.py                  # tighten RiskManager per-platform limits
│   └── adapters/
│       ├── base.py                # add cancel_all() to PlatformAdapter Protocol
│       ├── kalshi.py              # implement cancel_all via batch-cancel endpoint
│       └── polymarket.py          # implement cancel_all via client.cancel_all()
├── main.py                        # restructure handle_shutdown to cancel-before-task-kill
├── api.py                         # add /api/kill-switch + /api/safety/* + new WS events
├── config/
│   └── settings.py                # add SafetyConfig dataclass; extend MARKET_MAP with resolution_criteria
├── sql/                           # (if using PG persistence)
│   └── safety_events.sql          # CREATE TABLE safety_events
└── web/
    ├── dashboard.html             # add <section id="safetySection"> with kill-switch button
    ├── dashboard.js               # new render functions: renderSafetyPanel, renderRateLimitBadges, renderShutdownBanner, renderOneLegAlert
    ├── dashboard-view-model.js    # buildSafetyView(state) helper
    └── styles.css                 # new .kill-switch, .rate-limit-pill, .shutdown-banner, .one-leg-alert styles
```

### Pattern 1: Kill-Switch Trade Gate

**What:** Single choke point for all new order submission that consults `SafetySupervisor.allow_execution` before every trade.

**When to use:** Phase 3 wires this at `ExecutionEngine.execute_opportunity`. Every plan-level gate (Phase 2 readiness, Phase 3 safety) chains through `_check_trade_gate` -- order matters: readiness first (startup blocks), then safety (runtime kill).

**Example:**
```python
# Source: engine.py:327 (existing hook) + new SafetySupervisor
# arbiter/safety/supervisor.py

@dataclass
class SafetyState:
    armed: bool = False                    # True = kill switch ACTIVE, reject all new orders
    armed_by: Optional[str] = None          # "operator:sparx.sandeep@gmail.com" or "auto:daily_loss"
    armed_at: float = 0.0
    armed_reason: str = ""
    cooldown_until: float = 0.0             # 0 = no cooldown, else epoch when resetable
    last_reset_at: float = 0.0
    last_reset_by: str = ""


class SafetySupervisor:
    def __init__(self, config: SafetyConfig, engine, adapters, notifier, redis=None, store=None):
        self.config = config
        self.engine = engine
        self.adapters = adapters
        self.notifier = notifier
        self._redis = redis
        self._store = store  # Postgres ExecutionStore — reused for audit writes
        self._state = SafetyState()
        self._subscribers: list[asyncio.Queue] = []

    async def allow_execution(self, opp) -> tuple[bool, str, dict]:
        """Trade gate — called from ExecutionEngine._check_trade_gate."""
        if self._state.armed:
            return False, f"Kill switch armed: {self._state.armed_reason}", {
                "kill_switch": self._state.armed,
                "armed_by": self._state.armed_by,
                "armed_at": self._state.armed_at,
            }
        return True, "safety supervisor approved", {"kill_switch": False}

    async def trip_kill(self, by: str, reason: str) -> SafetyState:
        """Arm the kill switch — cancels ALL open orders across adapters, alerts, persists."""
        now = time.time()
        self._state = SafetyState(
            armed=True, armed_by=by, armed_at=now,
            armed_reason=reason,
            cooldown_until=now + self.config.min_cooldown_seconds,
        )
        # 1. Fan out cancel_all across adapters in parallel (≤5s per SAFE-01)
        async def _cancel_platform(platform: str, adapter):
            try:
                return platform, await asyncio.wait_for(adapter.cancel_all(), timeout=5.0)
            except Exception as exc:
                logger.error("Kill cancel failed platform=%s err=%s", platform, exc)
                return platform, []
        results = await asyncio.gather(*[
            _cancel_platform(p, a) for p, a in self.adapters.items()
        ], return_exceptions=True)
        # 2. Telegram alert
        await self.notifier.send(
            f"🛑 <b>KILL SWITCH ARMED</b>\n"
            f"By: {by}\nReason: {reason}\n"
            f"Cancelled orders: {sum(len(r[1]) for r in results if isinstance(r, tuple))}\n"
            f"Manual reset required.",
        )
        # 3. Redis live state
        if self._redis:
            await self._redis.set("arbiter:kill_switch", "armed", ex=None)
        # 4. Postgres audit
        if self._store:
            await self._store.insert_safety_event(by=by, reason=reason, state=self._state, results=results)
        # 5. Broadcast to subscribers
        await self._publish({"type": "kill_switch", "payload": self._state_dict()})
        return self._state

    async def reset_kill(self, by: str, note: str) -> SafetyState:
        now = time.time()
        if now < self._state.cooldown_until:
            remaining = self._state.cooldown_until - now
            raise ValueError(f"Kill switch cooldown: {remaining:.1f}s remaining")
        self._state = SafetyState(last_reset_at=now, last_reset_by=by)
        if self._redis:
            await self._redis.delete("arbiter:kill_switch")
        await self.notifier.send(f"🟢 <b>Kill switch RESET</b>\nBy: {by}\nNote: {note}")
        await self._publish({"type": "kill_switch", "payload": self._state_dict()})
        return self._state
```

**Wiring in main.py:**
```python
# arbiter/main.py (new lines around line 210)
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
    safety_config, engine, adapters, monitor.notifier,
    redis=None,  # or redis client if REDIS_URL set
    store=store,
)

# REPLACE readiness-only trade gate with chained gate:
async def chained_gate(opp):
    ok, reason, ctx = await readiness.allow_execution(opp) if asyncio.iscoroutinefunction(readiness.allow_execution) else readiness.allow_execution(opp)
    if not ok:
        return (False, reason, ctx)
    return await safety.allow_execution(opp)

engine.set_trade_gate(chained_gate)
```

### Pattern 2: Per-Platform Rate Limiting at Adapter Layer

**What:** Every adapter outbound call `await rate_limiter.acquire()` before the HTTP request, and on 429 response calls `apply_retry_after(retry_after_header, fallback=exponential)`.

**When to use:** Every adapter method: `place_fok`, `cancel_order`, `get_order`, `list_open_orders_by_client_id`, `cancel_all`, `check_depth`.

**Example:**
```python
# Source: arbiter/execution/adapters/kalshi.py (existing pattern, extend)
# rate_limiter already constructed in main.py:163 as kalshi_rate_limiter

class KalshiAdapter:
    async def place_fok(self, arb_id, market_id, canonical_id, side, price, qty) -> Order:
        await self.rate_limiter.acquire()  # ← MUST be first thing
        if not self.circuit.can_execute():
            return Order(..., status=OrderStatus.FAILED, error="circuit open")
        try:
            async with self.session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "1")
                    delay = self.rate_limiter.apply_retry_after(
                        retry_after, fallback_delay=2.0, reason="kalshi_429",
                    )
                    logger.warning("Kalshi rate-limited, penalty=%.1fs", delay)
                    # FOK semantics: do NOT retry — return FAILED + mark incident
                    self.circuit.record_failure()
                    return Order(..., status=OrderStatus.FAILED, error=f"rate_limited ({delay:.1f}s)")
                ...
```

### Pattern 3: Graceful Shutdown Ordering (cancel orders BEFORE tasks)

**What:** Signal handler sets event; event handler triggers kill-switch trip FIRST, awaits adapter.cancel_all() with timeout, THEN cancels background tasks.

**When to use:** Every time the process shuts down -- this is the fail-safe for SAFE-05.

**Example:**
```python
# Source: arbiter/main.py:297-312 (existing, needs restructuring)

shutdown_event = asyncio.Event()
shutting_down = False

def handle_shutdown(sig):
    nonlocal shutting_down
    if shutting_down:
        logger.warning(f"Received {sig.name} again, forcing immediate exit")
        os._exit(1)  # second signal = hard exit
    shutting_down = True
    logger.info(f"Received {sig.name}, shutting down...")
    shutdown_event.set()

for sig in (signal.SIGINT, signal.SIGTERM):
    asyncio.get_event_loop().add_signal_handler(sig, handle_shutdown, sig)

await shutdown_event.wait()

# ─── NEW: cancel orders BEFORE cancelling tasks ─────────────────────
logger.info("Tripping safety kill-switch for graceful shutdown...")
try:
    await asyncio.wait_for(
        safety.trip_kill(by="system:shutdown", reason="Process shutdown signal"),
        timeout=5.0,
    )
except asyncio.TimeoutError:
    logger.error("Kill-switch trip exceeded 5s — some orders may remain open")

# Now cancel tasks (old code)
for task in tasks:
    task.cancel()
await asyncio.gather(*tasks, return_exceptions=True)
# ... rest of cleanup
```

### Pattern 4: One-Leg Exposure Surfacing

**What:** When `_recover_one_leg_risk` detects a filled leg + failed leg, emit a distinct `one_leg_exposure` event (not just a generic incident) with explicit unwind recommendation.

**When to use:** Inside `ExecutionEngine._live_execution` after `asyncio.gather(yes_task, no_task)` -- the code branch for "one filled, one failed" already exists in engine.py:668-681, just needs to surface a richer signal.

**Example:**
```python
# Source: arbiter/execution/engine.py:852 (existing _recover_one_leg_risk, extend)

async def _recover_one_leg_risk(self, arb_id, opp, leg_yes, leg_no) -> list[str]:
    # Classify exposure direction
    filled_leg = leg_yes if leg_yes.status == OrderStatus.FILLED else leg_no
    failed_leg = leg_no if leg_yes.status == OrderStatus.FILLED else leg_yes
    exposure_usd = filled_leg.fill_qty * filled_leg.fill_price

    # Existing: log incident
    incident = await self._record_incident(
        arb_id, opp, "critical",
        "One-leg exposure detected — naked position requires unwind",
        metadata={
            "event_type": "one_leg_exposure",   # ← NEW structured type
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

    # NEW: safety supervisor handles the operator-facing side
    if self._safety:
        await self._safety.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)

    # Existing: attempt to cancel the still-open side
    ...
```

### Pattern 5: Resolution Criteria in MARKET_MAP

**What:** Each mapping gets `resolution_criteria: {kalshi: {source, rule}, polymarket: {source, rule}}` captured as free text + structured. Operator reviews side-by-side before confirming.

**When to use:** Every time a new candidate mapping is scored. Auto-populate from collector metadata if available; fall back to operator-filled fields.

**Example:**
```python
# Source: arbiter/config/settings.py::MARKET_MAP (extend schema)

MARKET_MAP = {
    "DEM_PRESIDENT_2028": {
        "description": "Will a Democrat win the 2028 US presidency?",
        "status": "candidate",
        "kalshi": {"market_id": "KXPRESPARTY-2028", "ticker": "DEM"},
        "polymarket": {"market_id": "PM-PRES-2028-DEM"},
        "resolution_criteria": {                           # ← NEW
            "kalshi": {
                "source": "https://kalshi.com/markets/KXPRESPARTY-2028",
                "rule": "Resolves YES if the Democratic candidate wins 270+ electoral votes as certified by Congress on Jan 6, 2029",
                "settlement_date": "2029-01-06",
            },
            "polymarket": {
                "source": "https://polymarket.com/event/...",
                "rule": "Resolves YES if the Democratic candidate is inaugurated on Jan 20, 2029",
                "settlement_date": "2029-01-20",
            },
            "criteria_match": "pending_operator_review",   # operator sets: identical | similar | divergent
            "operator_note": "",
        },
        "allow_auto_trade": False,
    },
}
```

### Anti-Patterns to Avoid

- **Kill switch as a Python boolean on some object:** Dies on restart. Use Redis (live) + Postgres (audit) for persistence. If Redis is unavailable, at least log the trip to Postgres so restarts can reconstruct state.
- **Cancelling tasks before cancelling orders in shutdown:** Once you cancel the adapter-owning task, you lose the ability to call its `cancel_all()`. Always do orders first, then tasks.
- **Per-market limit check that forgets cross-market aggregation:** Current `RiskManager.check_trade` checks `existing + exposure > max_position_usd` per canonical_id, but does NOT sum across platforms. Phase 3 must add `sum(platform_exposures[platform]) > max_platform_exposure` check.
- **Using `asyncio.shield` around `trip_kill`:** Don't -- if the caller (shutdown handler) is cancelled, you WANT the kill trip to also cancel, because the process is about to die anyway. Use `wait_for(..., timeout=5.0)` instead.
- **Silencing TelegramNotifier failures in kill-switch path:** If Telegram send fails, the alert should still land on dashboard + logs. Don't gate the kill trip on Telegram success.
- **Retrying a rate-limited FOK order:** FOK + retry = potential duplicate fill. On 429 from a place_fok, mark the leg FAILED and let one-leg recovery handle it. NEVER retry a POST /orders after 429.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Token-bucket rate limiter | Custom counter + timestamps | `utils.retry.RateLimiter` (already tested) | Repo has it; `aiolimiter` is alternative if needed, but don't reinvent |
| Telegram HTTP client | Raw `requests`/`aiohttp` with auth headers | `monitor.balance.TelegramNotifier` | 70 LOC already works, supports HTML, handles errors |
| Circuit breaker | Custom state machine | `utils.retry.CircuitBreaker` | Already instantiated per-adapter in main.py |
| Batch order cancellation on Kalshi | Loop calling `cancel_order` one at a time | Kalshi `DELETE /portfolio/orders/batched` (up to 20/call) | Single API call cancels 20 orders; fewer round-trips under shutdown pressure |
| Batch order cancellation on Polymarket | Iterate over open orders | `client.cancel_all()` L2 endpoint | One SDK call cancels everything |
| Signal handler wiring | `signal.signal(SIGINT, handler)` | `loop.add_signal_handler(sig, handler, sig)` | Signal-safe in asyncio; stdlib `signal.signal` doesn't interrupt `asyncio.sleep` |
| JSON-over-WebSocket event fanout | Custom dispatcher | Existing `_broadcast_loop` + `_ws_clients` in api.py | Works, just add new event types |
| Kill-switch state persistence | Files or custom serialization | Redis SET/GET for live + Postgres `safety_events` for audit | Battle-tested, survives restarts |
| Retry-after parsing | Manual Retry-After header parse | `RateLimiter.apply_retry_after(header)` (existing) | Already handles both numeric seconds and HTTP-date formats |

**Key insight:** Phase 3 is nearly 100% wiring, not invention. Every safety primitive the phase needs already exists in the codebase; this phase is about CONNECTING them and SURFACING them to the operator via the dashboard.

## Runtime State Inventory

Phase 3 introduces NEW runtime state that survives restart. Enumerate it explicitly:

| Category | Items Introduced | Action Required |
|----------|------------------|------------------|
| Stored data | Redis: `arbiter:kill_switch` key (live state); Postgres: new `safety_events` table (audit trail of every trip/reset) | New Redis client wiring in main.py; new SQL migration for safety_events table |
| Live service config | None — kill-switch is OPERATED from dashboard, not configured externally | None |
| OS-registered state | None — no new systemd/Task Scheduler entries; graceful shutdown uses existing SIGINT/SIGTERM | None |
| Secrets/env vars | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` already in .env; no new secrets for Phase 3 | Verify .env has both set before live testing |
| Build artifacts | None — pure-Python additions, no compiled artifacts | None |

**Nothing found in category:** The OS-registered state, live service config, and build artifacts categories have no new Phase 3 items — stated explicitly so the planner knows not to chase them.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| aiohttp | TelegramNotifier, api.py, adapters | ✓ | 3.9.0+ | — |
| redis-py | SafetySupervisor live state | ✓ (installed) | 5.0.0+ | Postgres-only persistence |
| asyncpg | safety_events audit table | ✓ | 0.29.0+ | In-memory only (phase dev) |
| Telegram Bot API | Alerts | ✓ (HTTP endpoint) | — | Log-only alerts if token absent (existing behavior in TelegramNotifier) |
| Kalshi batch-cancel endpoint | Kill-switch mass cancel | ✓ (docs confirm available) | — | Loop single cancels (slower) |
| Polymarket client.cancel_all | Kill-switch mass cancel | ✓ (py-clob-client 0.34.x) | 0.34.6 | Loop single cancels |
| POSIX signals | SIGINT/SIGTERM handling | ✓ | — | Windows: signal handling via asyncio on 3.12+ works, verified |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback:** Redis (falls back to Postgres-only state persistence — kill switch still works, but loses sub-ms reads; acceptable for small-capital phase).

## Common Pitfalls

### Pitfall 1: Kill switch armed but new orders still slip through

**What goes wrong:** Operator hits kill, dashboard confirms armed, but an order that was mid-flight (in `execute_opportunity` past the gate but before `_place_order_for_leg`) still submits.

**Why it happens:** Race condition -- gate is checked once at the top of `execute_opportunity`, but `asyncio.gather(yes_task, no_task)` schedules the adapter calls several statements later. By the time adapters run, the supervisor state may have flipped.

**How to avoid:**
1. `trip_kill()` must explicitly **also** call `adapter.cancel_all()` -- this cancels the mid-flight orders immediately after they're accepted by the exchange.
2. Additionally, check supervisor state inside `_place_order_for_leg` as a second gate (cheap: one attribute read).
3. Accept that some orders will reach the exchange before cancel arrives; that's what FOK + quick cancel_all covers.

**Warning signs:** Dashboard kill-switch shows "armed" while new executions appear in the trade list.

### Pitfall 2: Kalshi batch-cancel pagination

**What goes wrong:** Operator has 30 open orders; batch-cancel takes 20 at a time; second batch fails silently and 10 orders remain.

**Why it happens:** Kalshi limits batch-cancel to 20 orders per call. Simple mistake to assume `cancel_all()` is one HTTP call.

**How to avoid:** In `KalshiAdapter.cancel_all()`, list all open orders, chunk into 20-sized slices, call DELETE `/portfolio/orders/batched` per chunk, aggregate results. Respect rate limits between chunks (use `rate_limiter.acquire()`).

**Warning signs:** TelegramNotifier alert reports "Cancelled X orders" but portfolio dashboard still shows open orders after the trip.

### Pitfall 3: SIGTERM/SIGINT while a single order is mid-POST to exchange

**What goes wrong:** POST /orders is in flight when signal arrives. Signal handler cancels tasks, which cancels the aiohttp session mid-request. The order may or may not have been accepted -- we don't know.

**Why it happens:** Cancelling a task that's awaiting `session.post` raises CancelledError inside the coroutine. The server may have already received and accepted the request.

**How to avoid:**
1. Shutdown ordering: trip kill + cancel_all FIRST (5s window), THEN cancel tasks. By the time tasks die, mid-flight POSTs have been cancelled by the exchange (if the POST arrived).
2. Startup reconciliation (already wired in Phase 2 via `reconcile_non_terminal_orders`) catches any orphaned orders: on restart, any order marked PENDING/SUBMITTED in DB that isn't in the exchange's open orders is closed out as orphaned.

**Warning signs:** Restart-recovery incidents emitted that reference orders from before the shutdown. Existing handling is correct; just verify it still runs.

### Pitfall 4: One-leg exposure alert drowned in incident queue

**What goes wrong:** A naked position happens; it emits an incident; the incident queue already has 5 other incidents; operator doesn't spot it in time; market moves; loss realized.

**Why it happens:** Incidents are treated uniformly in UI; no escalation path for the one case where the clock is ticking.

**How to avoid:**
1. New WebSocket event type `one_leg_exposure` (separate from `incident`) triggers a hero-level overlay banner on the dashboard that MUST be acknowledged.
2. Telegram alert with `🚨 <b>NAKED POSITION</b>` formatting and explicit unwind instructions.
3. Audible-style visual treatment (pulsing border) on the incident card.

**Warning signs:** Operator discovers a naked position from portfolio view, not from alert -- means the alerting path failed.

### Pitfall 5: Rate limiter starves under shutdown batch-cancel

**What goes wrong:** Kill-switch trip tries to batch-cancel via Kalshi. Rate limiter acquire blocks. 5s timeout on `trip_kill` expires. Some orders remain open.

**Why it happens:** Shutdown under load has lots of in-flight requests competing with the cancel. Token bucket runs dry.

**How to avoid:**
1. Under `trip_kill`, bypass the token bucket (`adapter.cancel_all(force=True)` or use a dedicated higher-priority limiter).
2. Prioritize batch-cancel calls over everything else in the shutdown window.
3. Set rate limiter budget >= expected batch-cancel calls (e.g., if max 100 open orders, need 5 batch-cancel calls = 5 tokens minimum, which fits in 1s at 10 writes/sec).

**Warning signs:** `timeout_recovery.lookup_failed` logs during shutdown, or TelegramNotifier reports "Cancelled 0 orders".

### Pitfall 6: Resolution criteria schema churn

**What goes wrong:** MARKET_MAP schema extended with `resolution_criteria`; existing fixture data and tests break.

**Why it happens:** MARKET_MAP is used in scanner, API, tests, auditor -- every consumer does `mapping.get("status")` etc. Adding a new required key would explode.

**How to avoid:**
1. Make `resolution_criteria` optional (defaults to `None` or empty dict).
2. Dashboard treats missing criteria as "pending -- operator must fill before confirm".
3. Unit-test the schema-optional behavior explicitly.

**Warning signs:** Any `KeyError: 'resolution_criteria'` in tests or runtime.

## Code Examples

### Kalshi batch cancel implementation

```python
# Source: docs.kalshi.com/api-reference/orders/batch-cancel-orders
# arbiter/execution/adapters/kalshi.py (new method)

async def cancel_all(self) -> list[str]:
    """Cancel all open orders on Kalshi (up to 20 per batch call).

    Returns list of cancelled order_ids.
    """
    # 1. List all open orders
    open_orders = await self._list_all_open_orders()
    if not open_orders:
        return []

    cancelled_ids: list[str] = []
    # 2. Chunk into batches of 20
    CHUNK_SIZE = 20
    for i in range(0, len(open_orders), CHUNK_SIZE):
        chunk = open_orders[i:i + CHUNK_SIZE]
        await self.rate_limiter.acquire()
        headers = self._auth_headers("DELETE", "/trade-api/v2/portfolio/orders/batched")
        payload = {"ids": [o.order_id for o in chunk]}
        try:
            async with self.session.delete(
                f"{self.base_url}/trade-api/v2/portfolio/orders/batched",
                json=payload, headers=headers,
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    for entry in body:
                        if not entry.get("error"):
                            cancelled_ids.append(entry["order_id"])
                else:
                    text = await resp.text()
                    logger.warning("Kalshi batch-cancel %d failed: %s", resp.status, text[:200])
        except Exception as exc:
            logger.error("Kalshi batch-cancel raised: %s", exc)
    return cancelled_ids
```

### Polymarket cancel_all implementation

```python
# Source: py-clob-client 0.34.x + docs.polymarket.com/developers/CLOB/orders/cancel-orders
# arbiter/execution/adapters/polymarket.py (new method)

async def cancel_all(self) -> list[str]:
    """Cancel all open orders on Polymarket via CLOB client cancel_all().

    Returns list of cancelled order_ids.
    """
    client = self.clob_client_factory()
    if client is None:
        return []
    await self.rate_limiter.acquire()
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.cancel_all()
        )
        # cancel_all returns a dict with "canceled" and "not_canceled" keys
        if isinstance(result, dict):
            return list(result.get("canceled", []))
        return []
    except Exception as exc:
        logger.error("Polymarket cancel_all raised: %s", exc)
        return []
```

### Dashboard kill-switch button (HTML + JS wiring)

```html
<!-- arbiter/web/dashboard.html — NEW section, insert before commandCenter -->
<section id="safetySection" class="safety-section ops-only" data-ops-only="true" aria-label="Safety controls">
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
      <button id="killSwitchArm" type="button" class="btn btn-danger kill-switch-arm">ARM KILL SWITCH</button>
      <button id="killSwitchReset" type="button" class="btn btn-secondary kill-switch-reset hidden">Reset kill switch</button>
      <span id="killSwitchCooldown" class="kill-switch-cooldown hidden">Cooldown: 00:30</span>
    </div>
    <div id="rateLimitIndicators" class="rate-limit-grid"></div>
  </article>
</section>
```

```javascript
// arbiter/web/dashboard.js — NEW handler + render block

// In the WebSocket message handler (add to the else-if chain around line 1067):
} else if (message.type === "kill_switch") {
  state.safety = { ...(state.safety || {}), killSwitch: message.payload };
} else if (message.type === "rate_limit_state") {
  state.safety = { ...(state.safety || {}), rateLimits: message.payload };
} else if (message.type === "one_leg_exposure") {
  state.oneLegExposures = [message.payload, ...(state.oneLegExposures || [])].slice(0, 8);
} else if (message.type === "shutdown_state") {
  state.shutdown = message.payload;
}

// New render function
function renderSafetyPanel() {
  const ks = state.safety?.killSwitch || { armed: false };
  const badge = document.getElementById("killSwitchBadge");
  const summary = document.getElementById("killSwitchSummary");
  const armBtn = document.getElementById("killSwitchArm");
  const resetBtn = document.getElementById("killSwitchReset");
  if (!badge) return;
  if (ks.armed) {
    badge.textContent = "ARMED";
    badge.className = "panel-badge status-badge status-critical";
    summary.textContent = `Armed by ${ks.armed_by || "unknown"} at ${new Date((ks.armed_at||0)*1000).toLocaleTimeString()}. Reason: ${ks.armed_reason || "n/a"}`;
    armBtn.classList.add("hidden");
    resetBtn.classList.remove("hidden");
  } else {
    badge.textContent = "Disarmed";
    badge.className = "panel-badge status-badge status-ok";
    armBtn.classList.remove("hidden");
    resetBtn.classList.add("hidden");
  }
  renderRateLimitBadges();
}

// New click handler (add inside the document click listener)
const killArm = event.target.closest("#killSwitchArm");
if (killArm) {
  if (!hasOperatorAccess()) { showAuthOverlay("Sign in to arm kill switch."); return; }
  if (!confirm("ARM the kill switch? This will cancel ALL open orders and halt new execution.")) return;
  void runAction(killArm, () => postJson("/api/kill-switch", { action: "arm", reason: "Operator manual" }));
  return;
}
const killReset = event.target.closest("#killSwitchReset");
if (killReset) {
  if (!hasOperatorAccess()) { showAuthOverlay("Sign in to reset kill switch."); return; }
  if (!confirm("Reset the kill switch? New orders will resume immediately.")) return;
  void runAction(killReset, () => postJson("/api/kill-switch", { action: "reset", note: "Operator reset" }));
  return;
}
```

## Current UI State vs. Safety Layer Needs

This section is load-bearing per the user directive. It audits the existing dashboard UI against Phase 3 safety requirements.

### What exists today (inventory)

| Surface | Location | Purpose |
|---------|----------|---------|
| Sign-in overlay | `dashboard.html:16-37`, `dashboard.js:authForm*` | HMAC session auth for operator mode |
| Top nav + mode tag (Public/Ops desk) | `dashboard.html:58-76` | Indicates whether operator is authenticated |
| Hero overview (equity, delta, risk score bar) | `dashboard.html:79-175`, `dashboard-view-model.js:102` | Big-picture P&L + risk-posture summary |
| Command center + metric cards | `dashboard.html:178-189`, `dashboard-view-model.js:160` | Realized P&L, Open exposure, Validator state, Execution flow |
| Scanner section (edge chart + opportunity blotter) | `dashboard.html:191-228`, `dashboard.js:renderOpportunities` | Live trade candidates, filter pills, edge/freshness/liquidity |
| Risk section (Profitability verdict + Portfolio exposure list) | `dashboard.html:230-254`, `renderPortfolioPanel` | Exposure and violations from PortfolioMonitor |
| Ops section (Manual queue + Incident queue) | `dashboard.html:256-280`, `renderManualQueue`, `renderIncidentQueue` | PredictIt operator queue + incident resolution |
| Activity Atlas logs section | `dashboard.html:282-339`, `activity-atlas-model.js` | Filterable timeline of all system events (quotes, opps, executions, incidents) |
| Infra section (Mapping + Collectors) | `dashboard.html:341-363` | Market mappings list with confirm/review actions + collector health rail |
| WebSocket event types supported | `api.py:627-637`, `dashboard.js:1046-1072` | `bootstrap`, `system`, `quote`, `opportunity`, `execution`, `incident`, `heartbeat` |
| REST endpoints | `api.py:181-207` | `/api/system`, `/api/opportunities`, `/api/trades`, `/api/errors`, `/api/manual-positions`, `/api/market-mappings`, `/api/portfolio/*`, `/api/auth/*` |
| Operator action POST pattern | `dashboard.js:1984-2021` + `api.py:357-385` | Manual queue actions, incident resolve, mapping confirm/review -- all operator-gated |

### Gaps for Phase 3 safety needs

#### 1. Kill switch (SAFE-01)

| Concern | Current State | Gap |
|---------|---------------|-----|
| Kill switch button | **DOES NOT EXIST** | No UI surface to arm or reset |
| Armed/disarmed visual state | **DOES NOT EXIST** | No indication anywhere on dashboard |
| Cooldown timer | **DOES NOT EXIST** | No indicator that reset is blocked |
| Programmatic threshold indicator | **DOES NOT EXIST** | No UI for auto-kill triggers (even though auto is deferred to v2) |
| Confirmation modal before arming | **DOES NOT EXIST** | Using `window.confirm` is acceptable for v1; a branded modal is v2 |
| Audit trail of past trips | **DOES NOT EXIST** | No view of "kill switch was armed 3 times today" |
| Telegram confirmation | **Partially:** existing `TelegramNotifier.send()` works | Needs new message template for kill events |

**Phase 3 action:** Add `<section id="safetySection">` (top of dashboard, above command center, operator-only). Panel with status badge, ARM button (destructive red), RESET button (secondary, hidden until armed), cooldown timer. New WebSocket event `kill_switch` keeps state in sync.

#### 2. Position-limit breach warnings (SAFE-02)

| Concern | Current State | Gap |
|---------|---------------|-----|
| Per-market limit violations | **Partially:** PortfolioMonitor computes `per-market` violations (`monitor.py:390-404`); surfaced in Risk section via `renderPortfolioPanel` | These are **post-hoc** -- fired after positions exist. Phase 3 needs **pre-trade rejection visibility** |
| Per-platform limit warnings | **PARTIAL:** `per-venue` limit exists in PortfolioConfig but no platform aggregation in RiskManager | RiskManager.check_trade only checks per-market + total; platform aggregation needs wiring |
| Rejected-order log | **Sort of:** rejected trades emit generic debug log in `execute_opportunity` | No operator-facing list of "orders blocked by safety" |

**Phase 3 action:** When RiskManager rejects an order, emit a new `order_rejected` incident type with reason; new `renderRejectedOrders` component inside Risk section shows "Last 10 blocked attempts" with platform, market, reason.

#### 3. One-leg exposure alerts (SAFE-03)

| Concern | Current State | Gap |
|---------|---------------|-----|
| Detection | Exists in `_recover_one_leg_risk` | Emits generic `critical` incident -- buried in incident queue |
| Operator awareness | Relies on operator scanning incident queue | No hero-level callout |
| Unwind recommendation surfaced | `/api/portfolio/unwind/{position_id}` exists (`api.py:486-566`) + WorkflowManager integration | But only triggered manually -- no "here's the recommended action" popup |
| Audio/animation cue | None | Naked position is time-sensitive; needs visual distinction |

**Phase 3 action:** New `one_leg_exposure` WebSocket event. Hero-level `<div id="oneLegAlert" class="alert-banner alert-critical">` banner overlay on the dashboard that announces the naked position + recommended unwind action, with an "Acknowledge" button that also triggers `/api/portfolio/unwind/...`. Pulsing border + subtle shake animation.

#### 4. Rate-limit health indicators (SAFE-04)

| Concern | Current State | Gap |
|---------|---------------|-----|
| Collector rate-limit state | **EXISTS IN STATE:** `state.system.collectors` includes `rate_limiter.remaining_penalty_seconds` (`dashboard-view-model.js:60`) used for risk score | Raw value is in state but NOT rendered as a visible indicator |
| Execution adapter rate-limit state | **DOES NOT EXIST** | Adapter RateLimiter stats never flow to UI |
| Throttle visual (pulse / color) | **DOES NOT EXIST** | Nothing shows "Kalshi adapter is rate-limited right now" |
| Per-platform cap config visibility | **DOES NOT EXIST** | Operator doesn't know what the limit is |

**Phase 3 action:** New `rate_limit_state` WebSocket event (adapter name, available_tokens, max, remaining_penalty_seconds). New per-platform pill in Infra section OR Safety section: `Kalshi: 8/10 writes/sec (cooldown 2.3s)`. Pill turns amber at `remaining_penalty_seconds > 0`, red at circuit OPEN.

#### 5. Shutdown countdown/status (SAFE-05)

| Concern | Current State | Gap |
|---------|---------------|-----|
| Shutdown indicator | **DOES NOT EXIST** | Dashboard has no idea the server is shutting down |
| In-progress cancellation count | **DOES NOT EXIST** | Operator can't see "cancelling 12 orders, 3s left" |
| Connection loss handling | Exists via WebSocket close handler (`dashboard.js:1076-1081`) | Labels as "Polling/Reconnecting" -- doesn't distinguish planned shutdown from network blip |

**Phase 3 action:** New `shutdown_state` WebSocket event broadcast as soon as SIGINT received. Top-banner red bar: `🛑 Server shutting down — cancelling 12 orders, 4.8s remaining`. If WebSocket closes after receiving shutdown_state, label shows "Server shutdown complete" instead of "Reconnecting".

#### 6. Resolution criteria comparison view (SAFE-06)

| Concern | Current State | Gap |
|---------|---------------|-----|
| Mapping confirm/review actions | Exists: `handle_market_mapping_action` + `renderActionButton` | No side-by-side comparison before confirm |
| Resolution-rule fields | **DOES NOT EXIST** in MARKET_MAP | Schema extension needed |
| Divergence warning | **DOES NOT EXIST** | Operator can confirm a mapping where resolution rules differ, without being warned |

**Phase 3 action:** MARKET_MAP schema gets `resolution_criteria: {kalshi: {source, rule, settlement_date}, polymarket: {...}, criteria_match: "identical|similar|divergent|pending_review", operator_note: ""}`. Mapping card in Infra section expands to show side-by-side rule comparison + dropdown for operator to tag match status. "Confirm" button disabled until operator tags.

### Live-trade operational gaps (beyond Phase 3 specifics)

These are NOT blocking Phase 3 but worth flagging. Mark for Phase 3 (in scope) vs. future (deferred).

| Gap | Severity | Phase |
|-----|----------|-------|
| No confirmation modal for order submission in live mode | HIGH | Phase 3 (cheap, high-safety) |
| No "last trade time" indicator in hero | MEDIUM | Phase 3 (1-liner in hero) |
| No audit trail visibility (safety_events table content) | HIGH | Phase 3 (new `GET /api/safety/events` + panel) |
| No emergency reset flow documentation | MEDIUM | Phase 3 (tooltip on kill-switch panel) |
| No readiness checklist visual (what's blocking live trading?) | MEDIUM | `OperationalReadiness` has the data; need UI surface | Defer to Phase 4 |
| No platform status check (API health from Kalshi/Polymarket status pages) | LOW | Defer to Phase 4 |
| No P&L sparkline per-trade | LOW | Defer to v2 |
| No Telegram test-button from UI | MEDIUM | Phase 3 (quick win, verifies alerting path) |
| No "Simulate kill switch in dry-run" mode | LOW | Defer to v2 |

### Backend API changes required

| Change | File | Scope |
|--------|------|-------|
| `POST /api/kill-switch` (body: `{action: "arm"|"reset", reason: str, note: str}`) | `arbiter/api.py` | New route; operator-gated |
| `GET /api/safety/status` | `arbiter/api.py` | Returns current SafetyState + rate-limit stats + last events |
| `GET /api/safety/events` | `arbiter/api.py` | Paginated history from `safety_events` Postgres table |
| `POST /api/test-telegram` | `arbiter/api.py` | Operator-triggered test alert (quick-win) |
| New WS event: `kill_switch` | `arbiter/api.py::_broadcast_loop` | Fanout from `SafetySupervisor._publish` |
| New WS event: `rate_limit_state` | `arbiter/api.py::_broadcast_loop` | Periodic (every 2s) from adapter stats |
| New WS event: `one_leg_exposure` | `arbiter/api.py::_broadcast_loop` | From incident subscribe -- when `metadata.event_type == "one_leg_exposure"` |
| New WS event: `shutdown_state` | `arbiter/api.py::_broadcast_loop` | From SafetySupervisor on shutdown |
| Extend `/api/market-mappings/{id}` | `arbiter/api.py::handle_market_mapping_action` | Accept `resolution_criteria` body for capture |
| Extend `_build_system_snapshot` | `arbiter/api.py:650` | Include `safety`, `rate_limits` in state.system |

### Frontend changes required

| Change | File | Scope |
|--------|------|-------|
| New `<section id="safetySection">` with kill-switch + rate-limit + one-leg-alert | `arbiter/web/dashboard.html` | ~60 lines HTML + CSS |
| New render functions: `renderSafetyPanel`, `renderRateLimitBadges`, `renderShutdownBanner`, `renderOneLegAlert` | `arbiter/web/dashboard.js` | ~200 LOC |
| New WS event handlers: `kill_switch`, `rate_limit_state`, `one_leg_exposure`, `shutdown_state` | `arbiter/web/dashboard.js:1046-1072` | ~20 LOC |
| New click handlers: `#killSwitchArm`, `#killSwitchReset`, `#testTelegram` | `arbiter/web/dashboard.js:document.addEventListener("click")` | ~30 LOC |
| Extend mapping panel for resolution criteria comparison | `arbiter/web/dashboard.js::renderMappings` | ~50 LOC |
| New view-model helpers: `buildSafetyView`, `buildRateLimitView` | `arbiter/web/dashboard-view-model.js` | ~40 LOC + unit tests |
| Styles for safety section: `.kill-switch-*`, `.rate-limit-pill`, `.shutdown-banner`, `.one-leg-alert` | `arbiter/web/styles.css` | ~80 LOC |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `signal.signal(SIGINT, handler)` | `loop.add_signal_handler(sig, handler, sig)` | Python 3.4+ | Old pattern doesn't interrupt `asyncio.sleep` -- must use asyncio variant for async cleanup |
| Manual Retry-After parsing | `rfc2822`/`email.utils.parsedate_to_datetime` | Python 3.0+ | Handles both numeric seconds and HTTP-date formats |
| Single Kalshi cancel endpoint looped | `DELETE /portfolio/orders/batched` (up to 20 per call) | Kalshi API v2 (2025+) | 20x fewer round-trips during shutdown |
| Polymarket per-order cancel | `client.cancel_all()` (L2) | py-clob-client 0.34.x | One SDK call cancels all open orders |
| Redis SETNX for kill-state coordination | Redis `SET key value NX EX ttl` | Redis 2.6.12+ | Atomic set-if-not-exists with TTL in one command |
| Hardcoded rate-limit config | Per-platform config in `SafetyConfig` dataclass | N/A (this phase) | Operator can tune without code change |

**Deprecated/outdated:**
- `asyncio.get_event_loop().add_signal_handler()` (deprecated-ish in 3.12): use `asyncio.get_running_loop().add_signal_handler()` inside async context. Existing main.py uses `asyncio.get_event_loop()` -- works but emits DeprecationWarning in 3.12+. Fix opportunity in Phase 3.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `python-telegram-bot`/`aiogram` overkill for outbound-only alerts | Standard Stack - Alternatives Considered | Low -- can switch later if OPT-05 (`/kill` inbound) arrives |
| A2 | Redis adds value over Postgres-only for kill-switch state | Standard Stack - Supporting | Medium -- if Redis not available in phase dev, Postgres-only is fine |
| A3 | 5-second timeout on `trip_kill().cancel_all()` is enough for ≤20 open orders per venue | Pattern 3 | Medium -- if operator has 100+ open orders during shutdown, some may remain; mitigated by startup reconciliation |
| A4 | Operator wants to type `confirm()` before arming kill (not a branded modal) | Pattern 4 + UI | Low -- `window.confirm` is acceptable v1; branded modal is polish |
| A5 | Existing `TelegramNotifier` passes through Phase 3 load without refactor | Don't Hand-Roll | Low -- already proven in Phase 2 |
| A6 | Polymarket `client.cancel_all()` returns `{canceled, not_canceled}` | Code Examples | Medium -- verified via docs; actual SDK return shape needs runtime confirmation |
| A7 | Kalshi batch-cancel DELETE endpoint available on current account tier | Code Examples | Medium -- Kalshi docs confirm endpoint; account-level availability unverified until tested |
| A8 | MARKET_MAP schema extension with optional `resolution_criteria` won't break existing callers | Pitfall 6 | Low -- optional-with-default keeps compat |
| A9 | `PortfolioMonitor` violations fire within 30s (check_interval) -- sufficient for SAFE-02 "pre-trade" requirement combined with RiskManager | Architectural Responsibility Map | Medium -- if execution rate exceeds 30s-resolution, portfolio-side checks lag; hence RiskManager.check_trade is primary gate |
| A10 | `cancel_all` can safely bypass per-adapter rate limiter via dedicated priority path | Pitfall 5 | Medium -- risks 429 under load; needs runtime tuning |

**Resolution path:** Phase 3 plan-check / discuss-phase should turn A3, A6, A7, A9, A10 into explicit `D-*` user decisions before locking plans.

## Open Questions

1. **Does Redis availability in the target environment warrant extra complexity vs. Postgres-only?**
   - What we know: repo has redis[hiredis] as a dep; `.env` may or may not have REDIS_URL set; `price_store.py` accepts `redis_client=None` already.
   - What's unclear: whether the deployment environment provisions Redis in Phase 3 timeframe.
   - Recommendation: make Redis optional; fall back to in-process + Postgres. Let operator decide via env var.

2. **Should the kill-switch reset require two-factor (email + confirmation code) for operator auth?**
   - What we know: current auth is password-only HMAC session.
   - What's unclear: whether small-capital risk tolerance justifies extra friction.
   - Recommendation: v1 uses existing session auth + `window.confirm`. Defer 2FA to post-Live trading (v2).

3. **Is automated kill-switch triggering (daily-loss threshold, error-rate ceiling) in scope?**
   - What we know: REQUIREMENTS.md lists OPT-04 as v2 (deferred). SAFE-01 says "triggerable from dashboard and programmatic thresholds" but doesn't specify what thresholds.
   - What's unclear: does "programmatic" mean "via API call from another script" (trivially supported) or "auto-triggered by internal threshold monitor" (OPT-04 territory)?
   - Recommendation: implement `SafetySupervisor.trip_kill()` as a public async method that ANY thread of execution can call. That satisfies "programmatic" literally. Auto-triggers (daily-loss, error-rate) stay deferred.

4. **How detailed should Telegram alerts be? Include order_ids and market details, or just counts?**
   - What we know: existing alerts include full details (price, qty, fees).
   - What's unclear: PII/audit concerns if phone stolen + Telegram cached.
   - Recommendation: include canonical_id + counts + severity. Omit raw order_ids (Telegram is not the audit system; dashboard is).

5. **For SAFE-06 resolution-criteria comparison, should we auto-fetch Kalshi/Polymarket rules or require operator entry?**
   - What we know: Kalshi markets have a `rules_primary` field in their API response. Polymarket has `description` and `resolutionSource`.
   - What's unclear: reliability and schema stability across markets.
   - Recommendation: auto-populate from collector metadata when available; fall back to operator-entered free text. Tag auto-populated entries explicitly so operator knows to verify.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 5.0+ (configured via `conftest.py` at `arbiter/`) |
| Config file | `arbiter/conftest.py` (async harness) + `pytest.ini` (absent; uses default discovery) |
| Quick run command | `python -m pytest arbiter/safety/ -x` |
| Full suite command | `python -m pytest arbiter/ -x --tb=short` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SAFE-01 | `SafetySupervisor.trip_kill()` cancels across all adapters in ≤5s | integration | `pytest arbiter/safety/test_supervisor.py::test_trip_kill_cancels_all -x` | ❌ Wave 0 |
| SAFE-01 | Engine gate rejects new ops while armed | unit | `pytest arbiter/safety/test_supervisor.py::test_allow_execution_armed -x` | ❌ Wave 0 |
| SAFE-01 | `/api/kill-switch` arm+reset flow | integration | `pytest arbiter/test_api_safety.py::test_kill_switch_arm_and_reset -x` | ❌ Wave 0 |
| SAFE-01 | Cooldown prevents immediate reset | unit | `pytest arbiter/safety/test_supervisor.py::test_reset_respects_cooldown -x` | ❌ Wave 0 |
| SAFE-01 | Telegram alert fires on arm | unit | `pytest arbiter/safety/test_alerts.py::test_arm_sends_telegram -x` | ❌ Wave 0 |
| SAFE-02 | Per-platform exposure limit blocks order | unit | `pytest arbiter/execution/test_engine.py::test_risk_per_platform_limit -x` | ✅ (file exists, add case) |
| SAFE-02 | Per-market limit blocks order (existing) | unit | `pytest arbiter/execution/test_engine.py::test_risk_per_market_limit -x` | ✅ |
| SAFE-02 | Rejected-order incident emitted | unit | `pytest arbiter/execution/test_engine.py::test_rejected_order_emits_incident -x` | ✅ (extend) |
| SAFE-03 | One-leg filled+failed triggers `one_leg_exposure` event | integration | `pytest arbiter/execution/test_engine.py::test_one_leg_exposure_surfaces -x` | ✅ (extend) |
| SAFE-03 | Unwind recommendation persists to store | integration | `pytest arbiter/execution/test_engine.py::test_one_leg_unwind_recommendation_persisted -x` | ✅ (extend) |
| SAFE-03 | Telegram alert on one-leg | unit | `pytest arbiter/safety/test_alerts.py::test_one_leg_alert_format -x` | ❌ Wave 0 |
| SAFE-04 | Adapter `place_fok` awaits rate limiter | unit | `pytest arbiter/execution/adapters/test_kalshi_adapter.py::test_place_fok_acquires_rate_token -x` | ✅ (extend) |
| SAFE-04 | 429 response invokes `apply_retry_after` | unit | `pytest arbiter/execution/adapters/test_kalshi_adapter.py::test_429_applies_retry_after -x` | ✅ (extend) |
| SAFE-04 | Rate-limit state broadcast over WS | integration | `pytest arbiter/test_api_integration.py::test_rate_limit_ws_event -x` | ✅ (extend) |
| SAFE-05 | SIGINT triggers trip_kill BEFORE task cancel | integration | `pytest arbiter/test_main_shutdown.py::test_graceful_shutdown_cancels_orders_first -x` | ❌ Wave 0 |
| SAFE-05 | 5s timeout on cancel_all during shutdown | integration | `pytest arbiter/test_main_shutdown.py::test_shutdown_timeout_escalates -x` | ❌ Wave 0 |
| SAFE-06 | MARKET_MAP schema accepts optional `resolution_criteria` | unit | `pytest arbiter/test_config_loading.py::test_resolution_criteria_optional -x` | ✅ (extend) |
| SAFE-06 | Mapping API returns criteria in GET | unit | `pytest arbiter/test_api_integration.py::test_market_mappings_returns_resolution_criteria -x` | ✅ (extend) |
| SAFE-06 | Dashboard view-model builds comparison struct | unit | `node --test arbiter/web/dashboard-view-model.test.js` | ✅ (extend) |

### Sampling Rate
- **Per task commit:** `python -m pytest arbiter/safety/ arbiter/execution/test_engine.py -x` (<15s)
- **Per wave merge:** `python -m pytest arbiter/ -x --tb=short` (<60s)
- **Phase gate:** Full suite green + Playwright dashboard smoke (existing `output/verify_dashboard_polish.mjs` or equivalent) before `/gsd-verify-work`

### Wave 0 Gaps
- [ ] `arbiter/safety/__init__.py` — new package
- [ ] `arbiter/safety/supervisor.py` — implements SafetySupervisor, SafetyConfig, SafetyState
- [ ] `arbiter/safety/alerts.py` — Telegram message templates
- [ ] `arbiter/safety/persistence.py` — Redis + Postgres sync
- [ ] `arbiter/safety/test_supervisor.py` — covers SAFE-01 unit + integration
- [ ] `arbiter/safety/test_alerts.py` — covers SAFE-01/SAFE-03 alert formatting
- [ ] `arbiter/safety/test_persistence.py` — covers Redis + PG audit
- [ ] `arbiter/test_api_safety.py` — covers `/api/kill-switch` + `/api/safety/*` routes
- [ ] `arbiter/test_main_shutdown.py` — covers SAFE-05 shutdown ordering
- [ ] `arbiter/sql/safety_events.sql` — schema migration for audit table
- [ ] `arbiter/web/dashboard-view-model.test.js` — extend with `buildSafetyView` cases
- [ ] Extend `arbiter/execution/test_engine.py` — per-platform limit case, one-leg exposure case, rejected-order incident case
- [ ] Extend `arbiter/execution/adapters/test_kalshi_adapter.py` + `test_polymarket_adapter.py` — `cancel_all` batch-cancel case + rate-limit acquire case
- [ ] Extend `arbiter/test_api_integration.py` — new WS event types + resolution-criteria-in-mapping-response

## Security Domain

Security enforcement is enabled (no explicit `false` in config). Phase 3's security surface is narrow but critical because kill-switch is the single most dangerous operator control.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Existing HMAC-SHA256 session token (`api.py::_generate_token`); kill-switch POST requires `require_auth` |
| V3 Session Management | yes | 7-day token TTL already enforced; kill-switch actions are audit-logged with operator email |
| V4 Access Control | yes | `require_auth` guard on `POST /api/kill-switch`; public/ops mode split keeps read-only state visible to unauthenticated viewers; kill button renders only when `hasOperatorAccess()` true |
| V5 Input Validation | yes | Kill-switch body validation: `action` must be `"arm"` or `"reset"`, `reason`/`note` are `str` <= 500 chars; resolution_criteria: structured dict with explicit keys |
| V6 Cryptography | no | No new cryptography — reuse existing Kalshi RSA + cookie HMAC |
| V7 Error Handling | yes | Never leak stacktraces to API responses; existing pattern returns `{"error": msg}` |
| V9 Communications | yes | Telegram Bot API is HTTPS-only (already); dashboard WebSocket should be TLS in production (existing `_request_is_secure` pattern) |
| V12 Files | no | No file uploads in Phase 3 |
| V13 API | yes | CSRF protection: POST routes require `Authorization: Bearer` or cookie + same-origin; existing CORS allows `*` — **flag for hardening** |

### Known Threat Patterns for Python async web + safety controls

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Unauthorized kill-switch trip (CSRF from third-party site) | Tampering | Restrict CORS to specific origins (currently `*` — Phase 3 should narrow). Use `SameSite=Lax` cookie (already set). |
| Kill-switch bypass via direct engine manipulation | Tampering | SafetySupervisor is the only public path; engine's `_check_trade_gate` MUST be the only submission gate; add test asserting `execute_opportunity` calls gate before any adapter call |
| Replay of armed cookie after logout | Spoofing | Existing `_ACTIVE_SESSIONS` in-memory dict invalidates on logout; kill-switch actions double-check `_ACTIVE_SESSIONS.get(token) == email` |
| Operator email enumeration via login error | Information Disclosure | Existing login returns generic `"Invalid credentials"` regardless of whether email is known |
| Kill-switch log tampering | Repudiation | Postgres `safety_events` table with append-only INSERT; never UPDATE or DELETE; timestamp is `NOW()` server-side |
| Telegram bot token leak via dashboard | Information Disclosure | Token never sent to frontend; only `enabled=bool` surfaces to UI |
| Race condition: concurrent trip+reset | Tampering | Use `asyncio.Lock()` in SafetySupervisor around state transitions; one trip/reset at a time |
| Unbounded safety_events table growth | Denial of Service | TTL/archival policy (e.g., keep 90 days online); monitor table size |

**Phase 3 specific security gate:** Before merging, verify:
1. `POST /api/kill-switch` requires auth (`require_auth`) and rejects 401 without valid session.
2. WebSocket `kill_switch` event fires even to unauthenticated viewers (read-only state is OK to expose; only the action requires auth).
3. CORS origin for production deployment is narrowed (not left as `*`).
4. Concurrency test: spawn 10 parallel arm requests; assert exactly one succeeds and `safety_events` has exactly one INSERT.

## Sources

### Primary (HIGH confidence)

- **Codebase introspection:** `arbiter/execution/engine.py` (1160 lines), `arbiter/execution/adapters/base.py`, `arbiter/utils/retry.py`, `arbiter/monitor/balance.py`, `arbiter/portfolio/monitor.py`, `arbiter/readiness.py`, `arbiter/main.py`, `arbiter/api.py`, `arbiter/web/dashboard.html`, `arbiter/web/dashboard.js`, `arbiter/web/dashboard-view-model.js`, `arbiter/config/settings.py`, `requirements.txt`
- **REQUIREMENTS.md** — SAFE-01..SAFE-06 authoritative text
- **ROADMAP.md** — Phase 3 success criteria
- **Phase 2 RESEARCH.md** — establishes adapter Protocol, engine hook pattern, structlog/tenacity/Sentry wiring
- **Phase 2 PATTERNS.md** (02.1) — external_client_order_id threading pattern informs safety-event audit writes
- **Python docs — signal module** `https://docs.python.org/3/library/signal.html` — `loop.add_signal_handler` semantics `[CITED]`
- **Kalshi API docs — batch cancel** `https://docs.kalshi.com/api-reference/orders/batch-cancel-orders` — `DELETE /portfolio/orders/batched` up to 20 orders `[CITED]`
- **Polymarket CLOB docs — cancel** `https://docs.polymarket.com/developers/CLOB/orders/cancel-orders` — `cancel_all` L2 endpoint `[CITED]`
- **py-clob-client repo** `https://github.com/Polymarket/py-clob-client` — v0.34.x API surface `[CITED]`

### Secondary (MEDIUM confidence)

- **aiolimiter PyPI** `https://pypi.org/project/aiolimiter/` — 1.2.1 leaky-bucket implementation (used as comparison baseline) `[CITED]`
- **python-telegram-bot discussions** `https://github.com/python-telegram-bot/python-telegram-bot/discussions/2351` — v20 async model (used as comparison baseline) `[CITED]`
- **aiogram docs** `https://aiogram.dev/` — async alternative (comparison) `[CITED]`
- **Graceful shutdowns with asyncio** `https://roguelynn.com/words/asyncio-graceful-shutdowns/` — standard pattern reference `[CITED]`

### Tertiary (LOW confidence — flagged for validation)

- None. All findings were verified against the codebase or official docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every recommended package is already installed and proven in Phase 2
- Architecture: HIGH — Phase 2 established the engine gate + adapter Protocol + incident subscribe patterns Phase 3 depends on; no new architectural risk
- Pitfalls: HIGH — pitfalls #1, #2, #3, #5 are grounded in Phase 2 review findings (CR-01/CR-02 lessons). #4 and #6 are inferred from dashboard audit.
- UI gaps: HIGH — direct code read of dashboard.html/.js; behaviors of existing controls verified via grep

**Research date:** 2026-04-16
**Valid until:** 2026-05-16 (30 days — stable domain, no fast-moving libraries)
