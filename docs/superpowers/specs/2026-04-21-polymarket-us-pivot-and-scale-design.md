# Design: Polymarket US Pivot + Scale to Thousands of Markets

**Date:** 2026-04-21
**Author:** Claude Code (Opus 4.7 1M)
**Status:** v2 — spec-review-1 issues (3 critical, 5 major, 3 minor) addressed
**Scope:** Unblock Phase 5 live-fire by rewriting the Polymarket integration to target the CFTC-regulated `api.polymarket.us` DCM, then scale the scanner + mapping pipeline from 8 hand-curated markets to hundreds–thousands of auto-discovered markets per platform.

## 1. Problem

Two blockers sit between the arbiter and profitable live arbitrage:

1. **Polymarket non-US CLOB is off-limits.** Current code targets `clob.polymarket.com` (EOA-signed orders, py-clob-client, Polygon wallet). This path is geofenced for US operators, TOS-prohibited to circumvent, and exposes the operator to KYC-on-withdrawal fund-freeze risk.
2. **Scanner is curated to 8 pairs.** `MARKET_SEEDS` in `arbiter/config/settings.py` has 8 entries, all hand-written. The product goal — "scan hundreds if not thousands of markets correctly" — requires auto-discovery of markets on both platforms, mapping at scale, and a scanner whose per-tick cost doesn't explode at n ≫ 8.

## 2. Goal

- Ship a working Polymarket US collector + execution adapter against `api.polymarket.us/v1` using Ed25519 header auth.
- Keep every safety invariant intact: kill-switch, hard-locks, SAFE-01..06, 60s abort window, reconcile tolerance, per-mapping `allow_auto_trade` gate.
- Grow the market universe from 8 → target **1000 Kalshi markets × 1000 Polymarket markets**, mapped via a pipeline whose top-1% candidates are auto-enabled when resolution criteria match.
- Maintain 1s scan cadence at target scale: O(matched-pairs) per tick, not O(platforms × markets).
- All 407 existing tests green; ≥ 40 new tests for the US path + scale; `tsc --noEmit` clean; 15-item preflight passes.
- End state: I hand the operator two Telegram commands — (a) first supervised live trade, (b) flip `AUTO_EXECUTE_ENABLED=true` — and stop. Operator owns the kill-switch moment.

## 3. Non-goals

- No change to Kalshi auth, adapter, or collector except rate-limit budgets and pagination for scale.
- No change to SafetySupervisor, AutoExecutor policy gates, or dashboard.
- No migration of historical execution data.
- No VPN/offshore/reverse-engineering paths — killed in HANDOFF §0.
- No autonomous flip of `AUTO_EXECUTE_ENABLED`; no autonomous first live trade.

## 4. Key research findings (correcting HANDOFF §0 where stale)

| HANDOFF §0 said | Research confirms | Action |
|---|---|---|
| Auth headers `X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature` | ✅ Correct | Keep |
| Ed25519 on `{timestamp}{method}{path}{body}` | ❌ Wrong — **body is NOT included**. Payload is `f"{timestamp_ms}{METHOD}{path}"` (query is part of path) | Use the research-confirmed format |
| Fee: "0% maker, 0.75–1.80% taker" | ❌ Wrong — real schedule is `fee = Θ × C × p × (1 − p)` with `Θ_taker = 0.05`, `Θ_maker = −0.0125` (rebate), banker's rounding to the cent | Rewrite `polymarket_order_fee()` with the correct quadratic |
| SDK at `Polymarket/polymarket-us-python` | ⚠️ GitHub path not verified; `polymarket-us` on PyPI v0.1.2 (MIT, Python ≥ 3.10) is "Official Python SDK" | Use PyPI package; fall back to hand-rolled Ed25519 if SDK is thin |
| Base URL `https://api.polymarket.us/v1/` | ✅ Correct | Keep |
| "Developer portal at polymarket.us/developer" | ✅ Referenced in auth doc | Drive via Playwright |
| iOS-app KYC required before keys | ✅ Confirmed | User has done it (confirmed 2026-04-21) |

Additional facts used in this design:
- Polymarket US rate limit: **20 req/s per API key** (shared pool, no per-endpoint split).
- Polymarket US WebSocket: `wss://api.polymarket.us/v1/ws/markets`, subscription type `SUBSCRIPTION_TYPE_MARKET_DATA`, **max 100 markets per sub** → multiplex connections beyond that.
- Kalshi production base: `https://api.elections.kalshi.com/trade-api/v2` (not `trading-api.kalshi.com`).
- Kalshi `GET /markets` supports `cursor` + `limit` (default 100, max 1000) — pagination-friendly.
- Kalshi fee: `⌈0.07 × C × P × (1 − P)⌉` in cents.
- Kalshi rate limit (Basic tier): 20 reads/s, 10 writes/s.

## 5. Architecture changes

### 5.1 Collector + auth (Polymarket US)

New file: `arbiter/collectors/polymarket_us.py` (parallel to `polymarket.py`, which becomes `polymarket_legacy.py` and is not wired in production).

```
PolymarketUSCollector
├── auth:  Ed25519Signer (in arbiter/auth/ed25519_signer.py)
│           sign(method, path, ts_ms) -> b64 signature
│           headers(method, path) -> {X-PM-Access-Key, X-PM-Timestamp, X-PM-Signature}
├── rest:  REST client (aiohttp) with shared 20 RPS token bucket
│           discover_markets() -> List[USMarket]    # GET /v1/markets, cursor/offset pagination
│           fetch_orderbook(slug, depth=10)         # GET /v1/orderbook/{symbol}
│           fetch_bbo(slug)                         # GET /v1/orderbook/{symbol}/bbo
│           balance()                               # GET /v1/account/balances
│           positions()                             # GET /v1/positions
│           place_order(slug, intent, price, qty, tif="FILL_OR_KILL")
│           cancel_order(order_id, slug)
├── ws:    PolymarketUSWebSocket (multi-conn, ≤100 slugs/conn)
│           subscribe(slugs) -> async for tick in stream()
└── fees:  polymarket_us_order_fee(price, qty, intent="taker") using Θ·C·p·(1−p)
```

The Ed25519 signer lives as its own module because it's reusable and easy to unit-test. Unit tests exercise vector inputs with a known keypair; integration tests hit a staging endpoint if available, otherwise recorded `aioresponses` fixtures.

### 5.2 Execution adapter

New `arbiter/execution/adapters/polymarket_us.py` implementing the same `PlatformAdapter` protocol as the legacy adapter. `place_fok` maps to `POST /v1/orders` with `tif=FILL_OR_KILL`.

**Hard-lock enforcement order (exact, to prevent accidental bypass):**

```python
def place_fok(self, arb_id, market_id, canonical_id, side, price, qty) -> Order:
    notional = price * qty
    # Gate 1: legacy hard-lock (still authoritative if set, even on US path)
    if notional > config.PHASE4_MAX_ORDER_USD:
        raise OrderRejected("PHASE4 hard-lock")
    # Gate 2: Phase 5 hard-lock (stricter)
    if notional > config.PHASE5_MAX_ORDER_USD:
        raise OrderRejected("PHASE5 hard-lock")
    # Gate 3: supervisor armed check
    if self.supervisor.is_armed:
        raise OrderRejected("supervisor armed")
    # Only now do we construct, sign, and send
    signed = self._sign_and_prepare(side, price, qty)
    return self._client.post_order(signed)
```

Both locks intentionally stay in sequence so a future refactor that removes one still gets caught by the other. `PolymarketUSConfig` (new, §5.5) imports the SAME `PHASE4_MAX_ORDER_USD` and `PHASE5_MAX_ORDER_USD` module-level constants the legacy adapter uses — there is one source of truth for the cap, not two.

### 5.3 Scanner scaling

Current `scan_once()` is O(n × platforms²) per tick on a curated MARKET_MAP. At n=8 this is ~10ms. At n=1000 with no changes it's ~1s — the entire tick budget.

New model:

```
┌─────────────────────────────────────────────────────────────┐
│                  Quote Fan-in (async, per-platform)          │
│  Kalshi REST paginate + WS delta  →  PriceStoreShard(kalshi) │
│  Polymarket US WS fan-out         →  PriceStoreShard(poly)   │
└─────────────────────┬───────────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  MarketMapIndex (in-memory)                  │
│  canonical_id → {kalshi_ticker, poly_slug, allow_auto_trade, │
│                  min_edge_cents, ...}                        │
└─────────────────────┬───────────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  MatchedPairStream                           │
│  On price update for platform X, look up canonical_id,       │
│  fetch counterparty-platform quote from shard, yield pair.   │
│  Cost per quote: O(1). Total per tick: O(updates), not O(n²).│
└─────────────────────┬───────────────────────────────────────┘
                      ▼
           ArbitrageScanner.on_matched_pair(pair)
                → existing opportunity math
                → AutoExecutor (unchanged)
```

**Per-quote cost is O(1), but per-match cost is NOT zero.** WS frame decode (JSON parse ~50–200µs) + dataclass construction + subscriber fan-out + Prometheus counters realistically bound a single asyncio loop at **2–4k events/sec** on a dev M-series box, not the naive 10k. The scanner therefore includes:

- **Per-canonical debounce** (default 50ms): if a canonical_id fires > 20 quote updates/sec, the matcher coalesces to 1 match attempt per debounce window.
- **Backpressure via bounded `asyncio.Queue(maxsize=5000)`** between the fan-in and the matcher. On overflow, drop oldest with a `matcher_backpressure_drops_total` counter increment — better to miss a stale tick than to crash the loop.
- **Opportunity emit throttle:** no more than 10 opportunities per second per (canonical_id, side) — prevents a runaway feedback loop if AutoExecutor briefly can't consume them.

Target: p99 matcher tick ≤ 100ms at 1000 canonical pairs under burst (2× steady-state for 30 seconds). Verified by `test_scale_1000.py` (§6.2).

### 5.4 Market-mapping pipeline

`arbiter/mapping/auto_discovery.py` (new):

- Pull all live Kalshi markets via `GET /markets?status=open&limit=1000` with cursor pagination.
- Pull all live Polymarket US markets via `GET /v1/markets` (page size TBD; empirical probe first).
- Score candidate pairs via `similarity_score(description, aliases)` — existing function, no changes.
- Write to Postgres `market_mappings` table with status `candidate` and score.

#### 5.4.1 Resolution-criteria equivalence (the SAFE-06 gate)

`similarity_score` is **text-only** — it does NOT verify that two markets resolve to the same real-world outcome. Auto-promoting on text score alone is how phantom arbs happen. The gate is three layers:

**Layer 1 — structured-field equivalence (`arbiter/mapping/resolution_check.py`, new):**
- Extract from each market: `resolution_date` (or close date), `resolution_source` (where the outcome is determined), `tie_break_rule` (if any), `category`, `outcome_set` (binary Yes/No or categorical).
- Return `ResolutionMatch.IDENTICAL` only when every field matches OR the differences are within an allow-list (e.g., date within 24h, different-but-equivalent resolution sources like "AP" and "Associated Press").
- Return `ResolutionMatch.DIVERGENT` on any unallow-listed difference.
- Return `ResolutionMatch.PENDING` when either side lacks the data.

**Layer 2 — LLM verifier (`arbiter/mapping/llm_verifier.py`, new, optional):**
- Prompt: "Do these two prediction-market questions resolve to the same real-world outcome? Answer YES / NO / MAYBE with a one-sentence reason." Cache results keyed by (kalshi_ticker, poly_slug).
- Only consulted when Layer 1 returns `IDENTICAL` AND score ≥ 0.85 — as a tie-breaker, not a primary gate.
- Uses Claude Haiku 4.5 via the Anthropic SDK (cheap, ~$0.0001 per candidate).

**Layer 3 — fixture corpus for regression:**
- `arbiter/mapping/fixtures/known_equivalent_pairs.json` + `known_divergent_pairs.json` — 20+ hand-labeled examples per side, built from today's 8 confirmed pairs + 15 known-divergent pairs (e.g., Kalshi "Fed cuts in May" vs Polymarket "Fed cuts by July" — DIVERGENT despite high text score).
- CI test asserts `resolution_check` classifies every fixture correctly. A regression here blocks merge.

**Auto-promote gate (combined, replaces earlier version):**

A mapping is auto-promoted to `allow_auto_trade=True` only if ALL hold:
1. `AUTO_PROMOTE_ENABLED=true` in env (default OFF — operator judgment preserved).
2. Text `score >= 0.85`.
3. `resolution_check.result == IDENTICAL` (Layer 1).
4. LLM verifier returns `YES` (Layer 2, when configured).
5. Both sides show top-of-book depth ≥ `PHASE5_MAX_ORDER_USD × 2` at quote time (liquidity sanity).
6. `resolution_date` is within 90 days (no decade-out markets where prices can stay mispriced indefinitely).
7. Daily auto-promote count < `AUTO_PROMOTE_DAILY_CAP` (default 20) — a bad upstream data dump can't flood the confirmed set.
8. Cooling-off: first `AUTO_PROMOTE_ADVISORY_SCANS` (default 30) scans after promotion are **advisory-only** — scanner emits opportunities but AutoExecutor's gate G4 stays False; operator sees them in `/ops/mappings` and must click-confirm before trading. Only after that does `allow_auto_trade` flip to True at runtime.

When `AUTO_PROMOTE_ENABLED=false` (default), candidates just land in `/ops/mappings` for manual review — same workflow as today, just with thousands of candidates instead of eight. This keeps the operator-in-loop invariant from HANDOFF §1.

### 5.5 Settings / env / preflight renames

**Variant flag:** a new env `POLYMARKET_VARIANT` with values `legacy` | `us` | `disabled` (default `us`) selects which collector+adapter the system wires in. `legacy` remains importable and test-fixture-compatible so none of the 407 existing tests need to rewrite; `us` is what production ships. `disabled` is for Kalshi-only dev loops.

**Config classes (two, not one):**

```python
# settings.py — both kept, both always importable
@dataclass
class PolymarketConfig:           # legacy — unchanged from today
    clob_url: str = env("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
    private_key: str = env("POLY_PRIVATE_KEY", "")  # optional now
    funder: str = env("POLY_FUNDER", "")
    signature_type: int = env_int("POLY_SIGNATURE_TYPE", 2)
    ...

@dataclass
class PolymarketUSConfig:         # new
    api_url: str = env("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1")
    api_key_id: str = env("POLYMARKET_US_API_KEY_ID", "")
    api_secret: str = env("POLYMARKET_US_API_SECRET", "")
    ws_url: str = env("POLYMARKET_US_WS_URL", "wss://api.polymarket.us/v1/ws/markets")
    ...
```

`load_config()` selects one based on `POLYMARKET_VARIANT` and returns a union type. Tests that `PolymarketConfig()` directly keep working (legacy class is unchanged); new tests use `PolymarketUSConfig()`.

**Env-var changes (additive, nothing deleted):**

| Env | Change |
|---|---|
| `POLYMARKET_US_API_KEY_ID` | **new** — Ed25519 key ID |
| `POLYMARKET_US_API_SECRET` | **new** — base64 Ed25519 secret (32 bytes decoded) |
| `POLYMARKET_US_API_URL` | **new** — default `https://api.polymarket.us/v1` |
| `POLYMARKET_VARIANT` | **new** — `legacy` / `us` / `disabled`, default `us` |
| `POLY_PRIVATE_KEY` | **kept** (optional; legacy path only) |
| `POLY_FUNDER` | **kept** (optional; legacy path only) |
| `POLYGON_RPC_URL` | **kept** (optional; legacy path only) |

**Preflight — split into two sub-checks, not one:**

- Check #5a (credentials-only, can run in CI): asserts `POLYMARKET_VARIANT=us` → `POLYMARKET_US_API_KEY_ID` set + `POLYMARKET_US_API_SECRET` parseable as 32-byte Ed25519 key.
- Check #5b (live, requires network + valid keys): signs and calls `GET /v1/account/balances`, asserts `currentBalance ≥ $20`.

Check #5a always runs. Check #5b is skipped in CI unless `PREFLIGHT_ALLOW_LIVE=1`. `./scripts/setup/go_live.sh` sets the flag before invoking preflight.

**Rate-limit budget split (new):** `RateLimiter` for Polymarket US reserves 2 req/s for discovery, 18 req/s for quoting + execution. This prevents a startup discovery pass from starving live trading.

### 5.6 Playwright-driven onboarding

A new helper script `scripts/setup/onboard_polymarket_us.py` uses the `playwright` MCP to:

1. Open `https://polymarket.us/developer` in a fresh browser session.
2. Prompt the operator via Telegram: "Log in on this browser window; I'll wait for the dev-portal URL to land." Wait for URL change.
3. Once logged in, navigate to API-key generation, click "Create key," capture the secret **directly from the DOM field** (never via screenshot).
4. Write the secret into `.env.production` via the Edit tool (gitignored, `chmod 600`).
5. Close the page showing the secret, delete any intermediate screenshots, do not log the value.

## 6. Testing strategy

### 6.1 Unit
- `arbiter/auth/test_ed25519_signer.py` — vector tests with a fixed keypair; verify payload format `{ts}{METHOD}{path}` (no body).
- `arbiter/collectors/test_polymarket_us.py` — `aioresponses`-mocked REST + WS; pagination, rate-limit-retry, WS reconnect.
- `arbiter/scanner/test_matched_pair_stream.py` — O(1) cost property; correctness at n=1000.
- `arbiter/config/test_polymarket_us_fee.py` — quadratic fee matches docs' examples; sign is correct for maker rebate.
- `arbiter/execution/adapters/test_polymarket_us_adapter.py` — PHASE5 hard-lock, FOK semantics, order-id threading.
- `arbiter/mapping/test_auto_discovery.py` — score threshold, SAFE-06 gate, AUTO_PROMOTE gate.

### 6.2 Integration
- `arbiter/live/test_polymarket_us_preflight.py` — end-to-end preflight against recorded fixtures (no real creds in CI).
- `arbiter/scanner/test_scale_1000.py` — synthetic 1000-market feed, verify tick budget ≤ 100ms p99.

### 6.3 Live-fire (operator-only, unchanged)
- `arbiter/live/test_first_live_trade.py` — already exists, just points at the new adapter.

### 6.4 Continuous verification
- `tsc --noEmit` green.
- All 407 legacy tests green. Polymarket-specific ones that assert against CLOB-specific behavior move to `@pytest.mark.legacy_polymarket`; CI runs both markers by default, `skip_legacy_polymarket=1` can opt out.
- **Coverage targets (not count):** each of the following critical paths has **≥ 3 tests**, including a negative-path test:
  - Ed25519 signer (happy, wrong-timestamp, wrong-path)
  - Fee function (taker positive, maker negative, banker's rounding)
  - Adapter hard-lock enforcement order (both gates trip; G4 before G5 before signing)
  - Auto-promote gate (all 8 conditions individually cause rejection)
  - Scale matcher p99 at n=1000 (steady, burst, backpressure-drop)
  - Preflight split (5a alone, 5a+5b together, 5b network-error)
- Integration signature round-trip: `aioresponses`-mocked `api.polymarket.us` verifies the exact signature string the test harness expects.

## 7. Feedback loop for live production

HANDOFF §3 Steps 4–7 already describe this. The new pieces:

- **Prometheus scrape:** `GET /api/metrics` already exists; add:
  - `polymarket_us_rest_latency_p99_ms`
  - `polymarket_us_ws_reconnects_total`
  - `matched_pair_stream_events_total`
  - `matcher_backpressure_drops_total`
  - `matched_pair_latency_seconds` (histogram, per-canonical label)
  - `auto_discovery_candidates_pending`
  - `auto_promote_rejections_total{reason=...}` (reason labels: text_score_low, resolution_divergent, llm_no, liquidity_low, date_out_of_window, daily_cap, cooling_off)
  - `ed25519_sign_failures_total`
  - `ws_subscription_count{platform="polymarket_us"}` (for visibility into 100-markets/conn multiplex)
- **Telegram heartbeat:** every 15 min while auto-mode is on, post realized_pnl + open-order count to the ops chat.
- **Auto-abort triggers** (existing): reconcile breach, one-leg timeout, rate-limit crit, supervisor armed.
- **Daily reconciliation cron:** `scripts/ops/daily_reconcile.py` diffs dashboard `realized_pnl` against exchange balances; alerts on > $1 discrepancy.

## 8. Risks

1. **`polymarket-us` SDK thin or broken** — mitigated by hand-rolled Ed25519 path; SDK is only a convenience wrapper.
2. **Kalshi prod market count blows past rate limit** — mitigated by WebSocket-first design (one subscribe, many updates). REST only for discovery + fills.
3. **SAFE-06 resolution-criteria mismatch** at scale — this is the cliff. If auto-discovery promotes a pair whose resolution criteria diverge, the system trades a phantom arb. Mitigation: `AUTO_PROMOTE_ENABLED=false` default; score ≥ 0.85; human spot-check first 50 promoted pairs.
4. **Polymarket US liquidity thinner than non-US CLOB** — opportunities may be rare at $10/leg. Mitigation: scanner logs `opportunity_candidates_rejected_by_size` so we can see the gap.
5. **Ed25519 signing payload format changes** — trapped by a signed-request unit test that pins the exact bytes of the payload.

## 9. Rollout order

1. Ed25519 signer + unit tests — lowest risk, self-contained.
2. Fee function + tests — drop-in.
3. Settings / env renames + backwards-compat flag `POLYMARKET_VARIANT` (legacy|us) so the legacy tests still exercise the old path.
4. New collector + REST/WS + unit tests.
5. New adapter + hard-lock tests.
6. Scanner refactor to event-driven matcher + scale test at n=1000.
7. Auto-discovery pipeline + mapping UI hooks.
8. Preflight + `check_polymarket_us.py`.
9. `.env.production.template` update.
10. Playwright onboarding script.
11. Prometheus metrics + Telegram heartbeat.
12. Full suite + preflight + tsc clean + push to main.
13. Hand operator the two Step-5 / Step-6 commands.

Each step is a separate commit pushed to `main`. Work is parallelizable via `subagent-driven-development` across (1-2), (4-5), (6-7), (8-9), (10-11) independent slices.

## 9a. Fee-function migration — breaking change, handled explicitly

The current `polymarket_order_fee(price, qty, fee_rate, category)` in `settings.py:81-113` returns a **non-negative** float. The new form allows **negative** return values (maker rebate: `Θ_maker = −0.0125`). Callers that assume `fee ≥ 0` (notably `ArbitrageOpportunity.total_fees` and the PnL reconciler) will misreport.

Migration steps (in `arbiter/scanner/arbitrage.py` and `arbiter/audit/pnl_reconciler.py`):
1. New function `polymarket_us_order_fee(price, qty, intent)` returns signed float (maker=negative, taker=positive).
2. Legacy `polymarket_order_fee` kept untouched for `POLYMARKET_VARIANT=legacy`.
3. `ArbitrageOpportunity.total_fees` becomes `sum(abs(f) for f in fees)` for worst-case gross; a new `net_fees` field tracks signed sum (includes rebate).
4. PnL reconciler uses `net_fees` for true realized PnL; dashboard continues to show gross fees as a disclosure line.
5. All fee tests pinned to numeric expected values get re-pinned with the new Θ-curve — a mechanical sweep, tracked per-test in the implementation plan.

## 9b. Rollback plan

If the new collector misbehaves after a commit on `main`:
1. `AUTO_EXECUTE_ENABLED=false` (existing kill).
2. `POLYMARKET_VARIANT=disabled` → arbiter runs Kalshi-only, no trades.
3. `POLYMARKET_VARIANT=legacy` → reverts to old CLOB (only useful for non-US operators; kept for test fixtures).
4. `docker compose -f docker-compose.prod.yml restart arbiter-api-prod`.

Total rollback time: < 2 minutes. No code revert needed — it's a config flip.

## 10. What stays unchanged

- `SafetySupervisor`, `AutoExecutor`, all 7 policy gates, kill-switch, SAFE-01..06, D-17 tolerance.
- Kalshi adapter and collector (beyond rate-limit tuning).
- Dashboard, Telegram notifier retry/dedup, `/api/metrics` scaffolding.
- `AUTO_EXECUTE_ENABLED=false` default.
- Human-in-loop for: first live trade, auto-flip, mapping promotion when `AUTO_PROMOTE_ENABLED=false`.

## 11. Exit criteria

- [ ] 407 legacy tests green (Polymarket-specific ones marked legacy and skipped in default run; still runnable).
- [ ] ≥ 40 new tests green.
- [ ] `tsc --noEmit` clean.
- [ ] `python -m arbiter.live.preflight` returns 0 with real `.env.production`.
- [ ] `./scripts/setup/go_live.sh` returns "ALL CHECKS PASSED."
- [ ] Auto-discovery populates ≥ 100 candidate mappings on first run.
- [ ] Scanner holds < 100ms p99 tick at n=1000 synthetic load.
- [ ] Every commit on `main` at https://github.com/sandeepportfolio/arbiter-dashboard.
- [ ] Operator receives two pastable commands via Telegram to fire Step 5 (first live trade) and Step 6 (auto-mode flip).
