# Arbiter — Handoff

**Mission:** Make real money. Live arbitrage trades between Kalshi and Polymarket US, scaling to hundreds of thousands of quote signals per minute across thousands of auto-discovered market pairs. Run continuously and autonomously.

**Priority order, no exceptions:**
1. First live trade executes and settles with reconciled PnL.
2. `AUTO_EXECUTE_ENABLED=true` — system trades unattended.
3. Signal throughput at hundreds-of-thousands/min (auto-discovery + matcher at scale).
4. Net-positive realized PnL, verified against exchange balances.

**Last update:** 2026-04-21 — after the Polymarket US pivot + scale work landed on `main` (25 commits, HEAD `f693f7c`). Code is ready. What's left is provisioning creds, funding accounts, and pressing go.

---

## 1. State of the code (as of `f693f7c`)

Shipped and tested:
- **Polymarket US integration** against `api.polymarket.us/v1` with Ed25519 header auth (payload is `{timestamp_ms}{METHOD}{path}` — body NOT signed, regression-pinned). REST client, WebSocket multiplex (100 slugs per conn, auto-reconnect), execution adapter.
- **Scanner rewrite** from O(n²) tick loop to event-driven O(1)-per-quote matcher with bounded queue + per-canonical debounce + emit throttle. Benchmark: 1000 canonical pairs × 3 updates/sec → 0.01 ms p99 match-to-emit latency, zero backpressure drops. Headroom is large — this is nowhere near saturated.
- **Auto-discovery pipeline** for market pairs, rate-limited to 2 rps per platform so it never starves live trading. Hand-curated `MARKET_SEEDS` is still wired as a baseline; auto-discovery adds candidates on top.
- **3-layer resolution-equivalence gate** for auto-promoting candidate pairs: structured-field check + Claude Haiku 4.5 LLM verifier (fail-safe to MAYBE) + 22+21 hand-labeled fixture corpus as CI regression guard.
- **8-condition auto-promote gate** with explicit gating on liquidity (orderbook depth ≥ `PHASE5_MAX_ORDER_USD × 2`), resolution date window, daily cap, 30-scan advisory cooling-off, LLM verdict.
- **PHASE4 + PHASE5 hard-locks** enforced in sequence BEFORE signing. Tests monkeypatch `_sign_and_send` and assert `call_count == 0` when either cap trips or supervisor is armed.
- **Observability:** 9 new Prometheus metrics (matcher latency histogram, backpressure drops, auto-promote rejections by reason, Ed25519 sign failures, WS sub count, etc.). Telegram heartbeat every 15 min while auto-exec is on.
- **Operational tooling:** `check_polymarket_us.py` signed round-trip with subprocess-verified secret-leak guard; `onboard_polymarket_us.py` Playwright-driven API key capture; `go_live.sh` end-to-end orchestrator.
- **Rollback:** `POLYMARKET_VARIANT=legacy` or `disabled` — config flip, < 2 min, no code revert.

Test state: 495 pass / 87 skip / 0 fail on `pytest -q`. 0 errors on `npx tsc --noEmit`.

What's NOT done and what it's waiting on:
- Real credentials in `.env.production` (operator has iOS-KYC'd Polymarket US).
- Real money in Kalshi prod + Polymarket US accounts.
- `go_live.sh` has never run against real endpoints on this machine.
- `test_first_live_trade.py` has never been invoked against prod.
- Auto-execute has never been flipped.

Those are the only gaps.

---

## 2. Architecture

```
                    ┌──────────────────┐
Kalshi REST/WS ────▶│                  │    matched pair   ┌──────────────────┐
                    │ MatchedPairStream├──────────────────▶│  AutoExecutor    │
Polymarket US WS ──▶│ (O(1) per quote, │  (bounded queue)  │  (7 policy gates)│
                    │  debounce,       │                   └────────┬─────────┘
                    │  backpressure)   │                            │
                    └────────┬─────────┘                            ▼
                             ▼                            ┌──────────────────┐
                    ┌──────────────────┐                  │ ExecutionEngine  │
                    │ ArbitrageScanner │                  │  place/fill/     │
                    │ (emits opp via   │◀─────────────────┤  cancel, +       │
                    │  subscribers)    │                  │  PHASE4/PHASE5   │
                    └────────┬─────────┘                  │  hard-locks      │
                             ▼                            └────────┬─────────┘
                    ┌──────────────────┐                           │
                    │ SafetySupervisor │◀──────────────────────────┤
                    │ (kill-switch,    │  is_armed gate            ▼
                    │  one-leg recover,│                  ┌──────────────────────┐
                    │  rate-limit)     │                  │ PolymarketUSAdapter  │
                    └────────┬─────────┘                  │  + Ed25519 signer    │
                             ▼                            │ KalshiAdapter        │
                    ┌──────────────────┐                  └──────────────────────┘
                    │ TelegramNotifier │──▶ operator phone
                    │ (retry + dedup + │    15-min heartbeat when auto-exec=on
                    │  heartbeat)      │
                    └──────────────────┘
```

### 7 AutoExecutor policy gates (first failing wins)

1. `AUTO_EXECUTE_ENABLED=false` (default OFF — flip to trade)
2. `supervisor.is_armed` (kill-switch)
3. `opportunity.requires_manual` (SAFE-06 manual review)
4. `mapping.allow_auto_trade` (per-pair flag)
5. Duplicate in 5s window
6. Notional > `MAX_POSITION_USD`
7. Executed ≥ `PHASE5_BOOTSTRAP_TRADES` (rollout cap — unset after first clean hour)

### 8 auto-promote gate conditions (`arbiter/mapping/auto_promote.py`)

1. `AUTO_PROMOTE_ENABLED=true`
2. Text similarity `score ≥ 0.85`
3. `resolution_check() == IDENTICAL`
4. `llm_verifier() == YES`
5. Orderbook depth ≥ `PHASE5_MAX_ORDER_USD × 2` on BOTH sides
6. `resolution_date` within 90 days
7. Daily promotion count < `AUTO_PROMOTE_DAILY_CAP` (default 20)
8. Cooling-off: first 30 scans advisory-only

### Signal throughput

The matcher is O(1) per quote event. With 1000 canonical pairs × 2 platforms × realistic quote frequency (Kalshi WS ~5 updates/sec/market during active trading, Polymarket US similar), steady-state is already 10–20k events/sec, i.e. **600k–1.2M signals/min**. Backpressure drops kick in before the matcher lags. To actually scale to hundreds of thousands of signals per minute, turn on auto-discovery + auto-promote (§4 Step 7). The infrastructure does not need further work.

---

## 3. Execute the handoff

Do all of this. In order.

### Step 1 — Sanity check

```bash
cd /Users/rentamac/Documents/arbiter
git pull origin main
cat STATUS.md | head -30
pytest -q                    # expect 495 pass / 87 skip / 0 fail
```

If the test counts don't match, something regressed since `f693f7c`. Fix it before continuing.

### Step 2 — Get credentials and funding

Three portals. You can drive the first two via the browser automation in `.mcp.json` (Playwright MCP is registered).

**2A — Kalshi prod:**
- `https://kalshi.com` (NOT `demo-api`). Log in. Settings → API → Create Key.
- `KALSHI_API_KEY_ID` → `.env.production`. RSA PEM → `./keys/kalshi_private.pem` via `python scripts/setup/_write_kalshi_pem.py`; `chmod 600 keys/kalshi_private.pem`.
- Fund the account via ACH/wire. Recommended ≥ $100. System is capped at $10/leg so this is not a leverage decision — it's a "do I have enough to run 10+ trades" decision.
- Verify: `python scripts/setup/check_kalshi_auth.py` exits 0 and prints balance.

**2B — Polymarket US:**
- Run `python scripts/setup/onboard_polymarket_us.py` → Playwright opens `https://polymarket.us/developer`. Log in. Script captures Ed25519 secret via `locator.input_value()` (never screenshot), writes both env vars, closes the secret-visible page, deletes any intermediate screenshots.
- If the onboard script hits a selector the portal has changed, just paste the key ID and base64 secret manually into `.env.production`.
- Deposit ≥ $20 USD in the Polymarket US iOS app or web portal.
- Verify: `python scripts/setup/check_polymarket_us.py` exits 0. The check never prints the secret.

**2C — Telegram:**
- Telegram Web or Desktop → DM `@BotFather`: `/newbot` → name → username. Capture token.
- DM `@userinfobot` → capture chat_id.
- Write `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to `.env.production`.
- Message the bot once from your Telegram account (required by Telegram's bot rules — bot can't DM you until you DM it).
- Verify: `python scripts/setup/check_telegram.py` exits 0.

**2D — Session secret:**
```bash
echo "UI_SESSION_SECRET=$(openssl rand -hex 32)" >> .env.production
```

### Step 3 — Finalize `.env.production`

```bash
cp .env.production.template .env.production    # if you haven't already
# Fill all <placeholder>s via Edit.
chmod 600 .env.production
grep '<' .env.production                        # must return nothing
```

Confirm `POLYMARKET_VARIANT=us` and `POLYMARKET_MIGRATION_ACK=ACKNOWLEDGED`.

### Step 4 — Orchestrator

```bash
./scripts/setup/go_live.sh
```

Runs in order, stops on first failure:
1. `validate_env.py` — shape checks
2. `docker compose -f docker-compose.prod.yml up -d` — stack up
3. `check_kalshi_auth.py` — signed round-trip + balance
4. `check_polymarket_us.py` — signed round-trip + balance
5. `check_telegram.py` — dry-test message
6. `check_mapping_ready.py` — at least one mapping with `allow_auto_trade=true`
7. `python -m arbiter.live.preflight` (with `PREFLIGHT_ALLOW_LIVE=1`) — 16-item preflight; 5a credentials-only + 5b live balance

Expected terminator: "ALL CHECKS PASSED".

If step 6 fails (no ready mapping), open `http://localhost:8080/ops` → Mappings → pick any pair with identical resolution criteria from the seeded MARKET_MAP → Confirm → Enable auto-trade. Re-run `go_live.sh`.

### Step 5 — First live trade

Have `http://localhost:8080/ops` open in a browser tab — the **Arm Kill Switch** button is there. Keep it visible.

```bash
docker compose -f docker-compose.prod.yml exec arbiter-api-prod \
    pytest -m live --live arbiter/live/test_first_live_trade.py -v -s
```

Sequence:
1. Preflight re-runs.
2. Opportunity detected. `evidence/05/first_live_trade_<ts>/pre_trade_requote.json` written.
3. 60s abort window — Arm Kill Switch in /ops if anything is off.
4. Both legs fire FOK (Kalshi + Polymarket US), $10/leg max.
5. 60s settlement wait.
6. `reconcile_post_trade` checks fees + PnL within ±$0.01.
7. Breach triggers `wire_auto_abort_on_reconcile` → kill-switch arms, Telegram pages.

**Pass = reconcile within tolerance OR auto-abort fired correctly.** Both prove the safety path.

### Step 6 — Flip to auto-mode

```bash
# .env.production:
AUTO_EXECUTE_ENABLED=true

docker compose -f docker-compose.prod.yml restart arbiter-api-prod
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod | grep auto_executor
```

First hour — monitor:
- `/ops` dashboard stays green
- `curl http://localhost:8080/api/metrics | grep auto_executor`
- Telegram heartbeat every 15 min with `realized_pnl` + `open_order_count`
- Exchange balances match dashboard `realized_pnl`

`PHASE5_BOOTSTRAP_TRADES=5` caps to first 5 auto-trades. After those clear cleanly, unset it to remove the cap.

### Step 7 — Scale to hundreds of thousands of signals

After 60 min of clean auto-mode (zero `kill_armed`, zero `one_leg`, reconciled within D-17):

```bash
# .env.production:
PHASE5_BOOTSTRAP_TRADES=              # unset — lifts trade cap
AUTO_PROMOTE_ENABLED=true             # turns on auto-promote of candidate mappings
AUTO_PROMOTE_DAILY_CAP=50             # raise from 20 as confidence grows
AUTO_PROMOTE_ADVISORY_SCANS=30        # cooling-off window before a promoted mapping can trade
AUTO_DISCOVERY_INTERVAL_SEC=300       # re-scan both platforms every 5 min for new markets
```

```bash
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

Expected ramp:
- `auto_discovery_candidates_pending` climbs as both platforms' market lists are pulled. Kalshi alone lists tens of thousands of markets; Polymarket US is narrower but growing.
- `auto_promote_rejections_total{reason="..."}` tells you which gate is rejecting what. `score_low` dominates at first (text similarity is conservative by design). `resolution_divergent` and `llm_no` filter the rest. Trust the rejections — they're keeping you from trading phantom arbs.
- Confirmed pairs (`allow_auto_trade=true`) grow steadily after the advisory cooling-off clears.
- `matched_pair_stream_events_total` climbs. At ~1000 confirmed pairs × ~5 quote updates/sec, you're at 300k signals/min. Push further by raising `AUTO_PROMOTE_DAILY_CAP` and shortening `AUTO_PROMOTE_ADVISORY_SCANS` once the first wave of promoted pairs has traded clean.

No hard ceiling on signal throughput until `matcher_backpressure_drops_total` starts ticking. At that point either (a) raise the bounded-queue `maxsize` in `arbiter/scanner/matched_pair_stream.py`, (b) shard the matcher across worker processes, or (c) filter out low-probability pairs at the scanner level.

### Step 8 — Steady state

Run indefinitely. The system is designed for continuous operation. The only time you intervene is:
- Balance discrepancy > $1 between dashboard and exchange — reconcile.
- `kill_armed` event with unexplained root cause — read the evidence dump, fix, re-arm.
- Regulatory or platform notice arrives — handle out-of-band.
- `realized_pnl` trend is negative beyond your tolerance — tune `MIN_EDGE_CENTS` up or narrow the auto-promote gates.

Everything else is self-managing: rate-limit backoff, WS reconnect, one-leg recovery, fee reconciliation, auto-abort on breach, heartbeat alerts.

---

## 4. Observability

| URL | Purpose |
|---|---|
| `http://localhost:8080/ops` | Dashboard + kill-switch |
| `http://localhost:8080/api/health` | `{"status":"ok"}` |
| `http://localhost:8080/api/readiness` | go/no-go + blocking_reasons[] |
| `http://localhost:8080/api/metrics` | Prometheus text |
| `http://localhost:8080/api/safety/status` | Kill-switch state + cooldown |
| `http://localhost:8080/api/safety/events` | Recent kill_armed, one_leg events |
| `http://localhost:8080/api/market-mappings` | All mappings |
| `http://localhost:8080/api/market-mappings/{canonical_id}/audit` | Per-mapping audit log |
| `http://localhost:8080/api/portfolio/positions` | Open + closed positions |

New metrics from the pivot:
- `polymarket_us_rest_latency_p99_ms` (gauge)
- `polymarket_us_ws_reconnects_total` (counter)
- `matched_pair_stream_events_total` (counter — your signal throughput)
- `matcher_backpressure_drops_total` (counter — raise queue size if this ticks)
- `matched_pair_latency_seconds` (histogram)
- `auto_discovery_candidates_pending` (gauge)
- `auto_promote_rejections_total{reason}` (counter — reason labels: `auto_promote_disabled`, `score_low`, `resolution_divergent`, `llm_no`, `liquidity_low`, `date_out_of_window`, `daily_cap`, `cooling_off`)
- `ed25519_sign_failures_total` (counter)
- `ws_subscription_count{platform}` (gauge)

Scrape config: see `deploy/README.md`.

---

## 5. Tunables

All live in `.env.production`. Restart `arbiter-api-prod` to pick up changes.

| Variable | Default | What it does |
|---|---|---|
| `AUTO_EXECUTE_ENABLED` | `false` | Global kill. `true` = system trades. |
| `MAX_POSITION_USD` | `10` | Per-leg notional cap. |
| `PHASE4_MAX_ORDER_USD` | `10` | Adapter hard-lock (defense in depth). |
| `PHASE5_MAX_ORDER_USD` | `10` | Adapter hard-lock (stricter; same value). |
| `PHASE5_BOOTSTRAP_TRADES` | `5` | First N auto-trades get extra logging. Unset to remove cap. |
| `MIN_EDGE_CENTS` | `2` | Minimum opportunity edge. Raise to filter thin spreads. |
| `SCAN_INTERVAL_SEC` | `1.0` | Legacy scan loop tick (matcher is event-driven and faster). |
| `POLYMARKET_VARIANT` | `us` | `us` / `legacy` / `disabled`. |
| `AUTO_PROMOTE_ENABLED` | `false` | Turn on auto-promotion of candidate mappings. |
| `AUTO_PROMOTE_DAILY_CAP` | `20` | Max new promotions per day. |
| `AUTO_PROMOTE_ADVISORY_SCANS` | `30` | Cooling-off window before a promoted pair can trade. |
| `AUTO_DISCOVERY_INTERVAL_SEC` | `300` | How often to re-poll both platforms for new markets. |
| `AUTO_DISCOVERY_BUDGET_RPS` | `2.0` | Rate-limit budget slice for discovery (remaining 18 rps for live ops on Polymarket US). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Alerts + heartbeat. |

Raising `MAX_POSITION_USD` also requires raising `PHASE4_MAX_ORDER_USD` and `PHASE5_MAX_ORDER_USD` — all three caps must move together.

---

## 6. File map

| Path | Purpose |
|---|---|
| `HANDOFF.md` | This file |
| `STATUS.md` | Test counts + commit log |
| `GOLIVE.md` | Full 13-section operator runbook |
| `docs/superpowers/specs/2026-04-21-polymarket-us-pivot-and-scale-design.md` | Pivot design |
| `docs/superpowers/plans/2026-04-21-polymarket-us-pivot-and-scale.md` | Implementation plan |
| `.env.production.template` | Env template — US section default, legacy commented |
| `.env.production` | Real credentials (gitignored, chmod 600) |
| `keys/kalshi_private.pem` | Kalshi RSA key (gitignored, chmod 600) |
| `docker-compose.prod.yml` | Production stack |
| `deploy/systemd/arbiter.service` | Bare-metal systemd unit |
| `deploy/README.md` | Deployment + scrape config |
| `scripts/setup/go_live.sh` | End-to-end orchestrator |
| `scripts/setup/check_polymarket_us.py` | US signed round-trip validator |
| `scripts/setup/check_polymarket.py` | Legacy CLOB validator (when VARIANT=legacy) |
| `scripts/setup/onboard_polymarket_us.py` | Playwright dev-portal onboarding |
| `scripts/setup/check_kalshi_auth.py` | Kalshi signed round-trip validator |
| `scripts/setup/check_telegram.py` | Telegram bot dry-test |
| `scripts/setup/validate_env.py` | `.env.production` shape checker |
| `arbiter/auth/ed25519_signer.py` | Ed25519 signer |
| `arbiter/collectors/polymarket_us.py` | US REST client |
| `arbiter/collectors/polymarket_us_ws.py` | US WebSocket multiplex |
| `arbiter/collectors/kalshi.py` | Kalshi client |
| `arbiter/execution/adapters/polymarket_us.py` | US execution adapter |
| `arbiter/execution/adapters/exceptions.py` | Shared `OrderRejected` |
| `arbiter/scanner/arbitrage.py` | Scanner + opportunity math |
| `arbiter/scanner/matched_pair_stream.py` | Event-driven O(1) matcher |
| `arbiter/mapping/resolution_check.py` | SAFE-06 Layer 1 (structured fields) |
| `arbiter/mapping/llm_verifier.py` | SAFE-06 Layer 2 (Haiku 4.5) |
| `arbiter/mapping/fixtures/*.json` | SAFE-06 Layer 3 (fixture corpus) |
| `arbiter/mapping/auto_discovery.py` | Auto-discovery pipeline |
| `arbiter/mapping/auto_promote.py` | 8-condition promote gate |
| `arbiter/notifiers/heartbeat.py` | 15-min Telegram heartbeat |
| `arbiter/live/preflight.py` | 16-item preflight |
| `arbiter/live/test_first_live_trade.py` | Step-5 harness |
| `arbiter/live/test_rollback_variants.py` | `POLYMARKET_VARIANT` smoke tests |
| `evidence/05/first_live_trade_*/` | Live-fire evidence (gitignored) |

---

## 7. Glossary

| Term | Meaning |
|---|---|
| SAFE-01 | Within 5s of kill-switch trip, all open orders cancelled |
| SAFE-03 | One-leg recovery — if one leg fills and the other fails, unwind within timeout |
| SAFE-04 | Rate-limit backoff + operator UI pills (ok/warn/crit) |
| SAFE-05 | Graceful shutdown — SIGTERM cancels open orders before exit |
| SAFE-06 | Resolution-criteria equivalence gate (identical/similar/divergent/pending) |
| D-17 | ±$0.01 PnL + fee tolerance for reconciliation |
| Signal | A `matched_pair` event from the matcher — one per both-sides-present quote update |
| MARKET_MAP | Canonical pair registry (hand-seeded + auto-discovered) |
| `allow_auto_trade` | Per-mapping flag; AutoExecutor gate G4 |
| `POLYMARKET_VARIANT` | Runtime selector: `us` / `legacy` / `disabled` |

---

## 8. Commands

```bash
# Test suite
pytest -q                          # default
pytest --run-slow -q               # includes 30s scale test
pytest -m live --live              # live-fire (real API calls)

# Preflight
POLYMARKET_VARIANT=disabled PREFLIGHT_ALLOW_LIVE=0 python -m arbiter.live.preflight   # dry
PREFLIGHT_ALLOW_LIVE=1 python -m arbiter.live.preflight                               # live

# tsc
npx tsc --noEmit

# End-to-end go-live
./scripts/setup/go_live.sh

# Onboarding
python scripts/setup/onboard_polymarket_us.py

# Production stack
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
docker compose -f docker-compose.prod.yml down

# Rollback (config flip, no code revert)
# edit .env.production: POLYMARKET_VARIANT=legacy  (or =disabled)
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

---

**Execute from §3 Step 1.**
