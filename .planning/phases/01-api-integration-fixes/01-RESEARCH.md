# Phase 1: API Integration Fixes - Research

**Researched:** 2026-04-16
**Domain:** Platform API integration (Kalshi, Polymarket, PredictIt)
**Confidence:** HIGH

## Summary

Phase 1 fixes all hard-blocker API issues preventing the arbiter from operating against live platform APIs. There are six distinct work areas: (1) migrating Kalshi order payloads from legacy integer cents to fixed-point dollar strings, (2) fixing the Polymarket ClobClient initialization with `signature_type` and `funder` parameters, (3) implementing a dedicated Polymarket heartbeat manager, (4) correcting fee calculations across all platforms to match current official schedules, (5) stripping all PredictIt execution code while keeping the read-only collector, and (6) verifying all three collectors against live API responses.

Critical finding: The Polymarket fee rates hardcoded in both `settings.py` and `math_auditor.py` are significantly wrong. The code uses 0.02 for politics and 0.015 for crypto, but the actual current rates from official Polymarket docs are 0.04 for politics and 0.072 for crypto. This means every arbitrage opportunity calculation involving Polymarket has been underestimating fees by 50-400%, potentially showing phantom profits. The Kalshi taker fee rate (0.07) and formula are confirmed correct.

**Primary recommendation:** Fix fee rates first (they affect all downstream math), then Kalshi pricing format, then Polymarket auth/heartbeat, then PredictIt stripping, then collector verification against live APIs.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: User is US-based with an active Polymarket account that has API access
- D-02: Use `py-clob-client` SDK with updated ClobClient init -- add `signature_type` and `funder` parameters
- D-03: Target the CLOB API endpoint the user's account has access to -- verify correct base URL, chain_id, and auth method against live credentials
- D-04: Polymarket heartbeat manager (API-03) must run as a dedicated async task sending keepalive every 5 seconds to the CLOB connection -- separate from the dashboard WebSocket heartbeat in `api.py:595`
- D-05: Verify all three collectors with live API calls using real credentials -- read-only calls (price/market fetching) are safe, no money at risk
- D-06: Push for fastest refresh possible within each platform's rate limits -- second-by-second monitoring is the goal
- D-07: Use WebSocket feeds where available (Polymarket WS already has circuit breaker in `polymarket.py:49`), fall back to fast REST polling within rate limits
- D-08: Validate response schema and field names match what the code expects -- any mismatch is a fix target
- D-09: Fetch fee rates dynamically from each platform's SDK/API at startup -- not hardcoded
- D-10: Fall back to hardcoded documented rates if dynamic fetch fails, with a warning log so stale fees are visible
- D-11: Hardcoded fallback values: Polymarket per-category (crypto 0.072, sports 0.03, politics 0.04, geopolitics 0.0), Kalshi per current schedule, PredictIt profit fee 10% + withdrawal fee 5%
- D-12: Strip all PredictIt execution code entirely -- remove order submission, fill handling, and workflow stubs from `engine.py` and `workflow/predictit_workflow.py`
- D-13: Keep only the PredictIt collector (`arbiter/collectors/predictit.py`) for read-only price signals
- D-14: Clean break with no dead code -- no disabled flags, no execution stubs
- D-15: Migrate Kalshi order payload from integer cents `yes_price` (current: `engine.py:829`) to `yes_price_dollars` string format per March 2026 API migration
- D-16: Add `count_fp` support for fractional markets

### Claude's Discretion
- Exact error handling approach for failed API calls during verification
- Internal refactoring of fee function signatures to support dynamic rate injection
- Test structure for unit tests verifying fee calculations against real rate values

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| API-01 | Kalshi order submission uses fixed-point dollar string pricing (`yes_price_dollars: "0.56"`) | Kalshi fixed-point migration docs confirm `yes_price_dollars` string format with up to 6 decimal places, and `count_fp` for fractional markets. Legacy integer fields removed March 12, 2026. |
| API-02 | Polymarket ClobClient initialized with correct `signature_type` and `funder` parameters | py-clob-client 0.34.6 confirms constructor accepts `signature_type` (int) and `funder` (str). For US proxy wallets, `signature_type=2` (GNOSIS_SAFE) with funder as the proxy wallet address. |
| API-03 | Polymarket heartbeat manager sends keepalive every 5 seconds | ClobClient has `post_heartbeat(heartbeat_id)` method. Endpoint is POST `/v1/heartbeats`. Server cancels all orders if no heartbeat received within 10s (5s buffer). |
| API-04 | Fee calculations use correct platform-specific rates | Current verified rates: Polymarket crypto=0.072, sports=0.03, politics=0.04, geopolitics=0.0. Kalshi taker=0.07 quadratic. PredictIt profit=10% + withdrawal=5%. Existing code rates are WRONG for Polymarket. |
| API-05 | PredictIt scoped to read-only price signal | PredictIt API is operational (public JSON endpoint, no auth). Execution code exists in `engine.py` (manual workflow) and `workflow/predictit_workflow.py` (entire file). Must strip both. |
| API-06 | Polymarket platform decision resolved (US) with correct SDK, endpoints, and auth | User is US-based. py-clob-client 0.34.6 installed. Host: `https://clob.polymarket.com`. Chain ID: 137. Signature types: 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE. |
| API-07 | All platform collectors verified against current API responses | Kalshi collector reads dollar fields already (fallback chain includes `yes_bid_dollars`). Polymarket collector uses Gamma API for discovery + CLOB for books. PredictIt uses public JSON endpoint. All need live verification. |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Kalshi order format migration | API / Backend | -- | Order construction happens in `engine.py` server-side |
| Polymarket ClobClient init | API / Backend | -- | SDK initialization in `engine.py` server-side |
| Polymarket heartbeat | API / Backend | -- | Async task in Python backend, not browser |
| Fee calculation | API / Backend | -- | Fee math in `settings.py` and `math_auditor.py`, used by scanner |
| PredictIt code removal | API / Backend | -- | Removing execution paths from `engine.py` and `workflow/` |
| Collector verification | API / Backend | -- | All collectors are Python async tasks in `collectors/` |
| Dynamic fee fetching | API / Backend | -- | Fetching from platform SDKs/APIs at startup |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| py-clob-client | 0.34.6 | Polymarket CLOB order management, heartbeat, fee lookup | Official Polymarket SDK, already installed [VERIFIED: pip show py-clob-client] |
| aiohttp | 3.9.0+ | Async HTTP client/server for API calls | Already used throughout the project [VERIFIED: requirements.txt] |
| cryptography | 44.0.0 | Kalshi RSA-PSS signature auth | Already installed and used for Kalshi auth [VERIFIED: pip show cryptography] |
| web3 | 7.15.0 | Polygon USDC balance lookup for Polymarket | Already installed for balance checking [VERIFIED: pip show web3] |
| pytest | 8.3.4 | Test runner for fee calculation and API tests | Already configured with conftest.py [VERIFIED: pytest --collect-only] |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| python-dotenv | 1.0.0+ | .env loading for API keys | Already used via `settings.py` |
| asyncpg | 0.29.0+ | Async PostgreSQL (not needed this phase) | Phase 2+ for execution state persistence |

**Installation:**
No new packages needed. All dependencies are already installed.

## Architecture Patterns

### System Architecture Diagram (Phase 1 Scope)

```
                        PLATFORM APIs
                 +---------+-----------+---------+
                 |         |           |         |
              Kalshi    Polymarket   PredictIt
              REST v2   Gamma+CLOB   Public JSON
                 |         |           |
                 v         v           v
         [Collectors] (fetch prices, build PricePoints)
              kalshi.py  polymarket.py  predictit.py
                 |         |           |
                 +----+----+-----------+
                      |
                      v
              [PriceStore] (in-memory + Redis TTL)
                      |
                      v
              [ArbitrageScanner] (compute_fee() -> detect opportunities)
                      |
                      v
              [ExecutionEngine] (submit orders via platform APIs)
                   |       |
           +-------+       +-------+
           v                       v
    Kalshi Orders            Polymarket Orders
    (yes_price_dollars)      (ClobClient + heartbeat)
```

**Phase 1 touches:**
- Collectors: verify schema against live responses
- Fee functions in `settings.py`: fix rates, add dynamic fetch
- Fee functions in `math_auditor.py`: fix shadow calculator rates
- ExecutionEngine: fix Kalshi order format, fix Polymarket ClobClient init
- New: Polymarket heartbeat async task
- Remove: PredictIt execution code from engine.py + workflow/

### Pattern 1: Kalshi Fixed-Point Dollar String Pricing
**What:** Migrate order payload from `yes_price: <int_cents>` to `yes_price_dollars: "<string>"` and `count` to `count_fp: "<string>"`
**When to use:** All Kalshi order submissions
**Example:**
```python
# Source: https://docs.kalshi.com/api-reference/orders/create-order
# BEFORE (broken -- legacy fields removed March 12, 2026):
order_body = {
    "ticker": market_id,
    "action": "buy",
    "side": "yes",
    "type": "limit",
    "count": qty,
    "yes_price": int(round(price * 100)),  # integer cents
}

# AFTER (current API):
order_body = {
    "ticker": market_id,
    "client_order_id": client_order_id,
    "action": "buy",
    "side": side,
    "type": "limit",
    "count_fp": f"{qty:.2f}",              # string, 2 decimal places
}
if side == "yes":
    order_body["yes_price_dollars"] = f"{price:.4f}"   # string, up to 6 dp
else:
    order_body["no_price_dollars"] = f"{price:.4f}"
```
[VERIFIED: https://docs.kalshi.com/getting_started/fixed_point_migration]

### Pattern 2: Polymarket ClobClient Initialization with Auth
**What:** Initialize ClobClient with `signature_type` and `funder` for authenticated order placement
**When to use:** Any authenticated Polymarket operation (orders, heartbeat, API key management)
**Example:**
```python
# Source: https://github.com/Polymarket/py-clob-client README
from py_clob_client.client import ClobClient

# For US users with proxy wallet (GNOSIS_SAFE = 2):
client = ClobClient(
    host="https://clob.polymarket.com",
    key=private_key,               # wallet private key
    chain_id=137,                  # Polygon mainnet
    signature_type=2,              # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
    funder=proxy_wallet_address,   # address displayed on polymarket.com
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

# Verify auth:
api_keys = client.get_api_keys()  # Requires L2 auth, will 401 if broken
```
[VERIFIED: py-clob-client 0.34.6 source inspection via `inspect.getsource(ClobClient.__init__)`]

### Pattern 3: Polymarket Heartbeat Task
**What:** Dedicated async task sending heartbeat every 5 seconds to prevent order auto-cancellation
**When to use:** Whenever the system has open orders on Polymarket
**Example:**
```python
# Source: https://docs.polymarket.com/api-reference/trade/send-heartbeat
# ClobClient method: post_heartbeat(heartbeat_id: Optional[str])
# Endpoint: POST /v1/heartbeats with body {"heartbeat_id": heartbeat_id}
# Server cancels ALL open orders if no heartbeat within 10s (5s buffer)

async def heartbeat_loop(client: ClobClient):
    heartbeat_id = None  # empty string for first request
    while True:
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.post_heartbeat(heartbeat_id)
            )
            # Server returns new heartbeat_id to use in next request
            if isinstance(response, dict):
                heartbeat_id = response.get("heartbeat_id", heartbeat_id)
        except Exception as exc:
            logger.error("Heartbeat failed: %s", exc)
        await asyncio.sleep(5)  # 5s interval, well within 10s timeout
```
[VERIFIED: py-clob-client source: `ClobClient.post_heartbeat` signature and POST_HEARTBEAT = "/v1/heartbeats"]

### Pattern 4: Dynamic Fee Rate Fetching
**What:** Fetch per-token fee rates from Polymarket SDK at startup, fall back to hardcoded rates
**When to use:** During collector market discovery and before fee calculations
**Example:**
```python
# Source: py-clob-client source inspection
# ClobClient.get_fee_rate_bps(token_id: str) -> int
# Endpoint: GET /fee-rate?token_id=<token_id>
# Returns: {"base_fee": <int_bps>}  (basis points)

def get_polymarket_fee_rate(client: ClobClient, token_id: str) -> float:
    """Fetch fee rate as a decimal from Polymarket, with fallback."""
    FALLBACK_RATES = {
        "crypto": 0.072,
        "sports": 0.03,
        "politics": 0.04,
        "finance": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "weather": 0.05,
        "tech": 0.04,
        "mentions": 0.04,
        "geopolitics": 0.0,
        "default": 0.05,
    }
    try:
        bps = client.get_fee_rate_bps(token_id)
        return bps / 10000.0  # Convert basis points to decimal
    except Exception:
        logger.warning("Fee rate fetch failed for %s, using fallback", token_id)
        return FALLBACK_RATES.get(category, FALLBACK_RATES["default"])
```
[VERIFIED: py-clob-client source: `ClobClient.get_fee_rate_bps` inspected via `inspect.getsource`]

### Anti-Patterns to Avoid
- **Hardcoding fee rates without fallback logging:** The system previously had wrong rates with no indication they were stale. Always log when using fallback rates.
- **Integer cents for Kalshi prices:** Legacy format removed March 12, 2026. Will cause 400/422 errors.
- **Missing signature_type/funder on ClobClient:** Results in 401 errors on any authenticated endpoint.
- **Using the dashboard WebSocket heartbeat for CLOB keepalive:** These are completely separate systems -- the dashboard WS heartbeat in `api.py:595` keeps the browser dashboard connection alive. The CLOB heartbeat keeps trading session alive and prevents order cancellation.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Polymarket order signing | Custom EIP-712 signature logic | `py-clob-client`'s `OrderBuilder` with `sig_type` | Signature type handling is complex and version-dependent |
| Polymarket fee lookup | Scraping fee schedule web pages | `ClobClient.get_fee_rate_bps(token_id)` | Returns per-token basis points from official API |
| Heartbeat ID tracking | Custom session state management | `ClobClient.post_heartbeat(heartbeat_id)` | SDK handles serialization and L2 header auth |
| Kalshi RSA-PSS auth | Manual signing implementation | Existing `KalshiAuth` class (already correct) | Already verified working in `kalshi.py` |

**Key insight:** The py-clob-client SDK at 0.34.6 already has all the methods needed for heartbeat, fee lookup, and authenticated trading. The main issue is that the ClobClient constructor call in `engine.py` is missing two critical parameters (`signature_type` and `funder`), not that the SDK lacks the capability.

## Common Pitfalls

### Pitfall 1: Polymarket Fee Rate Mismatch
**What goes wrong:** Arbitrage opportunities appear profitable but are actually losers because fee calculations use wrong rates.
**Why it happens:** Polymarket expanded fees to nearly all categories on March 30, 2026. The code has rates from before this expansion.
**How to avoid:** Update all hardcoded fallback rates to match current official schedule. Use `ClobClient.get_fee_rate_bps()` for dynamic lookup. Always verify the rate returned is in basis points (divide by 10000).
**Warning signs:** Opportunities consistently show 2-5 cent edges that evaporate after execution.

**Current code errors (must fix):**
```
settings.py line 47: KALSHI_TAKER_FEE_RATE = 0.07      # CORRECT
settings.py line 48: POLYMARKET_DEFAULT_TAKER_FEE_RATE = 0.02  # WRONG (was correct pre-March 2026)
settings.py line 96: "politics": 0.02   # WRONG, should be 0.04
settings.py line 97: "sports": 0.02     # WRONG, should be 0.03
settings.py line 98: "crypto": 0.015    # WRONG, should be 0.072
math_auditor.py line 48: rates = {"politics": 0.02, "sports": 0.02, "crypto": 0.015, "default": 0.02}  # ALL WRONG
```
[VERIFIED: https://docs.polymarket.com/trading/fees -- official fee schedule]

### Pitfall 2: Kalshi Price Format String Precision
**What goes wrong:** Order rejected with 400/422 because price string has wrong decimal places or is not properly formatted.
**Why it happens:** Kalshi `yes_price_dollars` accepts up to 6 decimal places, but tick sizes vary by market. A `tapered_deci_cent` market only allows $0.001 ticks in certain ranges.
**How to avoid:** Format prices with 4 decimal places (covers all tick sizes). Check market's `price_level_structure` field to validate tick alignment.
**Warning signs:** 400 errors with message about invalid price level.

### Pitfall 3: Heartbeat Race Condition
**What goes wrong:** Heartbeat task starts sending before ClobClient has L2 auth credentials, causing 401 errors that never recover.
**Why it happens:** `post_heartbeat()` calls `assert_level_2_auth()` internally. If creds are not set, it raises immediately.
**How to avoid:** Only start heartbeat task after `client.set_api_creds(creds)` completes successfully. Guard the heartbeat loop with a ready flag.
**Warning signs:** "Level 2 auth required" errors in logs during startup.

### Pitfall 4: Kalshi Demo vs Production Divergence
**What goes wrong:** Order passes on Kalshi demo environment but fails on production because demo still accepts legacy fields.
**Why it happens:** Kalshi demo may lag behind production API changes.
**How to avoid:** Test with both `yes_price_dollars` string format AND confirm legacy `yes_price` integer field is NOT sent (remove it entirely, don't keep both).
**Warning signs:** Orders working on demo but getting 400 on production.

### Pitfall 5: PredictIt Execution Code Scattered Across Files
**What goes wrong:** Incomplete removal leaves dead code that confuses future development or breaks at runtime.
**Why it happens:** PredictIt execution touches multiple files: `engine.py` (manual workflow queue, line 336-398), `workflow/predictit_workflow.py` (entire file, 431 lines), `test_predictit_workflow.py` (entire file), and `test_engine.py` (PredictIt-specific test cases).
**How to avoid:** Search for all `predictit` references in execution/workflow paths. The collector in `arbiter/collectors/predictit.py` stays. The `compute_predictit_total_fee` in `scanner/arbitrage.py` stays (needed for scanner fee math). Remove the execution and workflow code.
**Warning signs:** Import errors or unreachable code warnings after removal.

### Pitfall 6: Polymarket Signature Type Selection
**What goes wrong:** Using `signature_type=0` (EOA) when the user has a proxy wallet, or vice versa.
**Why it happens:** Polymarket has three different wallet types and the correct `signature_type` depends on how the user's account was created.
**How to avoid:** User decision D-01 confirms US-based account. Most US accounts use browser/proxy wallets (GNOSIS_SAFE = 2). The `funder` address is the proxy wallet address shown on polymarket.com. Add config fields for both and validate during startup.
**Warning signs:** 401 errors on `create_or_derive_api_creds()` or order placement.

## Code Examples

### Kalshi Order Payload Migration
```python
# Source: https://docs.kalshi.com/api-reference/orders/create-order
# Current broken code (engine.py:819-831):
#   price_cents = max(1, min(99, int(round(price * 100))))
#   order_body["yes_price"] = price_cents
#   order_body["count"] = qty

# Fixed code:
def _build_kalshi_order_body(
    market_id: str,
    client_order_id: str,
    side: str,
    price: float,
    qty: int | float,
) -> dict:
    order_body = {
        "ticker": market_id,
        "client_order_id": client_order_id,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count_fp": f"{float(qty):.2f}",
    }
    price_str = f"{price:.4f}"
    if side == "yes":
        order_body["yes_price_dollars"] = price_str
    else:
        order_body["no_price_dollars"] = price_str
    return order_body
```
[VERIFIED: https://docs.kalshi.com/getting_started/fixed_point_migration]

### Kalshi Response Parsing Update
```python
# Source: https://docs.kalshi.com/api-reference/orders/create-order
# Response now uses *_dollars and *_fp fields:
order_data = data.get("order", data)
fill_qty_str = order_data.get("fill_count_fp", "0.00")
fill_qty = float(fill_qty_str)

# Price from response is now a dollar string:
fill_price_str = order_data.get("yes_price_dollars") or order_data.get("no_price_dollars", "0.0000")
fill_price = float(fill_price_str)

# Fees from response:
taker_fees_str = order_data.get("taker_fees_dollars", "0.00")
taker_fees = float(taker_fees_str)
```
[VERIFIED: https://docs.kalshi.com/api-reference/orders/create-order -- response schema]

### Polymarket ClobClient Fix
```python
# Source: py-clob-client 0.34.6 source inspection
# Current broken code (engine.py:896-900):
#   ClobClient(host=..., key=..., chain_id=...)  # missing signature_type, funder

# Fixed code:
from py_clob_client.client import ClobClient

client = ClobClient(
    host=config.polymarket.clob_url,        # "https://clob.polymarket.com"
    key=config.polymarket.private_key,
    chain_id=config.polymarket.chain_id,    # 137
    signature_type=config.polymarket.signature_type,  # NEW: 0, 1, or 2
    funder=config.polymarket.funder,                  # NEW: proxy wallet address
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
```
[VERIFIED: `inspect.getsource(ClobClient.__init__)` -- confirmed parameters]

### Updated Polymarket Fee Fallback Rates
```python
# Source: https://docs.polymarket.com/trading/fees (verified 2026-04-16)
# Formula: fee = C * feeRate * p * (1 - p)
# Maker fees: always 0. Only takers pay.

POLYMARKET_FEE_RATES = {
    "crypto": 0.072,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "tech": 0.04,
    "mentions": 0.04,
    "geopolitics": 0.0,
    "default": 0.05,  # conservative fallback for unknown categories
}
```
[VERIFIED: https://docs.polymarket.com/trading/fees -- official fee schedule page]

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Kalshi `yes_price: <int>` cents | `yes_price_dollars: "<string>"` fixed-point | March 12, 2026 | Legacy fields REMOVED from all responses. Orders with `yes_price` will fail. |
| Kalshi `count: <int>` | `count_fp: "<string>"` fixed-point | March 12, 2026 | Required for fractional contract markets. Integer `count` still works for whole numbers but must match if both provided. |
| Polymarket 2 fee categories | 11 fee categories | March 30, 2026 | Nearly all categories now have fees. Crypto jumped from 0.015 to 0.072. |
| py-clob-client 0.25.0 | py-clob-client 0.34.6 | Feb 2026 | Added `post_heartbeat()`, `get_fee_rate_bps()`, builder_config support |
| PredictIt trading discussion | PredictIt read-only confirmed | Sep 2025 | CFTC full approval granted, but still no trading API -- read-only public JSON only |

**Deprecated/outdated:**
- `yes_price`/`no_price` integer cents fields in Kalshi API: Removed March 12, 2026 [VERIFIED: https://docs.kalshi.com/getting_started/fixed_point_migration]
- Polymarket 0.02 default taker fee: Replaced with per-category schedule, most categories 0.03-0.072 [VERIFIED: https://docs.polymarket.com/trading/fees]

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | US user should use `signature_type=2` (GNOSIS_SAFE) for Polymarket | Pattern 2 / Pitfall 6 | Auth will fail silently. User must confirm their wallet type -- could be 0 (EOA) if they imported a key directly. |
| A2 | Heartbeat response includes a `heartbeat_id` field to track for next call | Pattern 3 | May need to pass empty string every time if response format differs. Low risk -- initial heartbeat_id of None/empty is documented. |
| A3 | Polymarket `get_fee_rate_bps()` returns basis points that divide by 10000 for decimal rate | Pattern 4 | If it returns a decimal directly, fee calculations would be off by 10000x. Mitigated by the existing BPS-to-decimal conversion in `_extract_fee_rate`. |
| A4 | `PredictItWorkflowManager` in `workflow/predictit_workflow.py` can be entirely removed without breaking imports elsewhere | Pitfall 5 | If other modules import from it, removal will cause ImportError. Need to grep for all imports. |

## Open Questions (RESOLVED)

1. **What is the user's Polymarket signature_type?**
   - What we know: User is US-based with an active account (D-01). Most US accounts are GNOSIS_SAFE (2).
   - What's unclear: The exact wallet type -- could be EOA (0) if they imported a private key directly.
   - Recommendation: Add `POLY_SIGNATURE_TYPE` and `POLY_FUNDER` to `.env.template`. Default to 2, but validate at startup by attempting `create_or_derive_api_creds()`.
   - RESOLVED: Plan 04 Task 1 adds `POLY_SIGNATURE_TYPE` (default=2) and `POLY_FUNDER` as configurable env vars in PolymarketConfig and .env.template. User sets their actual wallet type at runtime. ClobClient validates credentials at startup via `create_or_derive_api_creds()`, and 401 errors are logged clearly (per Plan 05 checkpoint verification).

2. **Kalshi demo environment API compatibility**
   - What we know: Production requires `yes_price_dollars` format as of March 2026. Demo may still accept legacy format.
   - What's unclear: Whether demo has also migrated (STATE.md notes this concern).
   - Recommendation: Code for production format only. If demo rejects it, that's a demo-specific issue, not a code issue.
   - RESOLVED: Plan 02 implements production format only (`yes_price_dollars` strings). Legacy integer fields are completely removed -- no dual-format fallback. If demo environment diverges from production, that is a demo-specific issue and does not affect code correctness. Plan 05 collector verification confirms the format works against live APIs.

3. **Heartbeat lifecycle management**
   - What we know: Heartbeat must send every 5s. Missing it cancels ALL open orders.
   - What's unclear: Should heartbeat run continuously or only when orders are open? Continuous is safer but adds unnecessary API traffic.
   - Recommendation: Start heartbeat when first order is placed, stop when no orders are open. Add a configurable `heartbeat_enabled` flag.
   - RESOLVED: Plan 04 Task 2 implements continuous heartbeat as a dedicated async task (per D-04). The heartbeat waits for ClobClient L2 auth before starting, then runs continuously at 5s intervals. Continuous mode chosen over order-triggered mode because: (a) safer -- no risk of missing the 10s timeout during startup of a new heartbeat, (b) simpler -- no race condition between order placement and heartbeat start, (c) API traffic is minimal (one small POST every 5s). The `stop_heartbeat()` method provides clean shutdown.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12+ | All backend code | Yes | 3.13.13 | -- |
| py-clob-client | Polymarket auth, heartbeat, fees | Yes | 0.34.6 | -- |
| cryptography | Kalshi RSA auth | Yes | 44.0.0 | -- |
| web3 | Polymarket balance check | Yes | 7.15.0 | -- |
| pytest | Test runner | Yes | 8.3.4 | -- |
| Node.js | Dashboard frontend (not this phase) | Yes | 22.14.0 | -- |
| Docker/Redis/PostgreSQL | Services (not required this phase) | Not checked | -- | Phase works without them -- collectors use HTTP directly |

**Missing dependencies with no fallback:**
- None -- all required dependencies are installed.

**Missing dependencies with fallback:**
- Docker/Redis/PostgreSQL: Not needed for Phase 1. Collectors make direct HTTP calls. PriceStore has in-memory fallback.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.4 |
| Config file | `conftest.py` (root) -- provides async test support via `pytest_pyfunc_call` |
| Quick run command | `python3 -m pytest arbiter/ -x --tb=short -q` |
| Full suite command | `python3 -m pytest arbiter/ --tb=short` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| API-01 | Kalshi order uses `yes_price_dollars` string | unit | `python3 -m pytest arbiter/execution/test_engine.py -x -k kalshi` | Partial -- needs new tests |
| API-02 | ClobClient init with signature_type/funder | unit | `python3 -m pytest arbiter/execution/test_engine.py -x -k polymarket` | No -- Wave 0 |
| API-03 | Heartbeat sends every 5s | unit | `python3 -m pytest arbiter/execution/test_engine.py -x -k heartbeat` | No -- Wave 0 |
| API-04 | Fee rates match current schedule | unit | `python3 -m pytest arbiter/audit/test_math_auditor.py -x` | Yes -- needs rate updates |
| API-05 | PredictIt execution code removed | unit | `python3 -m pytest arbiter/ -x --tb=short` (no import errors) | Partial -- existing tests reference PredictIt |
| API-06 | Polymarket auth succeeds | integration | Manual -- requires live credentials | No -- manual verification |
| API-07 | Collectors fetch live data | integration | Manual -- requires live API access | No -- manual verification |

### Sampling Rate
- **Per task commit:** `python3 -m pytest arbiter/ -x --tb=short -q`
- **Per wave merge:** `python3 -m pytest arbiter/ --tb=short`
- **Phase gate:** Full suite green before `/gsd-verify-work`

### Wave 0 Gaps
- [ ] Update `arbiter/audit/test_math_auditor.py` -- fix expected fee values to match current rates
- [ ] Add Kalshi order format test verifying `yes_price_dollars` string output
- [ ] Add ClobClient init test verifying `signature_type` and `funder` are passed
- [ ] Add heartbeat manager test verifying 5s interval and error handling
- [ ] Update `arbiter/execution/test_engine.py` -- remove PredictIt-specific test cases or adapt them

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | Yes | RSA-PSS for Kalshi, EIP-712 + HMAC-SHA256 for Polymarket (via py-clob-client) |
| V3 Session Management | Yes | Polymarket heartbeat = session keepalive; HMAC-signed API credentials |
| V4 Access Control | No | Single operator system, no multi-user access control |
| V5 Input Validation | Yes | Price format validation (dollar strings), quantity validation |
| V6 Cryptography | Yes | py-clob-client handles EIP-712 signing; cryptography handles RSA-PSS. Never hand-roll. |

### Known Threat Patterns for This Stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| API key exposure in logs | Information Disclosure | Never log private keys, API secrets, or full signatures. Mask in logger. |
| Stale heartbeat causing order cancellation | Denial of Service | Dedicated async task with 5s interval, error recovery, monitoring |
| Price string injection | Tampering | Validate price strings are valid decimals before sending to API |
| Race condition between fee fetch and order | Tampering | Fetch fees before calculating opportunity, never use stale fee rates for live orders |

## Sources

### Primary (HIGH confidence)
- [Kalshi Fixed-Point Migration](https://docs.kalshi.com/getting_started/fixed_point_migration) -- price format, field names, migration timeline
- [Kalshi Create Order API](https://docs.kalshi.com/api-reference/orders/create-order) -- complete request/response schema
- [Polymarket Fee Schedule](https://docs.polymarket.com/trading/fees) -- current category rates, formula, maker/taker rules
- [Polymarket Heartbeat API](https://docs.polymarket.com/api-reference/trade/send-heartbeat) -- endpoint spec, auth headers
- [Polymarket Authentication](https://docs.polymarket.com/api-reference/authentication) -- L1/L2 auth, signature types, POLY_* headers
- py-clob-client 0.34.6 source code (local inspection via `inspect.getsource`) -- ClobClient constructor, post_heartbeat, get_fee_rate_bps

### Secondary (MEDIUM confidence)
- [py-clob-client GitHub README](https://github.com/Polymarket/py-clob-client/blob/main/README.md) -- initialization examples, wallet type configuration
- [py-clob-client PyPI](https://pypi.org/project/py-clob-client/) -- version 0.34.6, released Feb 19, 2026

### Tertiary (LOW confidence)
- [AgentBets Polymarket API Guide](https://agentbets.ai/guides/polymarket-api-guide/) -- heartbeat background (redirects to official docs for specifics)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all packages verified installed with exact versions via pip show
- Architecture: HIGH -- source code inspected directly, API docs fetched and verified
- Fee rates: HIGH -- verified against official Polymarket docs page and Kalshi formula confirmed via multiple sources
- Pitfalls: HIGH -- identified from direct code inspection showing specific wrong values at specific line numbers

**Research date:** 2026-04-16
**Valid until:** 2026-05-16 (30 days -- platform APIs are stable post-migration)
