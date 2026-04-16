# Architecture Research

**Domain:** Production prediction market arbitrage system (Kalshi / Polymarket / PredictIt)
**Researched:** 2026-04-16
**Confidence:** MEDIUM-HIGH (platform APIs verified against official docs; production trading patterns from multiple real-world implementations)

## Standard Architecture

### System Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          SAFETY LAYER (global)                             │
│   Kill Switch (Redis flag)  |  Circuit Breakers  |  Position Limits        │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                      │
│  │  Kalshi WS   │  │ Polymarket   │  │  PredictIt   │                      │
│  │  + REST poll  │  │ WS + REST    │  │  REST poll   │  COLLECTOR LAYER     │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                      │
│         │                 │                 │                               │
│  ┌──────┴─────────────────┴─────────────────┴──────┐                       │
│  │           PRICE STORE (Redis, 30s TTL)          │  CACHE LAYER           │
│  │    Pub/Sub fan-out  |  Staleness detection      │                       │
│  └────────────────────────┬────────────────────────┘                       │
│                           │                                                │
│  ┌────────────────────────┴────────────────────────┐                       │
│  │            ARBITRAGE SCANNER                    │  DETECTION LAYER       │
│  │   Fee math  |  Persistence gating  |  Mapping   │                       │
│  └────────────────────────┬────────────────────────┘                       │
│                           │                                                │
│  ┌────────────────────────┴────────────────────────┐                       │
│  │          EXECUTION ENGINE                       │                       │
│  │  ┌─────────────────┐  ┌─────────────────────┐  │                       │
│  │  │   PRE-TRADE     │  │   ORDER MANAGER     │  │  EXECUTION LAYER      │
│  │  │  Re-quote check │  │  Platform adapters  │  │                       │
│  │  │  Math audit     │  │  FOK/limit routing  │  │                       │
│  │  │  Balance verify │  │  Fill monitoring     │  │                       │
│  │  └────────┬────────┘  └────────┬────────────┘  │                       │
│  │           │                    │                │                       │
│  │  ┌────────┴────────────────────┴────────────┐   │                       │
│  │  │        RECOVERY / UNWIND HANDLER         │   │                       │
│  │  │  Partial fill cancel  |  One-leg unwind  │   │                       │
│  │  └─────────────────────────────────────────┘   │                       │
│  └─────────────────────────┬───────────────────────┘                       │
│                            │                                               │
│  ┌─────────────────────────┴───────────────────────┐                       │
│  │           MONITORING & RECONCILIATION           │  MONITOR LAYER        │
│  │  Balance watch  |  P&L reconcile  |  Alerts     │                       │
│  └─────────────────────────┬───────────────────────┘                       │
│                            │                                               │
│  ┌─────────────────────────┴───────────────────────┐                       │
│  │        API / DASHBOARD / TELEGRAM               │  PRESENTATION LAYER   │
│  │  WebSocket broadcast  |  REST API  |  Alerts    │                       │
│  └─────────────────────────────────────────────────┘                       │
│                                                                            │
├────────────────────────────────────────────────────────────────────────────┤
│                   PERSISTENCE (PostgreSQL + Redis)                          │
│  Execution history | Positions | Incidents | Market mappings               │
└────────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Production Implementation |
|-----------|----------------|--------------------------|
| **Kill Switch** | Global emergency stop -- halts all new orders, optionally cancels open orders | Redis flag polled every execution cycle; dashboard toggle; Telegram command |
| **Platform Adapter (Kalshi)** | Translate generic order intent into Kalshi-specific API calls | RSA-PSS signed requests, fixed-point dollar pricing (`yes_price_dollars`), `count_fp` strings, limit orders only (market orders removed Feb 2026) |
| **Platform Adapter (Polymarket)** | Translate generic order intent into Polymarket CLOB calls | EIP-712 wallet signing via py-clob-client, heartbeat every 5s (10s timeout cancels all open orders), FOK/GTC order types |
| **Platform Adapter (PredictIt)** | Provide read-only price data; manual execution workflow | No trading API exists -- read-only data feed, operator executes manually via web UI |
| **Price Store** | Canonical price cache with staleness tracking | Redis-backed, 30s TTL, pub/sub notifications, quote age tracking |
| **Arbitrage Scanner** | Detect fee-aware cross-platform opportunities | Persistence gating, fee calculation per venue, net edge computation |
| **Execution Engine** | Coordinate two-leg order placement with safety checks | Pre-trade re-quote, concurrent leg submission, partial fill recovery |
| **Recovery Handler** | Handle failed or partial executions | Cancel unfilled legs, log incidents, alert operator for manual intervention |
| **Heartbeat Manager** | Keep Polymarket session alive during trading | Dedicated async task sending heartbeats every 5s; failure = all open Polymarket orders auto-cancelled |
| **Balance Monitor** | Track per-platform account balances | Pre-trade balance verification, low-balance alerts |
| **Reconciler** | Verify recorded P&L matches actual platform balances | Periodic comparison, flag discrepancies as incidents |

## Platform-Specific API Behavior (Critical for Architecture)

### Kalshi API

**Confidence: HIGH** (verified against docs.kalshi.com, April 2026)

| Aspect | Detail |
|--------|--------|
| **Auth** | RSA-PSS signature: sign `{timestamp_ms}{METHOD}{path}` with SHA-256. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP` |
| **Order Types** | **Limit orders only** (market orders removed Feb 11, 2026). Use `time_in_force: "fill_or_kill"` for immediate execution |
| **Pricing Format** | **Fixed-point dollar strings** (`yes_price_dollars: "0.5600"`, `no_price_dollars: "0.4400"`). Legacy integer cent fields (`yes_price: 56`) still accepted but deprecated. Subpenny pricing supported on some markets |
| **Quantity Format** | `count_fp` strings (`"10.00"`) for fractional contracts. Integer `count` still accepted. If both provided, must match |
| **Rate Limits** | Basic: 20 read/10 write per sec. Batch cancel: each cancel = 0.2 writes. 429 response requires exponential backoff |
| **Idempotency** | `client_order_id` field -- use to prevent duplicate orders on network retry |
| **Order Statuses** | `resting` (on book), `executed` (filled), `canceled`, `pending` (being removed from enum) |
| **WebSocket** | `wss://trading-api.kalshi.com/trade-api/ws/v2` -- orderbook_delta, ticker, trade, fill channels. Auth via headers |
| **Sandbox** | `demo-api.kalshi.co` -- separate API keys, fake money, real market structure. **Use this first** |
| **Settlement** | Centralized, typically hours after event determination. CFTC-regulated |
| **Fee Structure** | Taker fee on expected earnings. Quadratic fee function. Fee shown in response via `taker_fees_dollars` |

**BREAKING CHANGE (current codebase):** The execution engine at line 819 uses `price_cents = int(round(price * 100))` and sends integer `yes_price`/`no_price`. This must migrate to `yes_price_dollars`/`no_price_dollars` fixed-point strings. The `count` field should migrate to `count_fp`.

### Polymarket CLOB API

**Confidence: HIGH** (verified against docs.polymarket.com, April 2026)

| Aspect | Detail |
|--------|--------|
| **Auth** | Two-tier: L1 (wallet private key signs EIP-712 for credential derivation), L2 (HMAC-SHA256 with derived apiKey/secret/passphrase). Orders additionally require L1 wallet signature |
| **Signature Types** | `0` = EOA (standard wallet), `1` = POLY_PROXY (Magic Link), `2` = GNOSIS_SAFE (multisig). Most bots use `0` |
| **Order Types** | GTC (Good-Til-Cancelled), GTD (Good-Til-Date), FOK (Fill-Or-Kill), FAK (Fill-And-Kill). Post-only available for GTC/GTD |
| **Heartbeat** | **CRITICAL**: Must send heartbeat every 5 seconds. 10-second timeout (with 5s buffer) = **all open orders auto-cancelled**. Use session ID from response |
| **Price Conformance** | Prices must conform to market tick size (0.1 down to 0.0001). Rejected otherwise |
| **Batch Limits** | Max 15 orders per batch submission |
| **Order Lifecycle** | Pre-match: `live` / `matched` / `delayed` (sports, 1s delay) / `unmatched`. Post-match: `MATCHED` -> `MINED` -> `CONFIRMED` (or `RETRYING` -> `FAILED`) |
| **WebSocket** | `wss://ws-subscriptions-clob.polymarket.com/ws/market` -- public, no auth. PING every 10s. Orderbook snapshots, price changes, trades |
| **Settlement** | On-chain via UMA oracle. Minimum 2 hours uncontested; days/weeks if disputed |
| **Maker Fees** | 0% (as of March 2026). Taker fees apply |
| **US Access** | Polymarket US (polymarket.us) launched Dec 2025, CFTC-regulated, separate from international. Verify which platform the user's account is on |

**ARCHITECTURAL IMPLICATION:** The heartbeat mechanism means the system needs a dedicated async task running alongside the execution engine. If the heartbeat task dies, Polymarket auto-cancels all open orders. This is a safety feature (dead man's switch) but requires robust heartbeat management.

### PredictIt API

**Confidence: HIGH** (verified -- there is no trading API)

| Aspect | Detail |
|--------|--------|
| **Read API** | `https://www.predictit.org/api/marketdata/all/` -- public, no auth, JSON. Returns all markets with best bid/ask/last price |
| **Trading API** | **Does not exist**. All trades must be placed through the web UI manually |
| **Implication** | PredictIt legs are always manual. System detects the opportunity, sends Telegram alert with instructions, operator places the trade manually |
| **Platform Status** | Won approval to expand as regulated exchange (Sep 2025). User's account is active. Cap of $850/contract per market |
| **Fees** | 10% profit fee + 5% withdrawal fee. Applied on settlement, not on trade |

**ARCHITECTURAL IMPLICATION:** Any arbitrage involving PredictIt is inherently semi-manual. The system should detect and alert, but execution is operator-driven. The current codebase already handles this with the `ManualPosition` workflow -- this is correct.

## Recommended Project Structure

The existing structure is sound. Key production additions needed:

```
arbiter/
├── collectors/            # Price collection per platform (existing)
│   ├── kalshi.py          # REST + WebSocket (upgrade needed)
│   ├── polymarket.py      # Gamma + CLOB + WebSocket (existing)
│   └── predictit.py       # REST polling (existing)
├── execution/             # Order lifecycle (existing)
│   ├── engine.py          # Core execution logic (needs API fixes)
│   ├── adapters/          # NEW: Platform-specific order adapters
│   │   ├── kalshi.py      # Kalshi order placement with fixed-point pricing
│   │   ├── polymarket.py  # Polymarket CLOB with heartbeat management
│   │   └── predictit.py   # Manual workflow only
│   └── recovery.py        # NEW: Partial fill / one-leg recovery logic
├── safety/                # NEW: Production safety mechanisms
│   ├── kill_switch.py     # Redis-backed global halt
│   ├── heartbeat.py       # Polymarket heartbeat manager
│   └── limits.py          # Position/exposure/daily loss limits
├── scanner/               # Arbitrage detection (existing)
├── monitor/               # Balance & portfolio monitoring (existing)
├── audit/                 # Math auditing & reconciliation (existing)
├── config/                # Configuration (existing)
├── api.py                 # Dashboard API (existing)
└── main.py                # Entry point (existing)
```

### Structure Rationale

- **adapters/**: Platform-specific order placement is currently embedded in engine.py (lines 801-984). Extract into adapters to isolate platform API changes (like Kalshi's pricing migration) from execution logic.
- **safety/**: Kill switch, heartbeat management, and limit enforcement are cross-cutting production concerns that don't belong in any single layer.
- **recovery.py**: One-leg risk recovery is the hardest production problem. It deserves its own module with thorough testing rather than being a method on ExecutionEngine.

## Architectural Patterns

### Pattern 1: Dead Man's Switch (Polymarket Heartbeat)

**What:** A dedicated async task sends heartbeats to Polymarket every 5 seconds. If the task dies or the system crashes, Polymarket auto-cancels all open orders within 10 seconds. This prevents "zombie orders" that sit on the book with no monitoring system.

**When to use:** Always when Polymarket orders are live. The heartbeat task must start before any Polymarket orders are placed and stop only after all Polymarket orders are confirmed filled or cancelled.

**Trade-offs:**
- Pro: Automatic safety net -- crash = no lingering exposure
- Con: Adds a failure mode -- heartbeat task crash cancels orders even during healthy operation
- Con: Requires careful lifecycle management -- start/stop timing matters

**Example:**
```python
class PolymarketHeartbeatManager:
    def __init__(self, clob_client):
        self._client = clob_client
        self._running = False
        self._heartbeat_id = None
        self._last_success = 0.0

    async def run(self):
        """Must be started as asyncio.create_task() before any order placement."""
        self._running = True
        while self._running:
            try:
                resp = self._client.post_heartbeat(self._heartbeat_id)
                self._heartbeat_id = resp.get("heartbeat_id")
                self._last_success = time.time()
            except Exception as e:
                logger.error("Heartbeat failed: %s", e)
                # If heartbeat fails, we have ~10s before auto-cancel
                # Alert operator immediately
            await asyncio.sleep(4.5)  # Slight buffer under 5s

    @property
    def is_healthy(self) -> bool:
        return time.time() - self._last_success < 8.0  # 8s < 10s timeout
```

### Pattern 2: Pre-Trade Re-Quote with FOK Orders

**What:** Before executing, re-fetch current prices to verify the opportunity still exists. Then use FOK (Fill-Or-Kill) orders on both legs to ensure full fills or nothing.

**When to use:** Every live trade execution. The existing codebase already has re-quote checks (good). The missing piece is using FOK order types instead of resting limit orders.

**Trade-offs:**
- Pro: FOK eliminates partial fill risk entirely -- order fills completely or cancels
- Pro: Avoids the complexity of monitoring resting orders
- Con: FOK orders may fail more often (opportunity must have sufficient depth)
- Con: Kalshi requires `time_in_force: "fill_or_kill"` on limit orders since they removed market orders

**Architecture decision:** For sub-$1K capital with small position sizes, FOK is the right choice. Resting limit orders are for market makers, not arbitrageurs. Resting orders create monitoring complexity and exposure window that is not worth it at this scale.

### Pattern 3: Kill Switch with Redis Flag

**What:** A boolean flag in Redis (`arbiter:kill_switch`) checked before every execution attempt. When set to `true`, no new orders are placed. Optionally cancels all pending orders.

**When to use:** Production systems must have an emergency stop. Three trigger paths:
1. **Automatic**: Daily loss limit, consecutive failures, balance discrepancy
2. **Dashboard**: Manual toggle button
3. **Telegram**: `/kill` command for remote emergency stop

**Trade-offs:**
- Pro: Sub-millisecond check on every execution cycle
- Pro: Survives process restart (persisted in Redis)
- Con: Requires Redis to be available (but system already depends on Redis)

**Example:**
```python
class KillSwitch:
    KEY = "arbiter:kill_switch"
    REASON_KEY = "arbiter:kill_switch_reason"

    def __init__(self, redis_client):
        self._redis = redis_client

    async def is_active(self) -> bool:
        val = await self._redis.get(self.KEY)
        return val == b"1"

    async def activate(self, reason: str):
        await self._redis.set(self.KEY, "1")
        await self._redis.set(self.REASON_KEY, reason)
        logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    async def deactivate(self):
        await self._redis.delete(self.KEY, self.REASON_KEY)
        logger.warning("Kill switch deactivated")
```

### Pattern 4: Platform Adapter Abstraction

**What:** Each platform gets an adapter class that translates generic order intents into platform-specific API calls. The execution engine works with a uniform interface.

**When to use:** Mandatory for production. The current codebase mixes platform-specific API logic directly in engine.py, which means a Kalshi API change (like the pricing migration) requires editing the execution engine itself.

**Trade-offs:**
- Pro: Platform changes isolated to one file
- Pro: Testable -- mock the adapter, test execution logic independently
- Pro: Makes adding new platforms straightforward
- Con: Additional abstraction layer -- slight complexity increase

## Data Flow

### Live Trading Flow (Production)

```
[Platform APIs] ──(prices)──> [Collectors] ──(pub/sub)──> [Price Store]
                                                              │
                                                    ┌────────┘
                                                    v
                              [Scanner] <── price update notification
                                  │
                                  │ (opportunity detected, persistence gated)
                                  v
                           [Execution Engine]
                                  │
                     ┌────────────┼────────────────┐
                     v            v                 v
              [Kill Switch   [Pre-Trade        [Risk Manager
               check]        Re-Quote]          check]
                     │            │                 │
                     └────────────┼────────────────┘
                                  │ (all checks pass)
                                  v
                     ┌────────────┼────────────────┐
                     v                             v
              [Kalshi Adapter]              [Polymarket Adapter]
              POST /portfolio/orders        create_and_post_order()
              FOK limit order               FOK order
              Fixed-point pricing           Tick-size conformance
                     │                             │
                     v                             v
              [Fill Confirmed?]             [Fill Confirmed?]
                     │                             │
                     └────────────┬────────────────┘
                                  │
                          ┌───────┼───────┐
                          v               v
                     [Both Filled]   [One Failed]
                     Record trade    Cancel unfilled leg
                     Update P&L     Log incident
                     Update risk    Alert operator
                                    Update risk
```

### Order Execution Sequence (Critical Path)

```
1. Opportunity arrives from scanner
2. Kill switch check (Redis GET, <1ms)
3. Risk manager check (in-memory, <1ms)
4. Pre-trade re-quote (Redis GET x2, <2ms)
5. Math audit (in-memory, <1ms)
6. Balance verification (cached, <1ms)
7. Concurrent order submission:
   a. Kalshi: POST /portfolio/orders (FOK) ──> ~50-200ms
   b. Polymarket: create_and_post_order (FOK) ──> ~100-500ms
8. Await both responses (asyncio.gather)
9. If both filled: record trade, update P&L
10. If one failed: cancel unfilled leg, record incident, alert operator
11. Broadcast execution state to dashboard via WebSocket
```

### State Management

| State Category | Storage | Lifetime | Recovery |
|----------------|---------|----------|----------|
| Kill switch flag | Redis | Persistent until cleared | Survives restart |
| Quote cache | Redis (30s TTL) | Ephemeral | Re-collected on next poll |
| Open orders | In-memory + platform API | During trade lifetime | Query platform API on restart |
| Execution history | PostgreSQL | Permanent | Survives restart |
| Positions | PostgreSQL | Until settlement | Survives restart |
| Incidents | PostgreSQL + in-memory deque | Permanent | Survives restart |
| Heartbeat session | In-memory | During runtime | Re-establish on restart |
| Daily P&L/trade counts | In-memory | Until reset | Lost on restart (acceptable) |

## Scaling Considerations

| Scale | Architecture Adjustments |
|-------|--------------------------|
| **$1K/platform (current)** | Single process, FOK orders, manual PredictIt. No scaling needed. Focus entirely on correctness and safety |
| **$10K/platform** | Same architecture. Add position-level hedging for partial fills. Consider GTC limit orders with monitoring for better fills |
| **$100K+ total** | WebSocket price feeds instead of polling (lower latency). Kalshi Advanced/Premier tier (30+ writes/sec). Consider separate process for monitoring vs execution |

### Scaling Priorities

1. **First bottleneck:** Price freshness. REST polling every 10-30s misses fast-moving opportunities. WebSocket feeds from both Kalshi and Polymarket provide sub-second updates.
2. **Second bottleneck:** Execution speed. At scale, the 50-500ms order submission time becomes limiting. Kalshi's Premier tier (100 writes/sec) and Polymarket's batch orders (15/batch) help.
3. **Third bottleneck:** Recovery complexity. More capital = more risk from partial fills. Need automated unwinding rather than manual intervention.

## Anti-Patterns

### Anti-Pattern 1: Resting Limit Orders for Arbitrage

**What people do:** Place GTC limit orders on both legs and wait for fills, thinking they'll get better execution prices.
**Why it's wrong:** Creates a monitoring burden -- you must track each order, handle partial fills, detect when the opportunity disappears, and cancel unfilled orders. With two platforms, the state space explodes. One leg fills and the other doesn't? Now you have unhedged directional exposure.
**Do this instead:** Use FOK (Fill-Or-Kill) orders for both legs. Either both fill completely at the expected price, or neither does. No monitoring, no partial fills, no dangling exposure. At small capital sizes, the slightly worse fill price is worth the dramatic simplification.

### Anti-Pattern 2: Fire-and-Forget Order Submission

**What people do:** Submit orders to platform APIs without verifying the response, assuming success.
**Why it's wrong:** Network timeouts, 429 rate limits, and transient errors mean the order state is ambiguous. Did it submit? Did it partially fill before failing? You don't know, and now you might have untracked exposure.
**Do this instead:** Always parse the full response. On timeout or ambiguous failure, query the platform for order status using `client_order_id` (Kalshi) before retrying. Never retry blindly -- you risk duplicate orders.

### Anti-Pattern 3: Shared Session Without Heartbeat

**What people do:** Initialize the Polymarket CLOB client once and use it for the entire process lifetime without managing heartbeats.
**Why it's wrong:** Polymarket cancels ALL open orders if no heartbeat received in 10 seconds. Your orders vanish silently.
**Do this instead:** Start a dedicated heartbeat task before placing any orders. Monitor heartbeat health. If heartbeat fails, halt new Polymarket order placement immediately and alert the operator.

### Anti-Pattern 4: Using Integer Cents for Kalshi Pricing

**What people do:** Send `yes_price: 56` (integer cents) to Kalshi's order API.
**Why it's wrong:** Kalshi's fixed-point migration (March 2026) means some markets use subpenny pricing. Integer cents lose precision. The legacy fields still work but are deprecated and will eventually be removed.
**Do this instead:** Use `yes_price_dollars: "0.5600"` and `count_fp: "10.00"`. Parse responses using `*_dollars` and `*_fp` fields.

## Integration Points

### External Services

| Service | Integration Pattern | Critical Gotchas |
|---------|---------------------|-------------------|
| **Kalshi REST API** | RSA-PSS signed requests, limit orders only | No market orders (removed Feb 2026). Must migrate to fixed-point pricing. Rate limit: 10 writes/sec on Basic tier. Demo environment at `demo-api.kalshi.co` for testing |
| **Kalshi WebSocket** | `wss://trading-api.kalshi.com/trade-api/ws/v2`, auth via headers | Session limits per API tier (default 200). Provides orderbook_delta, fill, and ticker channels |
| **Polymarket CLOB** | py-clob-client v0.34.6, EIP-712 + HMAC-SHA256 | Heartbeat every 5s or all orders cancelled. L1 signing for orders, L2 for cancel/query. Tick size conformance required. Verify US vs international endpoint |
| **Polymarket WebSocket** | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Public, no auth. PING every 10s. Separate from CLOB heartbeat |
| **PredictIt** | HTTP GET, no auth | Read-only. No trading API. $850/contract cap per market |
| **PostgreSQL** | asyncpg, connection pooling | Store execution history, positions, incidents. Must survive process restart |
| **Redis** | redis[hiredis], 256MB LRU | Kill switch flag, quote cache (30s TTL), pub/sub event fan-out |
| **Telegram** | Bot API for alerts | Kill switch commands, execution alerts, manual trade instructions |

### Internal Boundaries

| Boundary | Communication | Key Considerations |
|----------|---------------|-------------------|
| Collector -> Price Store | Direct async write + pub/sub | Collectors must handle platform API failures without crashing. Circuit breaker pattern (existing) |
| Price Store -> Scanner | Pub/sub notification or timer | Scanner must tolerate stale/missing quotes. Never scan with quote older than TTL |
| Scanner -> Execution Engine | Async queue (existing) | Queue must not block scanner. Backpressure: drop opportunities if execution is busy |
| Execution Engine -> Platform Adapters | Direct async call | Adapters are the ONLY code that talks to platform APIs for orders. Engine never constructs raw API requests |
| Execution Engine -> Recovery Handler | Direct async call | Recovery must be idempotent -- calling it twice for the same execution must not create duplicate orders or double-cancel |
| Kill Switch -> Execution Engine | Redis flag check | Checked synchronously before every execution attempt. Must be faster than the execution path |
| Heartbeat Manager -> Polymarket | Dedicated async task | Independent of execution -- runs whether or not trades are active. Must outlive individual trade cycles |

## Settlement Divergence Risk

**This is the most dangerous architectural concern for cross-platform arbitrage.**

Kalshi and Polymarket may resolve what appears to be the same event with **different outcomes**. Kalshi uses CFTC-regulated source agencies. Polymarket uses the UMA oracle (decentralized). Resolution criteria can differ in edge cases (e.g., "Did candidate X win?" where the definition of "win" differs).

**Architectural mitigation:**
1. Only trade on markets with clear, binary, unambiguous resolution criteria
2. Maintain a `settlement_risk` flag on market mappings -- HIGH for markets with subjective resolution
3. Require larger edge (>15 cents as suggested by real-world implementations) for cross-platform hedges to buffer resolution risk
4. Monitor settlement status on both platforms -- alert immediately if one resolves while the other disputes

## Build Order (Dependencies)

The following ordering respects technical dependencies and safety-first principles:

```
Phase 1: Safety Infrastructure
  ├── Kill switch (Redis flag + dashboard toggle)
  ├── Kalshi fixed-point pricing migration
  └── Platform adapter extraction from engine.py
       Dependencies: None. Foundation for everything else.

Phase 2: Sandbox Validation
  ├── Kalshi demo environment integration testing
  ├── Polymarket testnet/small-amount testing
  ├── Heartbeat manager implementation
  └── FOK order type usage for both platforms
       Dependencies: Phase 1 (correct pricing, adapter abstraction)

Phase 3: Live Execution (Small Capital)
  ├── Live order placement with real money ($10-50 trades)
  ├── Partial fill recovery testing
  ├── Balance reconciliation against live data
  └── End-to-end monitoring (collector -> scanner -> executor -> dashboard)
       Dependencies: Phase 2 (validated adapters, heartbeat working)

Phase 4: Production Hardening
  ├── WebSocket price feeds (replace polling for lower latency)
  ├── Automated kill switch triggers (daily loss, consecutive failures)
  ├── Telegram kill switch command
  └── Position-level settlement divergence monitoring
       Dependencies: Phase 3 (proven live execution)
```

## What Differs From Prototype to Production

| Concern | Prototype (Current) | Production (Needed) |
|---------|---------------------|---------------------|
| **Pricing format** | Integer cents (`yes_price: 56`) | Fixed-point dollars (`"0.5600"`) |
| **Order types** | Assumes market orders exist | FOK limit orders only (Kalshi removed market orders) |
| **Heartbeat** | Not implemented | Mandatory for Polymarket -- 5s interval or orders cancelled |
| **Kill switch** | Not implemented | Redis flag + dashboard + Telegram triggers |
| **Recovery** | Basic cancel attempt | Idempotent recovery with platform status query |
| **Balance check** | Cached from periodic poll | Pre-trade verification against live API |
| **Price freshness** | REST polling 10-30s | WebSocket for sub-second updates (Phase 4) |
| **Idempotency** | No duplicate prevention | `client_order_id` on every Kalshi order; dedup on retry |
| **PredictIt** | Attempted auto-execution | Explicit manual-only workflow (correct in current code) |

## Sources

- [Kalshi API Documentation](https://docs.kalshi.com/) -- Create Order, Fixed-Point Migration, Rate Limits, WebSocket
- [Kalshi API Changelog](https://docs.kalshi.com/changelog) -- Breaking changes 2025-2026
- [Kalshi Demo Environment](https://docs.kalshi.com/getting_started/demo_env) -- Sandbox testing
- [Polymarket CLOB Documentation](https://docs.polymarket.com/developers/CLOB/orders/create-order) -- Order creation
- [Polymarket Authentication](https://docs.polymarket.com/developers/CLOB/authentication) -- L1/L2 auth flow
- [Polymarket Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle) -- State transitions
- [Polymarket Heartbeat](https://docs.polymarket.com/api-reference/trade/send-heartbeat) -- Session management
- [Polymarket WebSocket](https://docs.polymarket.com/market-data/websocket/overview) -- Real-time data
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client) -- Python SDK v0.34.6
- [PredictIt API](https://www.predictit.org/api/marketdata/all/) -- Read-only market data
- [Building a Prediction Market Arbitrage Bot](https://navnoorbawa.substack.com/p/building-a-prediction-market-arbitrage) -- Real-world architecture patterns
- [How Prediction Market Arbitrage Works](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works) -- Cross-platform mechanics
- [How Kalshi and Polymarket Settle Markets](https://defirate.com/prediction-markets/how-contracts-settle/) -- Settlement divergence risk
- [Polymarket US Launch](https://www.quantvps.com/blog/polymarket-us-api-available) -- US platform status

---
*Architecture research for: Production prediction market arbitrage system*
*Researched: 2026-04-16*
