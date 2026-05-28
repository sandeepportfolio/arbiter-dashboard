# Arbiter

Cross-platform prediction-market arbitrage system that detects price discrepancies between Kalshi, Polymarket (US gateway), and ForecastEx (IBKR-routed), then executes paired YES/NO trades to capture the spread.

Production-grade. Live money. Treat changes accordingly.

---

## Table of contents

1. [System overview](#system-overview)
2. [Architecture](#architecture)
3. [The three venues](#the-three-venues)
4. [ForecastEx YES/NO conid handling](#forecastex-yesno-conid-handling)
5. [Safety controls](#safety-controls)
6. [Stranded-leg mitigation](#stranded-leg-mitigation)
7. [Mapping system](#mapping-system)
8. [Configuration](#configuration)
9. [API endpoints](#api-endpoints)
10. [Ops dashboard](#ops-dashboard)
11. [Deployment](#deployment)
12. [Database schema](#database-schema)
13. [Current state snapshot](#current-state-snapshot)
14. [Known limitations](#known-limitations)
15. [Repository layout](#repository-layout)

---

## System overview

Arbiter looks for binary prediction-market arbitrages of the form:

> Buy YES on platform A at price `p_yes`, buy NO on platform B at price `p_no`, where `p_yes + p_no + fees < $1.00`. At settlement one side pays $1, the other pays $0, locking in a riskless `1 - p_yes - p_no - fees` profit per contract.

The system has three loosely-coupled async pipelines that share a Redis-backed quote cache and a PostgreSQL execution ledger:

1. **Collectors** poll each venue's public/authenticated market-data endpoints and write `PricePoint` snapshots into the price store.
2. **Scanner** subscribes to the price store, evaluates cross-platform pairs against fee-aware edge thresholds, and publishes `ArbitrageOpportunity` records.
3. **Executor** subscribes to opportunities, runs pre-flight gates (depth check, edge floor, supervisor armed, kill-switch state, profitability gate), submits primary + secondary orders, and reconciles fills against the opportunity.

A dashboard exposed on port 8080 surfaces live opportunities, executions, balances, incidents, and mapping state via a single-page HTML console and a JSON API.

---

## Architecture

```
                  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐
   Kalshi REST ───┤              │    │              │    │                  │
                  │  Collectors  │───▶│  PriceStore  │───▶│     Scanner      │
   Polymarket  ───┤  (per venue) │    │ (in-mem +    │    │ (cross-platform  │
   CLOB           │              │    │  Redis TTL)  │    │  pair detection) │
                  │              │    │              │    │                  │
   IBKR / FX   ───┤              │    └──────────────┘    └────────┬─────────┘
                  └──────┬───────┘                                  │
                         │                                          ▼
                         │                              ┌──────────────────────┐
                         │                              │  ArbitrageOpportunity│
                         │                              └────────┬─────────────┘
                         │                                       │
                         │                                       ▼
                         │                          ┌────────────────────────┐
                         │                          │     AutoExecutor       │
                         │                          │  (safety gates,        │
                         │                          │   primary + secondary  │
                         │                          │   leg orchestration)   │
                         │                          └────────┬───────────────┘
                         │                                   │
                         │                                   ▼
                         │                         ┌───────────────────┐
                         │                         │  ExecutionEngine  │
                         │                         │ (per-venue        │
                         │                         │  adapters,        │
                         │                         │  fill polling,    │
                         │                         │  recovery)        │
                         │                         └────────┬──────────┘
                         │                                  │
              ┌──────────▼──────────┐               ┌───────▼────────┐
              │  BalanceMonitor     │               │  Postgres ledger│
              │  StrandedReconciler │               │ execution_arbs  │
              │  Audit + Reconciler │               │ execution_orders│
              └─────────────────────┘               │ execution_incid.│
                                                    │ market_mappings │
                                                    └─────────────────┘
```

### Key abstractions

| Type | Location | Purpose |
|---|---|---|
| `PricePoint` | `arbiter/utils/price_store.py` | Single quote across a venue/market with `yes_market_id`, `no_market_id`, bid/ask each side, mapping metadata. |
| `PriceStore` | `arbiter/utils/price_store.py` | In-memory dict + optional Redis backing with TTL-based freshness gate and subscription queues. |
| `ArbitrageScanner` | `arbiter/scanner/arbitrage.py` | Pair evaluator with dead-ask gate, non-binary guard, wide-spread guard, fee-aware edge calculation, persistence-count gating. |
| `ArbitrageOpportunity` | `arbiter/scanner/arbitrage.py` | Detected edge with both `yes_market_id` and `no_market_id` distinct per side. |
| `AutoExecutor` | `arbiter/execution/auto_executor.py` | Gate-stack: kill-switch → mapping confirmed → cooldown → failure-pattern → balance reserve → ForecastEx-NO-conid integrity → notional cap → execute. |
| `ExecutionEngine` | `arbiter/execution/engine.py` | Per-platform adapter dispatch, primary FOK + secondary IOC walking, smart-unwind, soft-naked recovery. |
| `PlatformAdapter` | `arbiter/execution/adapters/*` | Per-venue `place_fok`, `place_ioc`, `place_resting_sell`, `place_unwind_sell`, `check_depth`, `get_order`, `cancel_order`. |
| `BalanceMonitor` | `arbiter/monitor/balance.py` | Per-platform balance polling with alert thresholds. |
| `StrandedReconciler` | `arbiter/recovery/stranded_reconciler.py` | Detects naked legs from venue position state, age-bucketed loss-cut policy, settlement-proximity hold, close-rate limit. |
| `MitigationEngine` | `arbiter/recovery/mitigation_engine.py` | Executes the close action picked by the reconciler (smart-unwind, hold-to-settle, panic-sell). |
| `MathAuditor` | `arbiter/audit/math_auditor.py` | Verifies executed arb math (`yes_qty == no_qty`, P&L sign, fee accuracy). |

---

## The three venues

### Kalshi (`arbiter/collectors/kalshi.py`, `arbiter/execution/adapters/kalshi.py`)

- US-regulated binary event exchange. RSA-signed REST.
- Each market has a single `ticker`; YES vs NO is a side flag (not separate IDs).
- Fees: quadratic `ceil(0.07 * qty * price * (1 - price) * 100) / 100` per order.
- Snapshots include real bid/ask each side (no synthesis).
- Bulk-fetch up to ~270 tickers per cycle in 3 batches.

### Polymarket US (`arbiter/collectors/polymarket_us.py`, `arbiter/execution/adapters/polymarket_us.py`)

- Routed via the Polymarket US public gateway + `py-clob-client` for orders.
- Single market `slug` per binary market; YES and NO share the same order book entry, NO is computed via wire-protocol flip in `_us_order_params` (this is correct for Polymarket's structure, not the ForecastEx phantom-trade pattern).
- Tick size read from market metadata, falls back to 0.01.
- Orders support FOK, IOC, GTC.

### ForecastEx (`arbiter/collectors/forecastex.py`, `arbiter/execution/adapters/forecastex.py`)

- IBKR-routed binary contracts. Polled through the local IBKR Client Portal Web API gateway (TWS or `host.docker.internal:5000`).
- **YES and NO are SEPARATE IBKR conids** (right=C for Call/YES, right=P for Put/NO) under the same parent event.
- This is the venue where the phantom-trade bug (ARB-000695/699) originated. See next section.
- Flat $0.005 taker fee per contract.
- BUY-only at the venue API level; closing a position is a separate SELL on the SAME conid.
- 10 rps rate limit; circuit breaker opens after 5 consecutive failures, recovery 30s.

---

## ForecastEx YES/NO conid handling

This is the architectural detail most relevant to operators and the focus of the ARB-000695/699 fix (commit `d157cb6`).

### The structure

Each ForecastEx binary event exists as a parent IND conid. Under it, IBKR exposes child OPT contracts at one or more strikes:

```
parent: 733131966 (HORC = US House Control)
  child: 762089343 right=C strike=1.0  (DEM majority — Call/YES)
  child: 773659700 right=C strike=2.0  (GOP majority — Call/YES of opposite outcome)
  child:    (right=P at strike 1.0 may or may not exist depending on the listing)
```

### Discovery flow

1. **Mapping seed / auto-discovery** (`arbiter/mapping/forecastex_discovery.py`) — enumerates FORECASTX events via `/iserver/secdef/search` with seed keywords, fuzzy-matches against confirmed Kalshi/Polymarket mappings, and writes BOTH `forecastex_contract_id` (YES) and `forecastex_no_contract_id` (NO, if a Put sibling exists at the same strike) via `mapping_store.upsert`.
2. **Recovery resolver** (`arbiter/recovery/forecastex_resolver.py`) — when a parent IND conid is bound by mistake, snapshot a child, find the Call (right=C) at the correct strike, AND find the Put sibling (right=P at same strike). Persist both.
3. **Runtime sibling discovery** (collector's `_attempt_no_conid_discovery`) — for legacy mappings that only have `forecastex_contract_id` set, on the first poll the collector calls `/iserver/contract/{yes_conid}/info` to retrieve the parent and strike, then `resolve_event_children(parent)` to enumerate siblings. The result is cached in-memory (`_no_discovery_cache` / `_no_discovery_failed`) so we never re-query for the same YES conid.

### Snapshot + price-point assembly

Per cycle, for each tracked `(yes_conid, no_conid_or_empty)` pair:

1. Fetch YES snapshot via `/iserver/marketdata/snapshot`.
2. Resolve NO conid: mapping → runtime cache → on-demand discovery → `None`.
3. If NO conid present, fetch NO snapshot via `_fetch_side_snapshot` (soft-handles 404/410).
4. `_build_price_point` produces a `PricePoint` with:
   - `yes_market_id = yes_conid` (always)
   - `no_market_id = no_conid` ONLY when NO snapshot has real bid OR ask > 0; else `""` (empty string)
   - `no_bid`, `no_ask` from the **REAL NO snapshot** — never `1 - yes_bid` synthesis
   - `yes_price = yes_ask or yes_bid`, `no_price = no_ask or no_bid` (or 0 if no NO data)

### Why some markets show `no_market_id=""`

HORC and SENM-style multi-strike binaries do not have Put options at each strike in IBKR's listings. The structural "NO" of `DEM_HOUSE_2026` is `GOP_HOUSE_2026`'s YES conid — a **different canonical_id** in `MARKET_MAP`, not a Put sibling.

For these markets, runtime NO discovery correctly returns `None`, the collector sets `no_market_id=""`, and the scanner's `dead_ask` gate skips any cross-side opportunity that would otherwise route to the wrong contract. The 24 currently-tracked FX YES conids (all HORC/SENM) have `forecastex_no_contract_id=''` in the DB — this is expected.

When a binary market with a real Put sibling appears (e.g., a single-strike CPI/BTC threshold), discovery will populate `forecastex_no_contract_id` automatically and trading will work both sides.

### Defense-in-depth gates

| Layer | Check | Action |
|---|---|---|
| Collector | NO snapshot is empty / sibling not found | Set `no_market_id=""`, `no_ask=0` |
| Scanner (`arbiter/scanner/arbitrage.py:384`) | `yes_ask <= 0 OR no_ask <= 0` | Skip opportunity (`dead_ask` counter) |
| AutoExecutor | `no_is_fx AND no_market_id == ""` | `skipped_forecastex_unavailable++`, refuse |
| AutoExecutor | 3-way FX with `yes_market_id == no_market_id` | `skipped_forecastex_unavailable++`, refuse |
| Adapter `_submit` | `market_id` empty / "0" / "None" | `OrderRejected`, log `forecastex.submit.empty_market_id` |
| Adapter `check_depth` | `market_id` empty / "0" / "None" | Return `(False, 0.0)` BEFORE any API call |

Any one of these gates would have prevented the ARB-000695/699 phantom trades. The combination makes it impossible for a NO order to route to a YES conid silently.

---

## Safety controls

### Kill switch (SafetySupervisor)

`arbiter/safety/supervisor.py`. State machine with `is_armed: bool`. When armed, all adapter `_submit` paths fail with `OrderRejected("supervisor armed")`. Smart-unwind (`place_resting_sell`, `place_unwind_sell`) is exempt — closing existing exposure must not be blocked by gates intended to stop opening new exposure.

Toggled via the ops dashboard or REST. Default: **unarmed** (trades allowed when other gates pass).

### Auto-execute master switch

`AUTO_EXECUTE_ENABLED` env var. When `false`, the auto-executor refuses every opportunity with `skipped_disabled++` and never reaches the engine. Kill switch and AUTO_EXECUTE are independent — the user can pause auto-trading without arming the supervisor.

### Circuit breakers

`arbiter/utils/retry.py:CircuitBreaker`. Per-venue. Default: 5 consecutive failures opens, 30s recovery, half-open lets one request through, success closes. Errors during transient circuit-open windows fail fast without contacting the venue.

### Rate limiters

`arbiter/utils/retry.py:RateLimiter`. Per-venue token bucket. ForecastEx: 10 rps. Kalshi/Polymarket: similarly conservative.

### Risk caps

| Env var | Default | What it caps |
|---|---|---|
| `MAX_POSITION_USD` | $10 | Per-trade notional after clamping. |
| `MAX_BALANCE_FRACTION_PER_TRADE` | 0.20 | Per-trade % of smallest platform balance. |
| `MIN_PLATFORM_RESERVE_USD` | $15 | Minimum balance the executor will leave on each venue. |
| `MIN_EDGE_CENTS` | 7 | Global minimum net edge to publish an opportunity (pair-specific overrides via `min_edge_for_pair`). |
| `PREFLIGHT_MIN_DEPTH_USD` | — | Required depth at touch for both sides. |
| `PREFLIGHT_MAX_QUOTE_AGE_S` | — | Maximum age of either quote at execution time. |
| `INTER_LEG_DELAY_MS` | — | Cooldown between primary and secondary leg submission. |

### Per-canonical async lock

`ExecutionEngine` serializes execute_opportunity calls per `canonical_id` so two concurrent scans can't fire two orders for the same market.

### Profitability gate

`arbiter/profitability/validator.py`. The system blocks new trades until it has accumulated enough evidence that it's profitable in aggregate:

- `scan_count >= MIN_SCANS`
- `published_opportunities >= 25`
- `average_realized_pnl >= ARBITER_MIN_PROFITABLE_RATIO` (default $0.25/trade)

When the gate is open (`verdict=collecting_evidence`), every opportunity that reaches the executor is logged as a warning incident "Trade gate blocked execution: Profitability verdict is not_profitable". This is normal pre-flight behavior and resets when the verdict transitions to `profitable`.

### Incident severity

`arbiter/audit/...` and the API. Severities: `critical`, `warning`, `info`. The `incident_rate` profitability statistic excludes `info` per commit `5920aef` so normal diagnostic noise (e.g., `depth_low`) doesn't trip the verdict.

### Audit + reconciliation

- `MathAuditor` verifies executed arb math correctness (`yes_qty == no_qty`, fee total, sign of `realized_pnl`).
- `PnLReconciler` reconciles recorded P&L against runtime balance changes per platform.

---

## Stranded-leg mitigation

`arbiter/recovery/stranded_reconciler.py` + `arbiter/recovery/mitigation_engine.py`. Runs on a periodic loop.

### Detection

Walks each venue's positions endpoint. For each open position:
- Match against the canonical_id via `market_mappings` (forward and reverse).
- If no matching opportunity is in `execution_arbs` with an unsettled paired leg, mark as **stranded**.

### Age-bucketed loss-cut policy (commit `ef49211`)

| Age since fill | Loss cut accepted | Rationale |
|---|---|---|
| 0–6h | Break-even only (entry ± $0.01) | Recent fills — favor patience over realized loss. |
| 6–24h | Up to 25% of entry value | Spread-widening or hedge cost is mounting. |
| 24h+ | Up to 50% of entry value | Position is materially exposed; cut to preserve capital. |

### Settlement-proximity hold

If the canonical market is within 24h of settlement, **hold to settle** rather than close at any loss. The expected settlement payout (or zero) dominates the closing-cost calculus.

### Close-rate limit

`_last_close_attempt_ts` sliding-window gate. After a close attempt for a given position, the reconciler waits a cooldown before re-attempting on the same position. Prevents thrashing a stranded leg in fast-moving thin books.

### Telegram alerts

Every close decision and outcome is alerted: action chosen, leg + canonical_id, fill price, realized P&L vs entry, residual exposure.

### Currently held

As of 2026-05-28 there are **16 stranded Kalshi positions** flagged HOLD_TO_SETTLE — all sub-penny positions where closing fees exceed recoverable value. The reconciler is preserving them; they will settle at expiry.

---

## Mapping system

`arbiter/mapping/market_map.py` (model + Postgres store), `arbiter/mapping/auto_discovery.py` (K↔P fuzzy matcher), `arbiter/mapping/forecastex_discovery.py` (K↔P↔FX matcher), `arbiter/mapping/auto_promote.py` (advisory→confirmed promotion).

### MarketMapping fields

```python
canonical_id: str               # human-stable ID, e.g. DEM_HOUSE_2026
description: str
status: confirmed | review | candidate | rejected | expired
allow_auto_trade: bool          # only confirmed + identical resolution can be true
aliases: tuple[str, ...]
tags: tuple[str, ...]
kalshi_market_id: str
polymarket_slug: str
polymarket_question: str
forecastex_contract_id: str     # YES (right=C) conid
forecastex_no_contract_id: str  # NO (right=P) conid — empty when no Put sibling
forecastex_not_available: bool  # negative cache; discovery searched & found none
resolution_criteria_json: str   # JSONB with per-venue rule + settlement_date
resolution_match_status: identical | similar | divergent | pending_operator_review
```

### Confirmation pipeline

1. Auto-discovery proposes a candidate (status=`review`) with a fuzzy match score.
2. Operator (or `auto_promote` if score + resolution match are clean enough) flips to `confirmed`.
3. Only `status=confirmed AND allow_auto_trade=true AND resolution_match_status=identical` is tradeable.
4. Stale mappings auto-expire when underlying markets settle or delist.

### Auto-expire of duplicate exact pairs

`_expire_duplicate_exact_pair_mappings` runs on every upsert. When two mappings bind the exact same `(kalshi_market_id, polymarket_slug)`, the older/auto-suffix one is expired so the scanner can't double-count it.

---

## Configuration

All secrets live in `.env.production` (git-ignored). The Docker compose stack mounts this via `env_file:`. Never commit secrets.

### Critical env vars (values redacted, defaults shown where applicable)

| Var | Purpose |
|---|---|
| `ARBITER_ENV` | `prod` / `staging` / `dev` |
| `AUTO_EXECUTE_ENABLED` | `true`/`false` — master switch for the auto-executor |
| `DRY_RUN` | `false` for live, `true` to log without sending orders |
| `ARBITER_MIN_PROFITABLE_RATIO` | Minimum average realized P&L per trade for the profitability gate (default $0.25) |
| `MAX_POSITION_USD` | Per-trade notional cap |
| `MIN_EDGE_CENTS` | Global minimum net edge to publish (default 7) |
| `EDGE_SCALING_ENABLED` | Enable pair-specific edge floor lookups |
| `LIQUIDITY_ADAPTIVE_SIZING` | Scale qty based on top-of-book depth |
| `DATABASE_URL` | Postgres DSN (or `PG_USER`/`PG_PASSWORD`/host vars) |
| `REDIS_URL` | Redis URL (default `redis://arbiter-redis-prod:6379`) |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Kalshi auth |
| `POLYMARKET_US_API_KEY_ID`, `POLYMARKET_US_API_SECRET` | Polymarket US auth |
| `IBKR_ACCOUNT_ID`, `IBKR_GATEWAY_URL`, `IBKR_PAPER_TRADING` | ForecastEx via IBKR |
| `FORECASTEX_ENABLED`, `FORECASTEX_LOW` | FX collector toggle + balance alert threshold |
| `AUTO_DISCOVERY_*` | Catalog enumeration knobs |
| `AUTO_PROMOTE_*` | Review→confirmed promotion knobs |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_*_CHAT_ID` | Alert routing |
| `OPS_EMAIL`, `OPS_PASSWORD` | Dashboard session auth (HMAC-SHA256 token, 7d expiry) |

### Per-platform low-balance thresholds

`KALSHI_LOW`, `POLYMARKET_LOW`, `FORECASTEX_LOW`. When balance drops below, BalanceMonitor alerts and the executor refuses new exposure on that platform until topped up.

---

## API endpoints

Served by `arbiter/api.py` on port 8080.

| Path | Returns |
|---|---|
| `/` | Ops dashboard HTML |
| `/api/system` | Aggregate health: mode, scanner stats, execution stats, auto_executor stats, audit, profitability, supervisor.is_armed |
| `/api/balances` | Per-platform balance + age + is_low flag |
| `/api/opportunities` | Currently active opportunities (post-gate) |
| `/api/executions` | Recent executions with per-leg market_id / fill_price / pnl |
| `/api/mappings` | Confirmed + review mappings + per-platform IDs |
| `/api/profitability` | Profitability gate state (verdict, progress, scan_count, average_realized_pnl) |
| `/api/alerts` | Recent alerts merged from incidents + execution results + safety events |
| `/api/failed-trades` | Trades that hit OrderRejected / FAILED with reason metadata |
| `/api/incidents` (best-effort) | Recent incidents — categorize by severity |
| `/api/ws` | WebSocket fan-out for live price/opp/execution streams |

Some legacy paths return 404 (`/healthz`, `/api/positions`, `/api/scanner`) — the dashboard uses the canonical ones above.

---

## Ops dashboard

Single-page HTML served at `http://localhost:8080/` (mirrored to GitHub Pages at `https://sandeepportfolio.github.io/arbiter-dashboard/`). Source: `arbiter/web/ops.html` (in-container) and `index.html` (at repo root, for Pages).

Pages:
- **Overview** — scanner stats, live opportunities, execution P&L, balance card
- **Mappings** — confirmed mappings + auto-discovery review queue + per-venue IDs
- **Trades** — execution_arbs ledger with per-leg detail
- **Audit** — math audit results + flag counts
- **Logs** — recent log lines from the running container
- **Operator panel** — kill-switch toggle, AUTO_EXECUTE override, supervisor controls (auth-gated via HMAC session)

To update the Pages dashboard: `cp arbiter/web/ops.html index.html && git add index.html && git commit -m "chore(dashboard): publish" && git push origin main`.

---

## Deployment

### Docker compose (prod)

```bash
/opt/homebrew/bin/docker compose -f docker-compose.prod.yml build arbiter-api-prod
/opt/homebrew/bin/docker compose -f docker-compose.prod.yml up -d arbiter-api-prod
```

Three containers:
- `arbiter-postgres-prod` — Postgres 16-alpine
- `arbiter-redis-prod` — Redis 7-alpine
- `arbiter-api-prod` — `ghcr.io/sandeepportfolio/arbiter:latest`, exposes port 8080

### Restart after env change

```bash
/opt/homebrew/bin/docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

### Tail logs

```bash
/opt/homebrew/bin/docker logs -f arbiter-api-prod
```

### DB shell

```bash
/opt/homebrew/bin/docker exec -it arbiter-postgres-prod psql -U arbiter -d arbiter_live
```

### Redis CLI

```bash
/opt/homebrew/bin/docker exec -it arbiter-redis-prod redis-cli
```

### IBKR Client Portal gateway

ForecastEx requires a running IBKR Client Portal gateway on the host at `https://localhost:5000/v1/api` (or `host.docker.internal:5000` from inside Docker). The gateway holds the SSO session. Re-auth via `/sso` when 401s appear.

---

## Database schema

Key tables (`arbiter_live` database, owner `arbiter`):

### `market_mappings`

| Column | Type | Notes |
|---|---|---|
| `canonical_id` | VARCHAR(200) PK | |
| `description` | TEXT | |
| `status` | VARCHAR(20) | confirmed / review / candidate / rejected / expired |
| `allow_auto_trade` | BOOLEAN | Only confirmed + identical can be true |
| `aliases` / `tags` | TEXT[] | |
| `kalshi_market_id` | VARCHAR(100) | |
| `polymarket_slug` | VARCHAR(200) | |
| `polymarket_question` | TEXT | |
| `forecastex_contract_id` | VARCHAR(100) | YES conid (right=C) |
| `forecastex_no_contract_id` | VARCHAR(100) | NO conid (right=P) — empty for multi-strike binaries |
| `forecastex_not_available` | BOOLEAN | Negative-cache flag |
| `resolution_criteria` | JSONB | |
| `resolution_match_status` | VARCHAR(40) | |
| `mapping_score` / `confidence` | DECIMAL(5,4) | |
| `notes` / `review_note` | TEXT | |
| `expires_at`, `last_validated_at`, `created_at`, `updated_at` | TIMESTAMPTZ | |

Partial indexes on each platform ID (only where non-empty).

### `execution_arbs`

`arb_id` (PK), `canonical_id`, `status` (pending/submitted/recovering/closed/failed/stranded), `net_edge`, `realized_pnl`, `unwind_pnl`, `opportunity_json` (JSONB with full opp snapshot), `analysis_md`, `created_at`, `updated_at`, `closed_at`.

### `execution_orders`

FK to `execution_arbs.arb_id`. `platform`, `market_id`, `side` (yes/no), `price`, `quantity`, `status`, `fill_price`, `fill_qty`, `timestamp`, `error`.

### `execution_incidents`

Severity-tagged events (`critical` / `warning` / `info`). Includes the trade-gate-blocked stream that grows while the profitability gate is open.

---

## Current state snapshot

> Captured 2026-05-28, post-NO-conid fix (commit `d157cb6`) and Docker rebuild.

| Metric | Value |
|---|---|
| Mode | live |
| Auto-execute | controlled by `AUTO_EXECUTE_ENABLED` env var |
| Kill switch (supervisor.is_armed) | false (unarmed) |
| Dry run | false |
| Total executions (lifetime) | 337 |
| Total realized P&L (lifetime) | +$136.82 |
| Naked legs | 0 |
| Stranded Kalshi positions | 16 (held to settle) |
| Confirmed mappings with FX conid | 24 |
| FX mappings with NO conid populated | 0 (HORC/SENM multi-strike — IBKR doesn't list Puts) |
| Kalshi balance | $353.91 |
| Polymarket US balance | $359.80 |
| ForecastEx balance | $298.95 |
| Scanner `dead_ask` skips | 4,274 (proves NO-conid safety gate is firing) |
| Incident rate (non-info) | 0.0% |
| Pre-fix phantom orders (NO-side on YES conid) | 16 |
| Post-fix phantom orders | 0 ✅ |

---

## Known limitations

1. **Multi-strike binaries (HORC, SENM, etc.) cannot be NO-side traded on ForecastEx via a single canonical_id.** The structural NO of `DEM_HOUSE_2026` is `GOP_HOUSE_2026`'s YES, which is a separate canonical_id. Cross-canonical 3-way arbs are not implemented; the system correctly skips these via the dead-ask gate rather than synthesizing a fake NO ask.
2. **IBKR rate-limit pressure during startup burst.** Initial NO-conid discovery for ~20 markets briefly trips the FX circuit breaker; it recovers within 30s. Steady-state is well under 10 rps.
3. **Polymarket US public gateway flakiness.** Many slugs return "unavailable on the public gateway" warnings — these are noise; the collector disables them and continues.
4. **Profitability gate verdict.** The system requires ~25 published opportunities AND average P&L ≥ $0.25 before flipping `verdict=profitable`. Until then, every opp is logged as a warning incident (not blocking execution if `AUTO_EXECUTE_ENABLED=true`).
5. **No HORC/SENM-style cross-canonical sizing.** A future enhancement could pair `DEM_HOUSE_2026.YES@ForecastEx + GOP_HOUSE_2026.YES@ForecastEx` as a synthetic riskless pair, but the current scanner pairs YES of one canonical with NO of the same canonical.

---

## Repository layout

```
arbiter/                   Main Python package
├── api.py                 aiohttp server + WebSocket fan-out
├── main.py                Orchestrator entry point
├── collectors/            Per-venue market data clients
│   ├── kalshi.py
│   ├── polymarket.py / polymarket_us.py
│   └── forecastex.py
├── execution/
│   ├── engine.py          Primary + secondary leg orchestration
│   ├── auto_executor.py   Gate stack + per-canonical lock
│   └── adapters/          Per-venue order adapters
├── scanner/arbitrage.py   Opportunity detection
├── mapping/               MarketMapping model, store, discovery
├── recovery/              Stranded reconciler + mitigation engine
├── monitor/balance.py     Per-platform balance polling + alerts
├── audit/                 Math auditor + P&L reconciler
├── safety/supervisor.py   Kill switch
├── profitability/         Profitability gate validator
├── utils/                 PriceStore, retry primitives, logger
├── config/settings.py     MARKET_SEEDS + ForecastExConfig + ScannerConfig
├── sql/init.sql           Schema bootstrap
└── web/                   Dashboard HTML

deploy/                    Deployment runbooks
scripts/                   Operational scripts
docker-compose.prod.yml    Prod stack definition
Dockerfile                 Python 3.12 slim runtime
requirements*.txt          Python deps (full + docker-only)
package.json + src/        TypeScript CLI (dry-run pipeline, not used in prod)
index.html                 Mirror of arbiter/web/ops.html for GitHub Pages
```

---

## Contributing / making changes

1. All test suites must pass: `python -m pytest arbiter/ -q` (1172 tests currently).
2. Use the GSD workflow (see `CLAUDE.md`) for any change. Bug fixes via `/gsd-debug`, features via `/gsd-execute-phase`.
3. For ForecastEx changes specifically, ensure the YES/NO conid invariants in `arbiter/collectors/forecastex.py:_build_price_point` and the executor `_submit` integrity gates remain intact.
4. New SQL: idempotent `ALTER TABLE ADD COLUMN IF NOT EXISTS` only. Migrations must roll forward cleanly on existing prod schemas.
5. Commit with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` when AI-assisted.
