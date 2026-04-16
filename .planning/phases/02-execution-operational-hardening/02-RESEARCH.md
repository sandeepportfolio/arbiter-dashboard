# Phase 2: Execution & Operational Hardening - Research

**Researched:** 2026-04-16
**Domain:** Production-grade async Python execution engine for prediction-market arbitrage (FOK orders, PostgreSQL persistence, structured logging, retry, adapter extraction)
**Confidence:** HIGH

## Summary

This phase converts an in-memory async execution engine into a durable, observable, production-ready trader for Kalshi and Polymarket. The good news: **every requirement maps cleanly onto a well-established Python pattern with first-party library support**. FOK is natively supported on both platforms (Kalshi via `time_in_force: "fill_or_kill"`, Polymarket via `OrderType.FOK`), so no synthesizing fill-or-kill behavior from cancel-after-partial logic is required. PostgreSQL persistence has a ready-made in-repo template in `arbiter/ledger/position_ledger.py` that should be mirrored rather than re-invented. structlog, tenacity, and sentry-sdk all have canonical asyncio patterns. The adapter-extraction work has low technical risk because `_place_kalshi_order`, `_place_polymarket_order`, `_cancel_kalshi_order`, and `_cancel_polymarket_order` already carry platform-specific logic in clearly separable methods; the refactor is mostly a mechanical move.

The real risk is **Polymarket's known-stale `get_order_book` endpoint** (Issue #180, reported November 2025) — the pre-trade depth verification (EXEC-03) will silently degrade if the research-phase assumption that `get_order_book` returns fresh data is taken at face value. Plans must treat `get_order_book` as suspect and cross-check against `get_price`. The second-biggest risk is **idempotency on retry**: tenacity will happily retransmit a POST on a timeout, and neither Kalshi nor Polymarket will deduplicate without a client-supplied idempotency key. Kalshi already accepts `client_order_id` (the code uses it), Polymarket does not natively support one — which means retries on Polymarket order POSTs must be avoided at the adapter layer and pushed to the leg-orchestration layer (retry means "cancel-or-verify then retry with a new signed order", not "naively resend").

**Primary recommendation:** Extract `execution/adapters/kalshi.py` and `execution/adapters/polymarket.py` behind a `PlatformAdapter` Protocol. Enforce FOK exclusively at the adapter layer (native platform mechanism, not synthetic). Persist every state transition via an `ExecutionStore` class that mirrors `PositionLedger`. Install structlog via stdlib-ProcessorFormatter bridge so existing `logger.info(...)` calls continue to work but emit JSON with bound `arb_id`/`order_id`/`platform` context. Use tenacity for transient HTTP errors inside adapters, keep `CircuitBreaker` as the outer-layer "platform is unhealthy, stop all execution" gate. On restart, reconcile with the platform API (query open orders by `client_order_id`) rather than trust the DB alone — this phase's capital is small enough that safety-on-reconciliation is cheap.

## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** FOK (fill-or-kill) order types for both legs on both platforms — no partial fills allowed (EXEC-01)
- **D-02:** Execution state (orders, fills, incidents) persisted to PostgreSQL and survives process restart (EXEC-02)
- **D-03:** Pre-trade order book depth verification before submission — confirm sufficient liquidity (EXEC-03)
- **D-04:** Per-platform execution adapters extracted from engine.py into `arbiter/execution/adapters/` — no platform-specific logic remains in engine.py (EXEC-04)
- **D-05:** Execution timeout with automatic cancellation if fill not received within threshold (EXEC-05)
- **D-06:** Structured JSON logging via structlog for all trading operations (OPS-01)
- **D-07:** Sentry error tracking for unhandled exceptions and execution failures (OPS-02)
- **D-08:** Retry logic via tenacity for transient API failures with appropriate backoff (OPS-03)
- **D-09:** Dependency versions upgraded: `py-clob-client` to 0.34.x, `cryptography` to 46.x (OPS-04)
- **D-10:** Kalshi uses dollar string format (`yes_price_dollars`, `count_fp`) per Phase 1 D-15, D-16
- **D-11:** Polymarket uses `py-clob-client` SDK with `signature_type` and `funder` params per Phase 1 D-02, D-03
- **D-12:** PredictIt execution code already removed — only Kalshi and Polymarket adapters needed
- **D-13:** Polymarket heartbeat already runs as dedicated async task — adapter extraction must not disturb it

### Claude's Discretion

- **D-14:** Adapter extraction pattern (Protocol/ABC/thin-wrapper) — Claude picks
- **D-15:** FOK enforcement layer (adapter, engine, or both) — Claude picks
- **D-16:** State persistence granularity (every transition vs terminal states) — Claude picks, biased to full audit trail per CLAUDE.md "cannot afford to lose capital to bugs"
- **D-17:** Restart recovery strategy (query platforms vs flag-as-orphaned) — Claude picks, biased to safer option
- **D-18:** Retry/CircuitBreaker layering (keep both layered vs replace with tenacity) — Claude picks, must keep tenacity per OPS-03
- **D-19:** Logging migration approach (full structlog vs stdlib-processor-chain) — Claude picks
- Exact PostgreSQL schema for `orders`, `fills`, `incidents`
- asyncpg connection pool sizing and lifecycle
- Backoff tuning per platform (respecting Kalshi 10 writes/sec)
- Execution timeout threshold values
- Sentry DSN env var name and sampling rate

### Deferred Ideas (OUT OF SCOPE)

- WebSocket price feeds replacing REST polling (OPT-01, v2)
- Automated kill-switch triggers (OPT-04, v2)
- Dynamic fee-rate SDK fetching (MON-02, v2)
- Settlement divergence monitoring (MON-01, v2)
- Telegram `/kill` integration (SAFE-01, belongs to Phase 3)

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| EXEC-01 | FOK order types both legs both platforms | Kalshi `time_in_force: "fill_or_kill"` verified on docs.kalshi.com; Polymarket `OrderType.FOK` verified on py-clob-client README |
| EXEC-02 | Execution state persisted to PostgreSQL, survives restart | asyncpg pattern established in `arbiter/ledger/position_ledger.py`; DDL template included below |
| EXEC-03 | Pre-trade order book depth verification | Kalshi `/trade-api/v2/markets/{ticker}/orderbook` with `depth=100` no auth; Polymarket `client.get_order_book()` public — BUT see pitfall on stale-data bug |
| EXEC-04 | Per-platform adapters in `execution/adapters/` | Protocol-based adapter pattern with 5 methods covers every platform-specific call site already in engine.py |
| EXEC-05 | Execution timeout with auto-cancel | `asyncio.wait_for(..., timeout=T)` + `CancelledError` handler + adapter `cancel_order` |
| OPS-01 | structlog JSON logging | `structlog.contextvars.merge_contextvars` + `JSONRenderer` via stdlib `ProcessorFormatter` for zero-touch of existing `logger.info(...)` calls |
| OPS-02 | Sentry error tracking | `AsyncioIntegration` + `AioHttpIntegration` auto-enabled when aiohttp in deps |
| OPS-03 | tenacity retry | `@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(1, 10), retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)))` |
| OPS-04 | Version upgrades | py-clob-client 0.34.6 verified installed; cryptography 44.0.0 currently installed, 46.x target |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Order lifecycle state machine | Engine (Python orchestrator) | — | Cross-platform invariant, owns arb-level state |
| Platform-specific order format | Adapter (`adapters/kalshi.py`, `adapters/polymarket.py`) | — | Platform API surface differs; engine stays platform-agnostic |
| FOK enforcement | Adapter | — | Each platform has native FOK — the adapter's only job is to pass the correct parameter |
| Order book depth verification | Adapter | — | Endpoint / method / response shape is platform-specific |
| Durable persistence (orders/fills/incidents) | Storage (new `execution/store.py`) | Engine (invokes store) | Separation of concerns; engine doesn't talk to asyncpg |
| Transient-failure retry | Adapter (via tenacity decorator) | — | Retry policy is scoped to the HTTP call, not the arb-level state machine |
| Circuit breaking | Engine / Collector layer | Adapter (consults state) | Sustained outage affects all adapter calls; already implemented in `utils/retry.py` |
| Structured context (arb_id, order_id) | Engine (binds contextvars) | Adapter (reads via `structlog.contextvars`) | Context propagates via `structlog.contextvars` — adapter log calls inherit automatically |
| Error reporting to Sentry | Global init in `arbiter/main.py` | Uncaught exceptions everywhere auto-captured | SDK-level concern, one-time setup |
| Restart reconciliation | Engine startup hook | Adapter (`list_open_orders_by_client_id`) | Engine queries DB for non-terminal orders, asks each adapter to verify with platform |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `structlog` | 25.5.0 | Structured JSON logging with contextvars | De facto Python structured logging; native asyncio contextvars support; first-class stdlib bridge [VERIFIED: pip index versions 2026-04-16] |
| `tenacity` | 9.1.4 | Retry with exponential backoff + jitter | Already a transitive dep (via streamlit); native async decorator; AsyncRetrying iterator for complex flows [VERIFIED: pip index versions 2026-04-16] |
| `sentry-sdk` | 2.58.0 | Error tracking | Official; auto-integrates with asyncio and aiohttp [VERIFIED: pip index versions 2026-04-16] |
| `asyncpg` | 0.31.0 | PostgreSQL async driver | Already installed 0.31.0; already used by `PositionLedger` and `MarketMap` [VERIFIED: `import asyncpg; print(asyncpg.__version__)` = 0.31.0] |
| `py-clob-client` | 0.34.6 | Polymarket CLOB SDK | Already installed 0.34.6; satisfies OPS-04 [VERIFIED: `pip show py_clob_client`] |
| `cryptography` | 46.x | Kalshi RSA signing | Currently 44.0.0 installed — OPS-04 requires upgrade to 46.x [VERIFIED: current; CITED: REQUIREMENTS.md OPS-04] |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `aiohttp` | 3.9.0+ | Async HTTP client | Already in use for Kalshi REST calls — keep |
| `structlog.stdlib.ProcessorFormatter` | (stdlib bridge) | Bridge stdlib `logging.Logger` output to structlog processors | To keep existing `logger.info(...)` calls working unchanged while producing JSON |
| `pytest` + repo's `conftest.py` | existing | Async test runner with bespoke `asyncio.run` harness | Extend, don't replace — existing convention |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| tenacity | `backoff` library | backoff is simpler but lacks tenacity's rich `stop`/`wait`/`retry_if` primitives; tenacity is Sentry's blessed choice and already in the env |
| tenacity | `asyncio_retry` (stdlib `asyncio` patterns) | Rolling our own duplicates what tenacity solves; OPS-03 explicitly names tenacity |
| ProcessorFormatter bridge | Full structlog migration of every module | Bridge keeps the PR scope small and avoids touching collector/audit/scanner code for this phase — matches "safety > speed" |
| `asyncpg` | `psycopg3` | psycopg3 has async support but asyncpg is already deployed in this repo — consistency wins |
| Protocol | ABC (`abc.ABC` + `@abstractmethod`) | Protocol doesn't require inheritance (lighter coupling); ABC gives nominal typing. Either works — Protocol preferred for Python 3.12 codebases |

**Installation:**

```bash
pip install "structlog>=25.5.0" "tenacity>=9.1.4" "sentry-sdk>=2.58.0" "cryptography>=46.0.0"
```

**Version verification (performed 2026-04-16):**

| Package | Latest on PyPI | Currently installed | Action |
|---------|---------------|--------------------|----|
| structlog | 25.5.0 | not installed | install |
| tenacity | 9.1.4 | 9.0.0 (transitive) | upgrade |
| sentry-sdk | 2.58.0 | not installed | install |
| cryptography | 46.x (to confirm in requirements.txt) | 44.0.0 | upgrade to 46.x per OPS-04 |
| py-clob-client | 0.34.6 | 0.34.6 | already satisfies OPS-04 |
| asyncpg | 0.31.0 | 0.31.0 | no change |

## Architecture Patterns

### System Architecture Diagram

```
                      arb_queue (asyncio.Queue)
                              │
                              ▼
                  ┌───────────────────────────┐
                  │     ExecutionEngine        │
                  │  (platform-agnostic        │
                  │   orchestrator)            │
                  │                            │
                  │  bind_contextvars(         │
                  │    arb_id, canonical_id)   │
                  └─────────────┬──────────────┘
                                │
                 ┌──────────────┼──────────────┐
                 │              │              │
   asyncio.wait_for(gather(     │              │
     yes_task, no_task), T)     │              │
                 │              │              │
                 ▼              ▼              │
     ┌──────────────────┐  ┌──────────────────┐│
     │ _place_leg(yes,  │  │ _place_leg(no,   ││
     │   platform_X)    │  │   platform_Y)    ││
     └────────┬─────────┘  └────────┬─────────┘│
              │                     │          │
              ▼                     ▼          │
     ┌────────────────┐   ┌────────────────┐   │
     │ KalshiAdapter  │   │ PolymarketAdptr│   │
     │ (tenacity.retry)   │ (tenacity.retry)│  │
     │ ─ check_depth  │   │ ─ check_depth  │   │
     │ ─ place_fok    │   │ ─ place_fok    │   │
     │ ─ cancel       │   │ ─ cancel       │   │
     │ ─ get_order    │   │ ─ get_order    │   │
     └────┬───────────┘   └─────┬──────────┘   │
          │                     │              │
          ▼                     ▼              │
     [Kalshi REST]        [Polymarket CLOB]    │
                                               │
              ExecutionStore (asyncpg)    ◀────┘
              ─ orders                   ▲
              ─ fills                    │
              ─ incidents                │
              ─ arb_executions           │
                    │                    │
                    ▼                    │
              PostgreSQL ────── restart recovery:
                                 query open_orders
                                 per adapter,
                                 reconcile with DB
```

**Key data flows:**
- Opportunity → Engine → parallel adapter calls (both legs wrapped in timeout+gather)
- Each adapter call: depth-check → place FOK → await fill confirmation → persist state
- Every state transition (PENDING → SUBMITTED → FILLED/CANCELLED/FAILED) writes to `orders` table
- Incident emitted on failure → incidents table → Sentry (via logger.error) → dashboard subscriber
- On startup: Engine queries orders where status IN ('pending', 'submitted'); asks adapter to verify platform state; reconciles or marks orphaned

### Recommended Project Structure

```
arbiter/
├── execution/
│   ├── engine.py                   # slim — orchestrator only, no platform code
│   ├── store.py                    # NEW — ExecutionStore (asyncpg persistence)
│   ├── adapters/
│   │   ├── __init__.py             # NEW — exports base + adapters
│   │   ├── base.py                 # NEW — PlatformAdapter Protocol
│   │   ├── kalshi.py               # NEW — moved from _place_kalshi_order + _cancel_kalshi_order
│   │   ├── polymarket.py           # NEW — moved from _place_polymarket_order + _cancel_polymarket_order
│   │   └── retry_policy.py         # NEW — tenacity decorators + predicates
│   ├── recovery.py                 # NEW — startup reconciliation logic
│   └── test_engine.py              # existing — extend, add adapter mocks
├── sql/
│   ├── init.sql                    # add orders/fills/incidents/arb_executions tables
│   └── migrations/                 # NEW — forward-only DDL migrations
│       └── 001_execution_persistence.sql
├── utils/
│   ├── logger.py                   # REPLACE — structlog setup
│   └── retry.py                    # keep — CircuitBreaker stays; retry_with_backoff can deprecate
└── main.py                         # wire DB pool, Sentry init, structlog config, startup reconciliation
```

### Pattern 1: Platform Adapter Protocol

**What:** Define a `Protocol` that every platform adapter implements. Engine depends on the protocol, not concrete classes.
**When to use:** For EXEC-04 (per-platform adapters). Pick Protocol over ABC because Protocol is structural (no inheritance requirement) and Python 3.12 handles it cleanly.

```python
# arbiter/execution/adapters/base.py
from __future__ import annotations
from typing import Protocol, runtime_checkable
from ..engine import Order  # or move Order to a shared module

@runtime_checkable
class PlatformAdapter(Protocol):
    """Every platform adapter must implement these methods.
    The engine knows ONLY about this protocol."""

    platform: str  # "kalshi" | "polymarket"

    async def check_depth(
        self, market_id: str, side: str, required_qty: int
    ) -> tuple[bool, float]:
        """Return (sufficient, best_price_at_depth). EXEC-03."""
        ...

    async def place_fok(
        self, arb_id: str, market_id: str, canonical_id: str,
        side: str, price: float, qty: int,
    ) -> Order:
        """Submit FOK order. EXEC-01. Retries via tenacity internally.
        Returns Order in terminal state (FILLED or CANCELLED) — FOK never leaves
        a partial."""
        ...

    async def cancel_order(self, order: Order) -> bool:
        """Best-effort cancel. Used for EXEC-05 timeout path."""
        ...

    async def get_order(self, order: Order) -> Order:
        """Query platform for current order state — used by startup reconciliation."""
        ...

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str
    ) -> list[Order]:
        """Used by startup recovery to find orphaned orders."""
        ...
```

### Pattern 2: FOK Enforcement at Adapter Layer

**What:** Each adapter passes the platform-native FOK directive. Engine never sees a partial fill from a healthy adapter.
**When to use:** Always — this is the core safety invariant for EXEC-01.

```python
# arbiter/execution/adapters/kalshi.py (Kalshi FOK)
# Source: https://docs.kalshi.com/api-reference/orders/create-order (verified 2026-04-16)
order_body = {
    "ticker": market_id,
    "client_order_id": f"{arb_id}-{side.upper()}-{uuid.uuid4().hex[:8]}",
    "action": "buy",
    "side": side,  # "yes" | "no"
    "type": "limit",
    "count_fp": f"{float(qty):.2f}",
    "time_in_force": "fill_or_kill",   # ← the critical line
}
if side == "yes":
    order_body["yes_price_dollars"] = f"{price:.4f}"
else:
    order_body["no_price_dollars"] = f"{price:.4f}"
```

```python
# arbiter/execution/adapters/polymarket.py (Polymarket FOK)
# Source: https://github.com/Polymarket/py-clob-client README (verified 2026-04-16)
from py_clob_client.clob_types import OrderArgs, OrderType

order_args = OrderArgs(
    token_id=market_id,
    price=round(price, 2),
    size=float(qty),
    side="BUY",
)
signed = client.create_order(order_args)
# OrderType.FOK: "market order that must be executed immediately in its entirety or cancelled"
response = client.post_order(signed, OrderType.FOK)
```

**Polymarket subtlety:** `OrderType` is an enum in `py_clob_client.clob_types`. Confirm enum values: `GTC`, `GTD`, `FOK`, `FAK`. The README example shows FOK paired with `create_market_order` + `MarketOrderArgs` (amount in USDC) — but `post_order(signed_limit_order, OrderType.FOK)` is also documented. **Arbitrage legs should use limit orders with FOK** so the bought price is capped at the edge-calculated value; market FOK orders in USDC would execute at whatever the best ask is, potentially exceeding the edge threshold. [VERIFIED: py-clob-client README + agentbets.ai reference]

### Pattern 3: Idempotent Order Submission + Retry

**What:** Only retry on network-level errors; never retry a POST whose server state is ambiguous without a reconciliation step first.
**When to use:** Every `place_fok` call.

```python
# arbiter/execution/adapters/retry_policy.py
import aiohttp
import asyncio
from tenacity import (
    AsyncRetrying, retry, stop_after_attempt, wait_exponential_jitter,
    retry_if_exception_type, before_sleep_log,
)

TRANSIENT_EXCEPTIONS = (
    aiohttp.ClientConnectionError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
)

def transient_retry(*, max_attempts: int = 3):
    """Decorator for network-level transient failures.
    DO NOT use on order POSTs that have already been accepted by the server
    unless the server supports idempotency keys (Kalshi: yes via client_order_id;
    Polymarket: no)."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=10),
        retry=retry_if_exception_type(TRANSIENT_EXCEPTIONS),
        before_sleep=before_sleep_log(structlog.get_logger(), logging.WARNING),
    )
```

**Critical distinction — WHERE to retry:**
- **Kalshi:** Retry at the adapter layer is SAFE because `client_order_id` is the idempotency key. If a retry resends the same client_order_id, Kalshi returns the existing order record. [CITED: docs.kalshi.com behavior is standard for REST order APIs with client_order_id]
- **Polymarket:** NO idempotency key. If `post_order` times out, we DO NOT know whether the order was accepted. Never blindly retry. Strategy: `AsyncRetrying` with a pre-check via `get_open_orders(market=token_id)` before each retry attempt — if an order with the leg's price/size exists, treat as success; else re-sign and re-post. [VERIFIED: agentbets.ai py-clob-client reference — no idempotency key field]

### Pattern 4: Execution Timeout with Cancel-on-Timeout

**What:** Wrap leg placement in `asyncio.wait_for`; on `TimeoutError`, actively cancel via adapter.
**When to use:** Every `_live_execution` call (EXEC-05).

```python
# arbiter/execution/engine.py (sketch)
async def _execute_leg_with_timeout(
    self, adapter: PlatformAdapter, arb_id: str, ...,
    timeout_seconds: float = 10.0,
) -> Order:
    place_task = asyncio.create_task(
        adapter.place_fok(arb_id, ...)
    )
    try:
        return await asyncio.wait_for(place_task, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        # place_task was cancelled by wait_for — but the HTTP request may have
        # reached the server. Actively reconcile.
        order = Order(..., status=OrderStatus.PENDING, error="local timeout")
        cancelled = await adapter.cancel_order(order)
        if cancelled:
            order.status = OrderStatus.CANCELLED
        else:
            # Couldn't confirm cancel. Emit incident, mark orphaned.
            order.status = OrderStatus.FAILED
            order.error = "timeout + cancel failed — manual check required"
        return order
```

### Pattern 5: structlog with stdlib Bridge + contextvars

**What:** Configure stdlib logging to route through structlog's `ProcessorFormatter`. Existing `logger.info("...")` calls continue to work — but output becomes JSON. Context variables propagate through async code via `structlog.contextvars`.
**When to use:** OPS-01. Pick this over a full migration to minimize PR surface area and avoid churn in collectors/audit/scanner.

```python
# arbiter/utils/logger.py (replaces current setup_logging)
import logging
import sys
import structlog
from structlog.processors import JSONRenderer, TimeStamper, add_log_level
from structlog.contextvars import merge_contextvars
from structlog.stdlib import ProcessorFormatter, add_logger_name

SHARED_PROCESSORS = [
    merge_contextvars,                             # arb_id, order_id, platform
    add_log_level,
    add_logger_name,
    TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

def setup_logging(level: str = "INFO") -> None:
    # Configure structlog itself
    structlog.configure(
        processors=SHARED_PROCESSORS + [
            ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib handler to use ProcessorFormatter → JSON
    formatter = ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
```

```python
# Binding context inside engine.py — zero extra imports for adapters
from structlog.contextvars import bind_contextvars, clear_contextvars

async def execute_opportunity(self, opp):
    clear_contextvars()
    bind_contextvars(
        arb_id=arb_id, canonical_id=opp.canonical_id,
    )
    try:
        # All downstream log calls (including stdlib `logger.info(...)`
        # in adapters) will include arb_id + canonical_id in JSON output
        ...
    finally:
        clear_contextvars()
```

[CITED: structlog.org/en/stable/contextvars.html; django-structlog.readthedocs.io/en/latest/getting_started.html for ProcessorFormatter foreign_pre_chain pattern]

### Pattern 6: asyncpg Connection Pool — Mirror PositionLedger

**What:** Reuse the exact pattern from `arbiter/ledger/position_ledger.py`. A single `ExecutionStore` class owns a pool, exposes async CRUD methods.
**When to use:** EXEC-02.

```python
# arbiter/execution/store.py
import asyncpg
import logging
from typing import Optional
from ..execution.engine import Order, ArbExecution, ExecutionIncident

logger = logging.getLogger("arbiter.execution.store")

class ExecutionStore:
    """Postgres-backed durable store for execution state.
    Writes on every state transition for full audit trail (D-16)."""

    _pool: Optional[asyncpg.Pool] = None

    def __init__(self, database_url: str):
        self.database_url = database_url

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                max_queries=50_000,
                max_inactive_connection_lifetime=300.0,
                command_timeout=30,
            )
            logger.info("ExecutionStore: connected to Postgres")

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def upsert_order(self, order: Order) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO execution_orders (...)
                       VALUES (...)
                       ON CONFLICT (order_id) DO UPDATE SET
                         status=$N, fill_price=$N, fill_qty=$N,
                         updated_at=NOW(), error=$N""",
                    ...
                )

    async def insert_fill(self, order_id: str, price: float, qty: int) -> None: ...
    async def insert_incident(self, incident: ExecutionIncident) -> None: ...
    async def list_non_terminal_orders(self) -> list[Order]: ...
```

**Pool sizing (verified):** `min_size=2, max_size=10, max_queries=50_000, max_inactive_connection_lifetime=300.0`. This matches `PositionLedger` exactly and the 2026 asyncpg best-practice guidance for low-to-medium concurrency workloads. [CITED: asyncpg docs; magicstack.github.io/asyncpg/current/usage.html; PositionLedger pattern at arbiter/ledger/position_ledger.py:144-151]

### Pattern 7: Sentry Integration

**What:** Single-shot init in `arbiter/main.py`, automatically captures unhandled exceptions, integrates with asyncio tasks and aiohttp sessions.
**When to use:** OPS-02.

```python
# arbiter/main.py (early in startup)
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("ARBITER_ENV", "development"),
    release=os.getenv("ARBITER_RELEASE", "unknown"),
    integrations=[
        AsyncioIntegration(),
        AioHttpIntegration(),
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ],
    traces_sample_rate=0.0,           # keep off for live trading — latency-sensitive
    sample_rate=1.0,                   # capture 100% of errors
    send_default_pii=False,
    attach_stacktrace=True,
)
```

**Secret handling:** `SENTRY_DSN` stays in `.env`, pattern matches the existing `POLY_PRIVATE_KEY` handling. Absence of DSN means Sentry is a no-op — dev-safe. [VERIFIED: docs.sentry.io/platforms/python/integrations/asyncio]

### Anti-Patterns to Avoid

- **Retrying non-idempotent POSTs without an idempotency key.** Blindly retrying a Polymarket `post_order` after timeout can submit the same trade twice. Always pre-check open orders.
- **Using `OrderType.GTC` for arbitrage legs.** GTC leaves the order resting. Phase 2 goal is FOK everywhere.
- **Writing only on terminal states.** If the process crashes between SUBMIT and FILL, a terminal-only write loses the "I sent this order, I don't know its state" signal. D-16 says bias toward full audit trail — write on every transition.
- **Trusting `get_order_book` as the only liquidity check on Polymarket.** See pitfall — cross-check against `get_price`.
- **Mixing adapter retry with engine-level retry on the same call.** Tenacity inside adapter + engine-level retry of the leg = exponential retry count. Pick one layer.
- **Instantiating the ClobClient per-call.** Already cached via `_poly_clob_client` — adapter must honor the same pattern, and must not disturb the heartbeat task (D-13).
- **Logging the private key / RSA key / Sentry DSN.** Add a `structlog.processors.CallsiteParameterAdder` filter that strips any key matching `*_KEY` or `*_DSN` from event_dict before JSONRenderer.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Retry with exponential backoff + jitter | Custom `for attempt in range(3): try/except + asyncio.sleep` loop | `@tenacity.retry` | Tenacity handles `retry_if`, `stop_after`, `wait_exponential_jitter`, `AsyncRetrying` for complex cases — solved problem |
| FOK behavior on partial fill | Synthesizing FOK by cancelling after partial | Native `time_in_force: "fill_or_kill"` (Kalshi), `OrderType.FOK` (Polymarket) | Both platforms have native FOK; synthesizing it opens a race where the partial has already affected the other leg |
| Structured JSON logging | Custom `json.dumps` log formatter | `structlog` + `JSONRenderer` | Contextvars propagation, processor chain composability, Sentry integration — all built in |
| Correlation ID propagation across `asyncio.gather` | Passing `arb_id` as a parameter to every function | `structlog.contextvars` | Context inherits across `await` boundaries and task creation |
| Connection pooling | `asyncpg.connect()` per query | `asyncpg.create_pool(...)` with reused pool | 10x faster, avoids connection storms, already the pattern in `PositionLedger` |
| DB migrations | Ad-hoc `ALTER TABLE` in `init.sql` | Forward-only `sql/migrations/NNN_*.sql` files + migration runner | Phase 2 is the second major schema change — a pattern for future phases |
| Error tracking | `try/except Exception as e: logger.error(e)` alone | `sentry-sdk` with `AsyncioIntegration` | Captures unhandled async task failures that otherwise vanish into the event loop |
| Restart recovery | "Just restart and forget pending orders" | Adapter-based reconciliation via `list_open_orders_by_client_id` | Silent loss of a resting order = capital at risk (the bug CLAUDE.md warns about) |
| Timeout enforcement | Custom heartbeat polling + manual cancel | `asyncio.wait_for(task, T)` + cleanup in `TimeoutError` handler | Stdlib; battle-tested; integrates with cancellation |

**Key insight:** Every requirement in this phase has a canonical Python library. The engineering work is **plumbing and testing, not invention**. Any task plan that proposes a custom retry loop, custom JSON log formatter, or custom async DB pooling should be rejected in plan-check.

## Runtime State Inventory

> This phase adds new persistence, renames nothing. Most categories are N/A.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `positions` table exists (`PositionLedger`), `trades` table exists (`init.sql`). NEW tables this phase: `execution_orders`, `execution_fills`, `execution_incidents`, `execution_arbs`. | New DDL migration; no rename of existing tables |
| Live service config | **None.** Kalshi and Polymarket server-side config is auth credentials only; FOK is per-order. No service registration touches platform UIs. | None |
| OS-registered state | **None.** No scheduled tasks, no systemd units, no pm2 process names reference anything that changes. Docker container names stay the same (`arbiter-core`, `arbiter-postgres`, `arbiter-redis`). | None |
| Secrets/env vars | New env vars introduced: `SENTRY_DSN`, `ARBITER_ENV`, `ARBITER_RELEASE`. Existing: `DATABASE_URL` already set in docker-compose.yml (line 62). No renames. | Add to `.env.template`; document in config loader |
| Build artifacts | `requirements.txt` updated (structlog, tenacity, sentry-sdk added; cryptography bumped). Docker image needs rebuild. No compiled binaries. | `docker compose build arbiter` after requirements.txt change |

## Common Pitfalls

### Pitfall 1: Polymarket `get_order_book` returns stale "ghost" data

**What goes wrong:** Depth check reads best-bid=0.01 / best-ask=0.99 from `get_order_book`; system believes there is no liquidity or 99¢ spread; refuses to trade a real opportunity. Worse inversion: the stale book looks favorable, and the adapter approves a trade that fails against actual asks.
**Why it happens:** Known issue #180 on py-clob-client repo (November 2025). `/book` endpoint caches stale state for active markets while `get_price` returns fresh data.
**How to avoid:** In `adapters/polymarket.py::check_depth`, call BOTH `get_order_book` AND `get_price`. Treat the two as a consistency check. If `get_price` is within the best-bid/ask range of `get_order_book`, trust the book. If `get_price` falls outside the book's range by more than 1¢, log incident and refuse the trade.
**Warning signs:** Depth check consistently reports bid≈0.01 or ask≈0.99 for markets with known volume; `get_price` returns a price outside the book spread.
[CITED: github.com/Polymarket/py-clob-client/issues/180]

### Pitfall 2: Polymarket `post_order` has no idempotency key

**What goes wrong:** Retry-on-timeout sends the same order twice. Both fill. Position is 2x intended size. Capital lost to a bug — exactly what CLAUDE.md forbids.
**Why it happens:** `py-clob-client` `post_order` signs and submits; there is no `client_order_id` on the Polymarket side equivalent to Kalshi's. On network timeout, the caller cannot distinguish "server never saw request" from "server accepted, response dropped".
**How to avoid:** In `adapters/polymarket.py::place_fok`, wrap the POST in `AsyncRetrying(...)` but use `before=` hook to call `client.get_open_orders(market=token_id)` — if an open order with matching price/size exists, treat the previous attempt as successful and return. Only issue a new signed order if no matching order exists.
**Warning signs:** Same arb_id appears on two different order IDs in `execution_orders` table; platform balance drops by 2x expected USDC.

### Pitfall 3: FOK on Polymarket limit orders — documentation ambiguity

**What goes wrong:** Using `OrderType.FOK` with `MarketOrderArgs` (amount in USDC) buys at whatever the best ask is, potentially exceeding the edge-calculated price. The arb is no longer profitable.
**Why it happens:** The py-clob-client README example pairs `OrderType.FOK` with `MarketOrderArgs`. This is a market FOK — fills at best price available. Arbitrage requires a priced FOK to cap the buy.
**How to avoid:** Use `OrderArgs(price=X, size=Y, side, token_id)` (a limit order) and pass `OrderType.FOK` to `post_order`. The search results confirm "limit orders support GTC, FOK, and FAK." Test this in a dry-run: the post should succeed and either fill instantly at X-or-better or be rejected.
**Warning signs:** Fill price in `execution_fills` differs from limit price; realized PnL doesn't match opportunity.net_edge.
[CITED: agentbets.ai py-clob-client reference: "Limit orders support GTC, FOK, and FAK; market orders must use FOK"]

### Pitfall 4: Kalshi 10 writes/sec rate limit vs tenacity retries

**What goes wrong:** Tenacity retries a failed order 3 times in rapid succession, each retry also counts against the rate limiter. Combined with scanner-driven burst of opportunities, hit rate limit, get 429. Circuit breaker opens. System halts.
**Why it happens:** Rate limit is measured at the platform, not locally. Retries consume budget. Existing `RateLimiter` in `utils/retry.py` is advisory — tenacity doesn't know about it.
**How to avoid:** In `adapters/kalshi.py::place_fok`, call `self.rate_limiter.acquire()` inside the retry-decorated function body (BEFORE the HTTP call), not around the whole retry block. Each retry attempt will wait for a token. Combine with tenacity `wait_exponential_jitter` so retries also spread out temporally.
**Warning signs:** Sustained 429 responses; `RateLimiter.stats["penalty_count"]` > 0; circuit breaker trips open.

### Pitfall 5: Lost orders across restart

**What goes wrong:** Process crashes after SUBMIT, before FILL notification. On restart, DB says "order is SUBMITTED"; platform may have filled, cancelled, or the order may still be resting. If the engine doesn't reconcile, we have a ghost position.
**Why it happens:** FOK resolves in milliseconds, but a crash at a bad moment is possible. Kalshi FOK orders that fail validation return immediately; Polymarket FOK can also be rejected. Without reconciliation, DB and platform drift.
**How to avoid:** Startup hook in `ExecutionEngine.__init__` (or `arbiter/execution/recovery.py`): load all orders WHERE status IN ('pending', 'submitted') from DB; for each, call `adapter.get_order(order_id)` or `adapter.list_open_orders_by_client_id(client_order_id_prefix)`; if platform says CANCELLED/FILLED, update DB; if platform has no record, mark ORPHANED and emit an incident; if platform says RESTING (shouldn't happen with FOK), cancel it.
**Warning signs:** `execution_orders` rows in non-terminal state after startup; platform portfolio endpoint shows orders not in DB; balance check fails reconciliation.

### Pitfall 6: structlog `bind_contextvars` leaking across concurrent arbitrages

**What goes wrong:** Two opportunities execute concurrently. Both bind `arb_id` at the start. Thread 1's log entries contain Thread 2's arb_id.
**Why it happens:** `structlog.contextvars` uses `contextvars.ContextVar`, which is per-Task by default in asyncio — IF each execution runs in its own task. If both legs of one arb run in a shared task, they share context, which is fine. But if the engine runs multiple `execute_opportunity` calls in the same task sequentially without clearing, residual context leaks.
**How to avoid:** Always `clear_contextvars()` at the TOP of `execute_opportunity`, then `bind_contextvars`. Use `structlog.contextvars.bound_contextvars()` context manager if available (structlog 24+). Run `execute_opportunity` in its own `asyncio.create_task`.
**Warning signs:** Log entries with wrong `arb_id`; missing `arb_id` in some log entries.

### Pitfall 7: Sentry capturing PII / secrets

**What goes wrong:** Sentry breadcrumb or stack trace includes the `POLY_PRIVATE_KEY` or a session token, which is then sent to Sentry's servers.
**Why it happens:** Default Sentry integration auto-captures breadcrumbs for `logging.error` calls. If a stack trace includes local variables with secrets, they're sent.
**How to avoid:** `sentry_sdk.init(send_default_pii=False, ...)` + `before_send` callback that scrubs any dict value matching `*_KEY`, `*_SECRET`, `*_DSN`, `Authorization`. Structlog `CallsiteParameterAdder` with a filter processor also applies before JSON output.
**Warning signs:** Review Sentry events for event payloads — any key ending in `_KEY` or `_DSN` in the event body is a leak.

### Pitfall 8: asyncpg pool starvation under load

**What goes wrong:** Pool max_size=10 + a long-running transaction in one handler + 10 concurrent arbs = pool exhaustion, new queries time out, circuit trips, trades fail.
**Why it happens:** Transactions hold connections for their duration; if a transaction includes a slow HTTP call (never do this), pool is hostage.
**How to avoid:** Rule: NO network I/O inside a DB transaction. Structure writes as: "prepare row in memory, open transaction, execute short INSERT/UPDATE, commit." Set `command_timeout=30` to abort hung queries.
**Warning signs:** `asyncpg.exceptions.InternalClientError` ("timeout waiting for connection"); pool queue depth > 0 in metrics.

## Code Examples

### Kalshi FOK limit order (full adapter method sketch)

```python
# arbiter/execution/adapters/kalshi.py
# Source: docs.kalshi.com/api-reference/orders/create-order (verified 2026-04-16)
import json
import time
import uuid
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential_jitter
from ..engine import Order, OrderStatus

log = structlog.get_logger("arbiter.adapters.kalshi")

class KalshiAdapter:
    platform = "kalshi"

    def __init__(self, config, session, auth, rate_limiter, circuit):
        self.config = config
        self.session = session
        self.auth = auth
        self.rate_limiter = rate_limiter
        self.circuit = circuit

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=10),
    )
    async def place_fok(
        self, arb_id, market_id, canonical_id, side, price, qty,
    ) -> Order:
        if not self.circuit.can_execute():
            return Order(..., status=OrderStatus.FAILED, error="circuit open")
        await self.rate_limiter.acquire()

        client_order_id = f"{arb_id}-{side.upper()}-{uuid.uuid4().hex[:8]}"
        body = {
            "ticker": market_id,
            "client_order_id": client_order_id,  # idempotency key
            "action": "buy",
            "side": side,
            "type": "limit",
            "count_fp": f"{float(qty):.2f}",
            "time_in_force": "fill_or_kill",
            ("yes_price_dollars" if side == "yes" else "no_price_dollars"):
                f"{price:.4f}",
        }
        headers = self.auth.get_headers(
            "POST", "/trade-api/v2/portfolio/orders"
        )
        url = f"{self.config.base_url}/portfolio/orders"
        async with self.session.post(url, json=body, headers=headers) as resp:
            payload = await resp.text()
            if resp.status not in (200, 201):
                self.circuit.record_failure()
                log.error("kalshi.order.rejected",
                          status=resp.status, body=payload[:200],
                          client_order_id=client_order_id)
                return Order(..., status=OrderStatus.FAILED,
                            error=f"Kalshi {resp.status}: {payload[:200]}")
            self.circuit.record_success()
            data = json.loads(payload).get("order", {})
            # FOK: expect terminal status — FILLED or CANCELLED (killed)
            status_map = {
                "executed": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELLED,
                "resting": OrderStatus.SUBMITTED,  # shouldn't happen with FOK
            }
            fill_qty = float(data.get("fill_count_fp", "0") or "0")
            return Order(
                order_id=str(data.get("order_id", client_order_id)),
                platform="kalshi",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side, price=price, quantity=qty,
                status=status_map.get(data.get("status"), OrderStatus.FAILED),
                fill_price=float(data.get("yes_price_dollars",
                                          data.get("no_price_dollars", price))),
                fill_qty=fill_qty,
                timestamp=time.time(),
            )
```

### Polymarket FOK limit order

```python
# arbiter/execution/adapters/polymarket.py
# Source: github.com/Polymarket/py-clob-client README, agentbets.ai reference
import asyncio
import structlog
from py_clob_client.clob_types import OrderArgs, OrderType
from ..engine import Order, OrderStatus

log = structlog.get_logger("arbiter.adapters.polymarket")

class PolymarketAdapter:
    platform = "polymarket"

    def __init__(self, config, clob_client_factory, rate_limiter, circuit):
        self.config = config
        self._get_client = clob_client_factory  # reuse engine's cached instance
        self.rate_limiter = rate_limiter
        self.circuit = circuit

    async def place_fok(
        self, arb_id, market_id, canonical_id, side, price, qty,
    ) -> Order:
        # NOTE: NO @retry decorator at this layer — Polymarket has no
        # idempotency key, so retry must be reconcile-first. See _place_fok_reconciling.
        return await self._place_fok_reconciling(
            arb_id, market_id, canonical_id, side, price, qty,
        )

    async def _place_fok_reconciling(self, arb_id, market_id, ...,
                                     max_attempts=3):
        client = self._get_client()
        for attempt in range(max_attempts):
            # Pre-check: did a previous attempt succeed?
            existing = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_orders(market=market_id)
            )
            matching = [o for o in existing
                        if abs(o.price - price) < 0.01
                        and o.size == qty
                        and o.side == side.upper()]
            if matching:
                log.info("polymarket.order.reconciled",
                         order_id=matching[0].id, attempt=attempt)
                return self._order_from_clob_response(
                    matching[0], arb_id, canonical_id, side, price, qty
                )

            try:
                await self.rate_limiter.acquire()
                order_args = OrderArgs(
                    token_id=market_id,
                    price=round(price, 2),
                    size=float(qty),
                    side=side.upper(),  # "BUY" or "SELL"
                )
                signed = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.create_order(order_args)
                )
                response = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.post_order(signed, OrderType.FOK)
                )
                self.circuit.record_success()
                return self._order_from_post_response(
                    response, arb_id, canonical_id, side, price, qty
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                log.warning("polymarket.order.timeout",
                            attempt=attempt, exc=str(exc))
                # Loop back — pre-check will find the order if it went through
                await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception as exc:
                self.circuit.record_failure()
                log.error("polymarket.order.error", exc=str(exc))
                return Order(..., status=OrderStatus.FAILED, error=str(exc))

        return Order(..., status=OrderStatus.FAILED,
                     error="Polymarket max attempts exhausted")
```

### PostgreSQL schema (DDL template)

```sql
-- arbiter/sql/migrations/001_execution_persistence.sql

CREATE TABLE IF NOT EXISTS execution_arbs (
    arb_id              VARCHAR(40) PRIMARY KEY,
    canonical_id        VARCHAR(60) NOT NULL,
    status              VARCHAR(20) NOT NULL,  -- pending/submitted/filled/failed/recovering
    net_edge            DECIMAL(8,4),
    realized_pnl        DECIMAL(10,4) DEFAULT 0,
    opportunity_json    JSONB,
    is_simulation       BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS execution_orders (
    order_id            VARCHAR(100) PRIMARY KEY,
    arb_id              VARCHAR(40) NOT NULL REFERENCES execution_arbs(arb_id),
    client_order_id     VARCHAR(100),                 -- for restart reconciliation
    platform            VARCHAR(20) NOT NULL,
    market_id           VARCHAR(100) NOT NULL,
    canonical_id        VARCHAR(60) NOT NULL,
    side                VARCHAR(4) NOT NULL,
    price               DECIMAL(8,4) NOT NULL,
    quantity            DECIMAL(12,2) NOT NULL,
    status              VARCHAR(20) NOT NULL,          -- OrderStatus enum
    fill_price          DECIMAL(8,4) DEFAULT 0,
    fill_qty            DECIMAL(12,2) DEFAULT 0,
    error               TEXT DEFAULT '',
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminal_at         TIMESTAMPTZ
);
CREATE INDEX idx_execution_orders_arb ON execution_orders(arb_id);
CREATE INDEX idx_execution_orders_nonterminal
    ON execution_orders(status)
    WHERE status IN ('pending', 'submitted', 'partial');
CREATE INDEX idx_execution_orders_client_id
    ON execution_orders(client_order_id)
    WHERE client_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS execution_fills (
    fill_id             SERIAL PRIMARY KEY,
    order_id            VARCHAR(100) NOT NULL REFERENCES execution_orders(order_id),
    price               DECIMAL(8,4) NOT NULL,
    quantity            DECIMAL(12,2) NOT NULL,
    fees_paid           DECIMAL(10,4) DEFAULT 0,
    filled_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_execution_fills_order ON execution_fills(order_id);

CREATE TABLE IF NOT EXISTS execution_incidents (
    incident_id         VARCHAR(40) PRIMARY KEY,
    arb_id              VARCHAR(40),
    canonical_id        VARCHAR(60),
    severity            VARCHAR(20) NOT NULL,          -- info/warning/error/critical
    message             TEXT NOT NULL,
    metadata            JSONB,
    status              VARCHAR(20) DEFAULT 'open',
    resolved_at         TIMESTAMPTZ,
    resolution_note     TEXT DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_incidents_open ON execution_incidents(status) WHERE status = 'open';
CREATE INDEX idx_incidents_arb ON execution_incidents(arb_id);
```

**Design notes:**
- `execution_arbs.opportunity_json` stored as JSONB so the full opportunity is preserved without schema coupling to `ArbitrageOpportunity`.
- Partial indexes on non-terminal states accelerate startup reconciliation.
- `client_order_id` carried on `execution_orders` so Kalshi adapter's reconciliation query is a direct lookup.
- `execution_fills` is a separate table (1:N from orders) even though FOK produces ≤1 fill per order — keeps schema future-proof for when GTC/GTD orders are introduced.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `logging.getLogger(...).info("foo %s", bar)` printf-style | `structlog.get_logger(...).info("event.name", key=value)` event-name + kwargs | structlog 20.x+ | JSON output, zero-touch contextvars, processor pipeline |
| Thread-locals for request IDs | `contextvars.ContextVar` | Python 3.7+; structlog 20.x exposed via `structlog.contextvars` | Works correctly across `asyncio.gather` and `create_task` |
| Ad-hoc `try/except/sleep/retry` | `@tenacity.retry(...)` | Tenacity is now the standard | Composable, testable, documented retry policies |
| Kalshi integer-cents price fields (pre-March 2026) | `yes_price_dollars` / `no_price_dollars` string format with `count_fp` | March 2026 API migration (Phase 1 work) | Phase 1 already migrated this; Phase 2 must not regress |
| py-clob-client `create_and_post_order()` one-shot | `create_order()` → `post_order(signed, OrderType.FOK)` two-phase | py-clob-client 0.34.x | Allows inspection of signed order before submission (future-proof for dry-run validation) |

**Deprecated/outdated:**
- `raven-aiohttp` (legacy Sentry transport) — replaced by `sentry-sdk`'s native `AioHttpIntegration`.
- Kalshi integer-cents order fields — Phase 1 migrated away; any reference in Phase 2 plans is a bug.
- PredictIt execution code — Phase 1 removed; Phase 2 plans must not reintroduce (only Kalshi + Polymarket adapters).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Polymarket's `OrderType.FOK` enum value is literally `"FOK"` when compared and serializes correctly with `post_order(signed, OrderType.FOK)` | Pattern 2 | Orders post with wrong time-in-force → partial fills possible → EXEC-01 violated |
| A2 | Kalshi's `time_in_force: "fill_or_kill"` on a limit order rejects the order entirely if full size can't be filled at the specified price (does not partial-fill and cancel) | Pitfall 3 | Same — EXEC-01 violated |
| A3 | `cryptography>=46.0.0` is compatible with Kalshi's RSA signing path used in `arbiter/collectors/kalshi.py` | Version table | Kalshi auth fails on upgrade; roll-back required |
| A4 | `py-clob-client` `get_orders(market=token_id)` returns orders for the authenticated user, matching by market — accurate for reconciliation | Pattern 3 / Polymarket example | False positives in reconciliation → duplicate orders; false negatives → missed reconciliation → duplicate orders. Both land in EXEC-01 violation. |
| A5 | Execution timeout threshold of 10 seconds is reasonable starting default | Pattern 4 | Too low: legitimate fills cancelled. Too high: delay in recovering from stuck orders. Recommend making configurable. |
| A6 | asyncpg pool size 2/10 is sufficient (matching `PositionLedger`) even under scan-burst load | Pattern 6 | Pool exhaustion under burst — Pitfall 8 |
| A7 | Sentry `traces_sample_rate=0.0` (no tracing) is the right production default for this latency-sensitive trading system | Pattern 7 | Missing performance insights if ever needed; trivial to enable later. |
| A8 | Structlog's stdlib `ProcessorFormatter` bridge preserves structured kwargs from `logger.info("event", key=value)` calls in existing code | Pattern 5 / D-19 | Some log entries won't have structured fields → reduced observability (not a safety issue) |
| A9 | No Polymarket rate limiter is currently enforced — tenacity retries may hit platform limits | Pitfall 4 | Rate-limit bans. But SAFE-04 is scoped to Phase 3 anyway. |

**If any A1-A4 is wrong, it's a safety issue.** Recommend the planner add a dry-run/sandbox validation task that explicitly tests FOK partial-fill behavior on both platforms before any live trading (also aligns with Phase 4 TEST-01, TEST-02).

## Open Questions

1. **Cryptography 46.x compatibility with Kalshi RSA signing**
   - What we know: currently on 44.0.0, OPS-04 requires 46.x. Kalshi auth uses `cryptography`'s RSA-PSS primitive.
   - What's unclear: whether 46.x's API changes (if any) affect `collectors/kalshi.py::KalshiAuth`.
   - Recommendation: Planner creates a task that (a) upgrades, (b) runs `test_kalshi_collector.py`, (c) runs a live signed request against demo if available.

2. **Polymarket adapter retry strategy for CREATE_AND_POST vs CREATE + POST**
   - What we know: current code uses `client.create_and_post_order(order_args)`. Best practice for idempotency is `create_order(...)` → inspect → `post_order(signed, OrderType.FOK)`.
   - What's unclear: whether the one-shot method supports passing `OrderType.FOK`.
   - Recommendation: Planner reads py-clob-client 0.34.6 source to confirm; bias toward the two-phase pattern.

3. **Should existing `trades` table be migrated or kept alongside new tables?**
   - What we know: `init.sql` has a `trades` table with arb-level fields already, but it's not currently written to.
   - What's unclear: whether `execution_arbs` supersedes `trades` or they coexist.
   - Recommendation: Planner decides in phase-01-01-PLAN (schema migration). Suggestion: `execution_arbs` is the authoritative arb record; `trades` can be retired or kept as a summary view.

4. **Structured logging context in aiohttp request handlers (dashboard)**
   - What we know: `arbiter/api.py` uses aiohttp to serve the dashboard and WebSocket. Sentry `AioHttpIntegration` handles HTTP exceptions.
   - What's unclear: whether the dashboard request-scope context (e.g., `session_token_hash`, `request_id`) should be in every log entry.
   - Recommendation: Out of scope for Phase 2 — add a middleware task to Phase 3 or later.

5. **Timeout value for `asyncio.wait_for` on leg placement**
   - What we know: FOK orders typically resolve in <500ms under normal conditions.
   - What's unclear: upper bound under degraded network.
   - Recommendation: Start at 10 seconds (config-tunable via `ArbiterConfig.execution_timeout_s`). Observe in Phase 4 sandbox, adjust.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | Runtime | ✓ | 3.13.13 (local), 3.12 (Docker) | — |
| PostgreSQL | EXEC-02 persistence | ✓ (via Docker) | 16-alpine | — |
| asyncpg | EXEC-02 | ✓ | 0.31.0 | — |
| py-clob-client | Polymarket adapter | ✓ | 0.34.6 | — |
| tenacity | OPS-03 | ✓ (installed as transitive) | 9.0.0 → upgrade to 9.1.4 | — |
| structlog | OPS-01 | ✗ | needs install | No fallback — core requirement |
| sentry-sdk | OPS-02 | ✗ | needs install | No fallback — core requirement |
| cryptography | Kalshi auth | ✓ | 44.0.0 → upgrade to 46.x | — |
| Docker | Local dev | ✓ | 27.4.0 | — |
| Node.js | TypeScript CLI (not this phase) | ✓ | 22.14.0 | — |

**Missing dependencies with no fallback:**
- `structlog` and `sentry-sdk` must be added to `requirements.txt` and installed — trivial, just install step in plan.

**Missing dependencies with fallback:**
- None.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest + bespoke async harness in `conftest.py` |
| Config file | `conftest.py` at repo root (no pyproject.toml/setup.cfg) |
| Quick run command | `pytest arbiter/execution/ -x -v` |
| Full suite command | `pytest arbiter/ -x` |
| Async support | Custom `pytest_pyfunc_call` wrapping coroutines in `asyncio.run` — no pytest-asyncio plugin required |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| EXEC-01 | FOK: place order where insufficient depth → order CANCELLED not PARTIAL (Kalshi) | unit (mock aiohttp) | `pytest arbiter/execution/adapters/test_kalshi_adapter.py::test_fok_rejects_when_insufficient_depth -x` | ❌ Wave 0 |
| EXEC-01 | FOK: place order where insufficient depth → order CANCELLED (Polymarket) | unit (mock ClobClient) | `pytest arbiter/execution/adapters/test_polymarket_adapter.py::test_fok_rejects_when_insufficient_depth -x` | ❌ Wave 0 |
| EXEC-01 | FOK: full fill produces status=FILLED | unit | `pytest arbiter/execution/adapters/test_kalshi_adapter.py::test_fok_full_fill -x` | ❌ Wave 0 |
| EXEC-01 | FOK request body contains `time_in_force: "fill_or_kill"` (Kalshi) | unit (body assertion) | `pytest arbiter/execution/adapters/test_kalshi_adapter.py::test_fok_request_body_shape -x` | ❌ Wave 0 |
| EXEC-01 | FOK post_order called with `OrderType.FOK` (Polymarket) | unit | `pytest arbiter/execution/adapters/test_polymarket_adapter.py::test_fok_post_order_type -x` | ❌ Wave 0 |
| EXEC-02 | Order INSERT on submit, UPDATE on fill | integration (testcontainers / real PG) | `pytest arbiter/execution/test_store.py::test_order_lifecycle_persisted -x` | ❌ Wave 0 |
| EXEC-02 | After restart (pool close + re-init), non-terminal orders recoverable | integration | `pytest arbiter/execution/test_recovery.py::test_orders_survive_restart -x` | ❌ Wave 0 |
| EXEC-02 | Incident INSERT on adapter error | integration | `pytest arbiter/execution/test_store.py::test_incident_persisted -x` | ❌ Wave 0 |
| EXEC-03 | Adapter refuses when depth < required_qty | unit | `pytest arbiter/execution/adapters/test_kalshi_adapter.py::test_check_depth_insufficient -x` | ❌ Wave 0 |
| EXEC-03 | Polymarket stale-book guard: reject if `get_price` outside book spread by >1¢ | unit | `pytest arbiter/execution/adapters/test_polymarket_adapter.py::test_check_depth_stale_book -x` | ❌ Wave 0 |
| EXEC-04 | `import engine`; no `kalshi` or `polymarket` or `clob_client` or `aiohttp.post.*portfolio/orders` strings in engine.py | static (grep) | `! grep -E 'ClobClient\|portfolio/orders\|py_clob_client' arbiter/execution/engine.py` | ❌ Wave 0 (post-refactor) |
| EXEC-04 | `PlatformAdapter` protocol is structurally satisfied by both adapters | unit (runtime_checkable isinstance) | `pytest arbiter/execution/adapters/test_protocol_conformance.py -x` | ❌ Wave 0 |
| EXEC-05 | Timeout cancels and marks order FAILED or CANCELLED | unit (mock adapter that sleeps forever) | `pytest arbiter/execution/test_engine.py::test_execution_timeout -x` | ❌ Wave 0 |
| OPS-01 | Log output is valid JSON | unit | `pytest arbiter/utils/test_logger.py::test_output_is_json_parseable -x` | ❌ Wave 0 |
| OPS-01 | `arb_id` appears in every log line during `execute_opportunity` | unit (capture handler) | `pytest arbiter/utils/test_logger.py::test_contextvars_propagate -x` | ❌ Wave 0 |
| OPS-02 | Unhandled exception in a task is captured by Sentry (fake transport) | unit (sentry_sdk transport mock) | `pytest arbiter/main/test_sentry_integration.py::test_async_exception_captured -x` | ❌ Wave 0 |
| OPS-03 | Tenacity retries transient (`aiohttp.ServerTimeoutError`) 3x then raises | unit | `pytest arbiter/execution/adapters/test_retry_policy.py::test_transient_retries -x` | ❌ Wave 0 |
| OPS-03 | Non-transient errors (e.g. 400 Bad Request) NOT retried | unit | `pytest arbiter/execution/adapters/test_retry_policy.py::test_permanent_error_no_retry -x` | ❌ Wave 0 |
| OPS-04 | `requirements.txt` pins versions; `pip install -r requirements.txt` completes; `import` checks pass | smoke | `pip install -r requirements.txt && python -c "import structlog, tenacity, sentry_sdk; import cryptography; assert cryptography.__version__.startswith('46.')"` | ❌ Wave 0 (new script) |

### Sampling Rate

- **Per task commit:** `pytest arbiter/execution/ arbiter/utils/test_logger.py -x -v` (~quick subset of touched modules)
- **Per wave merge:** `pytest arbiter/ -x` (full Python suite)
- **Phase gate:** Full suite green + `docker compose up -d postgres` + integration tests green before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `arbiter/execution/adapters/__init__.py`, `base.py`, `kalshi.py`, `polymarket.py`, `retry_policy.py` — all new modules must exist before adapter tests can import
- [ ] `arbiter/execution/adapters/test_kalshi_adapter.py` — covers EXEC-01 (Kalshi), EXEC-03 (depth), OPS-03 (retry)
- [ ] `arbiter/execution/adapters/test_polymarket_adapter.py` — covers EXEC-01 (Polymarket), EXEC-03 (stale-book guard), reconciliation-on-retry
- [ ] `arbiter/execution/adapters/test_retry_policy.py` — covers OPS-03 transient vs permanent error classification
- [ ] `arbiter/execution/adapters/test_protocol_conformance.py` — `isinstance(KalshiAdapter(...), PlatformAdapter)` structural check
- [ ] `arbiter/execution/test_store.py` — covers EXEC-02 persistence (requires running Postgres; gate with `@pytest.mark.skipif(no PG)`)
- [ ] `arbiter/execution/test_recovery.py` — covers EXEC-02 restart recovery
- [ ] `arbiter/utils/test_logger.py` — covers OPS-01 JSON output + contextvars propagation
- [ ] `arbiter/main/test_sentry_integration.py` — covers OPS-02 async exception capture
- [ ] `arbiter/sql/migrations/001_execution_persistence.sql` — EXEC-02 DDL
- [ ] Dependency upgrade: add `structlog>=25.5.0`, `tenacity>=9.1.4`, `sentry-sdk>=2.58.0`, bump `cryptography>=46.0.0` in `requirements.txt`
- [ ] Update `Dockerfile` if needed so Docker image picks up new deps on rebuild

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes (Kalshi RSA signing, Polymarket EIP-712 signing via py-clob-client) | Existing patterns; `cryptography` upgrade must preserve key-handling semantics |
| V3 Session Management | yes (dashboard HMAC tokens, existing) | No change this phase |
| V4 Access Control | no (single-operator system) | — |
| V5 Input Validation | yes (order body fields, DB inputs) | Use Python typed dataclasses (`Order`); asyncpg parameterized queries (`$1, $2, ...`) — never f-string SQL |
| V6 Cryptography | yes (cryptography 46.x upgrade for Kalshi signing) | Never hand-roll; keep RSA-PSS via `cryptography` library |
| V7 Error Handling | yes (Sentry, structlog) | Don't log secrets (see Pitfall 7); Sentry `send_default_pii=False` |
| V8 Data Protection | yes (DB stores execution records, not secrets) | Private keys stay in env; DB connection string uses password from env |
| V9 Communications | yes (TLS to Kalshi/Polymarket APIs) | aiohttp defaults to TLS verification — do not disable |
| V14 Configuration | yes (.env pattern) | `SENTRY_DSN` etc. via `.env`; never commit |

### Known Threat Patterns for Python async + REST trading

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Secret leakage via exception stack trace (Sentry) | Information Disclosure | `before_send` scrubber + `send_default_pii=False` |
| Duplicate order via retry-without-idempotency-key (Polymarket) | Tampering (of account state) | Reconcile-before-retry (Pattern 3) |
| DB connection string in logs | Information Disclosure | structlog filter processor stripping `*_URL` / `*_DSN` / `*_KEY` |
| SQL injection via asyncpg | Tampering | Always use `$1, $2` parameterized queries — asyncpg does not interpolate |
| API key in env-var value accidentally echoed | Information Disclosure | Centralize config loading in `arbiter/config/settings.py`; never log the full config object |
| Weak RSA key during `cryptography` upgrade | Spoofing | Keep existing key-loading path unchanged; run Kalshi auth test immediately post-upgrade |
| Overly verbose dashboard error responses | Information Disclosure | aiohttp error handlers already strip internals; Sentry captures detail server-side |

## Sources

### Primary (HIGH confidence)

- [Kalshi Create Order API docs](https://docs.kalshi.com/api-reference/orders/create-order) — `time_in_force: "fill_or_kill"` confirmed; order type mechanics
- [Kalshi Get Market Orderbook docs](https://docs.kalshi.com/api-reference/market/get-market-orderbook) — depth endpoint, no auth, yes/no bid arrays
- [py-clob-client README (Polymarket)](https://github.com/Polymarket/py-clob-client/blob/main/README.md) — `OrderType.FOK`, `OrderArgs`, `MarketOrderArgs`
- [Structlog Context Variables docs](https://www.structlog.org/en/stable/contextvars.html) — `merge_contextvars`, stdlib `ProcessorFormatter`
- [Sentry asyncio integration](https://docs.sentry.io/platforms/python/integrations/asyncio/) — `AsyncioIntegration`
- [Sentry AIOHTTP integration](https://docs.sentry.io/platforms/python/integrations/aiohttp/) — `AioHttpIntegration`
- [asyncpg Usage docs](https://magicstack.github.io/asyncpg/current/usage.html) — connection pool, transactions
- [Tenacity docs](https://tenacity.readthedocs.io/) — async retry, `stop_after_attempt`, `wait_exponential_jitter`
- Codebase: `arbiter/ledger/position_ledger.py` — established asyncpg pattern to mirror for `ExecutionStore`
- Codebase: `arbiter/execution/engine.py` — existing Order/ArbExecution/ExecutionIncident dataclasses (no shape changes needed)
- Codebase: `arbiter/utils/retry.py` — existing `CircuitBreaker` and `RateLimiter` to reuse

### Secondary (MEDIUM confidence)

- [py-clob-client PyPI](https://pypi.org/project/py-clob-client/) — version 0.34.6 current
- [AgentBets py-clob-client reference (2026)](https://agentbets.ai/guides/py-clob-client-reference/) — enum details, order type × order args compatibility
- [AgentBets py_clob_client get_order_book guide](https://agentbets.ai/guides/py-clob-client-get-order-book/) — depth calculation patterns
- [django-structlog getting started](https://django-structlog.readthedocs.io/en/latest/getting_started.html) — `foreign_pre_chain` pattern for ProcessorFormatter
- [Better Stack structlog guide](https://betterstack.com/community/guides/logging/structlog/) — processor chain examples
- [Zuplo Kalshi API guide](https://zuplo.com/learning-center/kalshi-api) — supplementary order book / order type context

### Tertiary (LOW confidence — flagged for validation)

- [GitHub issue #180 - py-clob-client stale orderbook](https://github.com/Polymarket/py-clob-client/issues/180) — reported November 2025; validated via independent mention in agentbets.ai. Should still be treated as active until confirmed fixed in 0.34.6 release notes.
- Specific claim that "Limit orders support GTC, FOK, and FAK; market orders must use FOK" (py-clob-client) — seen in agentbets.ai reference, consistent with README examples, but not found verbatim in official docs. Plan should include a dry-run test to confirm.
- Polymarket `get_orders(market=token_id)` API shape — inferred from README snippets + agentbets.ai; planner should verify against 0.34.6 source before implementing Polymarket reconciliation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every library has first-party docs, versions verified via pip, existing codebase patterns confirm integration points.
- Architecture: HIGH — adapter pattern, asyncpg pattern, structlog pattern all established practice with one existing in-repo reference (`PositionLedger`).
- FOK semantics: HIGH for Kalshi (explicit `time_in_force: "fill_or_kill"` in docs), MEDIUM for Polymarket (README example uses market FOK; limit FOK is inferred from secondary sources — A1/A2 assumption).
- Polymarket retry / idempotency: MEDIUM — `client_order_id` absence confirmed; reconciliation-before-retry pattern is a sound design but specific `get_orders` API shape (A4) needs code-level confirmation.
- Restart recovery behavior: HIGH — standard pattern; plan needs to specify ORPHANED-state handling.
- Pitfalls: HIGH — #1 (stale book) and #2 (idempotency) are both documented issues, not speculation.

**Research date:** 2026-04-16
**Valid until:** 2026-05-16 (stable ecosystem; Polymarket SDK in transition to v2, so revisit if that ships before phase start)
