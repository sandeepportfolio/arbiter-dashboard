# Arbiter — Go-Live Operator Runbook

**Goal.** Take a clean checkout to a 24×7 automated cross-platform arbitrage system trading real dollars.

**What this doc covers.** Everything from credential provisioning to the first live trade to flipping auto-execute on. Written for an operator with shell, docker, and wallet familiarity but no prior Arbiter context.

**What you'll need (~2 hours end-to-end).**
- A credit card or bank account for Kalshi funding (~$100)
- A Polygon wallet with USDC on it (~$20–50 — a fresh throwaway, NOT your personal wallet)
- A Telegram account (for bot-based alerts)
- Docker + Docker Compose v2, OR a Linux host with Python 3.12 + systemd

---

## 0. Pre-flight gate check

Before you start provisioning, understand the safety architecture:

| Layer | Gate | Where enforced |
|---|---|---|
| **Adapter hard-lock** | `PHASE5_MAX_ORDER_USD=10` — every order notional is checked BEFORE any HTTP/CLOB call. `qty * price > cap` → adapter returns `_failed_order` silently, no network side effect. | `arbiter/execution/adapters/{kalshi,polymarket}.py` |
| **Position cap** | `MAX_POSITION_USD=10` — AutoExecutor checks per-leg notional before calling `engine.execute_opportunity`. | `arbiter/execution/auto_executor.py` |
| **Bootstrap cap** | `PHASE5_BOOTSTRAP_TRADES=5` — first N auto-trades, then stops. Lift once confident. | `AutoExecutor._consider_opportunity` |
| **Kill-switch** | `supervisor.is_armed=True` blocks AutoExecutor AND cancels all open orders within 5s. | `SafetySupervisor.trip_kill` |
| **Auto-execute toggle** | `AUTO_EXECUTE_ENABLED=false` (default) — nothing auto-trades until you flip this. | Env var |
| **Mapping allow-list** | `MARKET_MAP[canonical_id].allow_auto_trade=True` — only pairs you explicitly curate auto-trade. | Dashboard `/ops` → Mappings |
| **One-leg recovery** | If one leg fills and the other fails, `SafetySupervisor.handle_one_leg_exposure` unwinds automatically or pages you via Telegram. | `SafetySupervisor.handle_one_leg_exposure` |

Four independent layers gate any live trade. You can revoke any of them at any time without restart.

---

## 1. Credentials you must provision yourself

I cannot automate the following — they require your browser, your seed phrase, your KYC. Do them in order; each step takes 5–15 minutes.

### 1A. Kalshi production API key

1. Open https://kalshi.com (NOT `demo-api.kalshi.co`). Log in or sign up.
2. If you haven't yet: complete KYC (ID upload + personal info). Kalshi requires this for real trading.
3. Fund the account via **Account → Deposit** (ACH is free, wire is instant, minimum ~$100 recommended).
4. Navigate **Account → API Keys → Create Key**. Copy the Key ID.
5. Download the RSA private key — Kalshi shows it **once**, download it immediately.
6. Save the RSA key to `./keys/kalshi_private.pem` in the Arbiter checkout. This path is `.gitignore`d.
7. `chmod 600 ./keys/kalshi_private.pem` (Linux/mac) or equivalent file-permission restriction.

### 1B. Polymarket wallet + funding

1. Install MetaMask (or any Ethereum wallet). **Create a brand-new account** — do NOT use a wallet you use elsewhere. This is the Arbiter's arbitrage wallet; it should only ever hold trading funds.
2. Fund with ~$20–50 USDC on **Polygon mainnet** (chain ID 137). Routes:
   - Coinbase: Send → Polygon network → USDC
   - Any bridge (e.g., Across, Stargate): bridge USDC from Ethereum to Polygon
3. Export the wallet's private key: MetaMask → Account Details → Show Private Key. Paste into `.env.production` as `POLY_PRIVATE_KEY`.
4. Note the wallet address: this is `POLY_FUNDER`.
5. Open https://polymarket.com, connect the wallet, complete Polymarket's own KYC / deposit flow. Your USDC must appear in the Polymarket trading balance for the CLOB to accept orders.

### 1C. Telegram bot

1. In Telegram, search for `@BotFather`. Send `/newbot`, follow prompts.
2. Copy the bot token — this is `TELEGRAM_BOT_TOKEN`.
3. In Telegram, search for `@userinfobot` and message it. Reply contains your numeric chat ID — this is `TELEGRAM_CHAT_ID`.
4. **Send a message to your bot first** — Telegram won't deliver messages to a chat the bot has never spoken in.

### 1D. Operator dashboard password

Pick a strong password you'll use to log into `/ops`. This is `OPS_PASSWORD`. The dashboard auth uses HMAC-SHA256 session tokens signed by `UI_SESSION_SECRET` (generate a random 32-byte hex string via `openssl rand -hex 32`).

---

## 2. `.env.production`

```bash
cp .env.production.template .env.production
```

Open `.env.production` and fill in every `<placeholder>`:

```bash
DRY_RUN=false

# Database (docker compose will spin up arbiter-postgres-prod)
DATABASE_URL=postgresql://arbiter:<strong-password>@arbiter-postgres-prod:5432/arbiter_live
PG_USER=arbiter
PG_PASSWORD=<strong-password>
REDIS_URL=redis://arbiter-redis-prod:6379/0

# Kalshi prod
KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2
KALSHI_WS_URL=wss://api.elections.kalshi.com/trade-api/ws/v2
KALSHI_API_KEY_ID=<from-step-1A>
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_private.pem

# Polymarket prod
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLY_PRIVATE_KEY=<from-step-1B>
POLY_FUNDER=<wallet-address-from-step-1B>
POLY_SIGNATURE_TYPE=2

# Safety caps (all enforced; defense in depth)
PHASE4_MAX_ORDER_USD=10
PHASE5_MAX_ORDER_USD=10
MAX_POSITION_USD=10
PHASE5_BOOTSTRAP_TRADES=5

# Auto-execute DEFAULT OFF — flip after supervised first trade
AUTO_EXECUTE_ENABLED=false

# Telegram
TELEGRAM_BOT_TOKEN=<from-step-1C>
TELEGRAM_CHAT_ID=<from-step-1C>

# Dashboard auth
OPS_EMAIL=<your-email>
OPS_PASSWORD=<your-strong-password>
UI_SESSION_SECRET=<openssl rand -hex 32>

# Acknowledgments (preflight will check these)
POLYMARKET_MIGRATION_ACK=ACKNOWLEDGED
OPERATOR_RUNBOOK_ACK=ACKNOWLEDGED
```

---

## 3. Bring up production stack

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d

# Wait ~20s for healthchecks to settle, then verify:
curl -s http://localhost:8080/api/health | jq
# Expect: {"status":"ok", ...}

curl -s http://localhost:8080/api/readiness | jq
# Expect: {"ready": true, "blocking_reasons": [], ...}
```

If `ready: false`, the `blocking_reasons[]` array lists what's wrong. Fix and retry.

---

## 4. Telegram dry-test

```bash
docker compose -f docker-compose.prod.yml exec arbiter-api-prod python -m arbiter.notifiers.telegram
# Expect stdout: "Telegram dry-test OK — message delivered."
# Expect Telegram: "🧪 Arbiter Telegram dry-test ..."
```

If the CLI exits 1: `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is wrong, OR you haven't messaged the bot first.

---

## 5. Preflight (15-item checklist)

```bash
docker compose -f docker-compose.prod.yml exec arbiter-api-prod python -m arbiter.live.preflight
```

The runner checks 15 items (DB up, Redis up, Kalshi auth, Polymarket auth, private key readable, Telegram reachable, mapping has `allow_auto_trade=true`, etc.). Any red item aborts — fix it before moving on.

---

## 6. Curate at least one market mapping

Open http://localhost:8080/ops → log in with `OPS_EMAIL` / `OPS_PASSWORD`.

In the **Mappings** panel:
1. Review each `MARKET_MAP` entry's Kalshi + Polymarket pair.
2. Find one where the resolution criteria are unambiguously identical (e.g., "Will X happen by date Y?" on both platforms, same wording, same cutoff).
3. Click **Confirm** → adds `status=confirmed`.
4. Click **Enable auto-trade** → toggles `allow_auto_trade=true`.
5. Verify `/api/market-mappings/{canonical_id}/audit` shows your actions: `actor=<your-email>, field=allow_auto_trade, old=false, new=true`.

⚠️ **You are responsible for judging resolution-criteria equivalence.** If Kalshi resolves on "official government count" and Polymarket on "major news networks consensus", those pairs can diverge and leave you with naked exposure. Start with one *very tight* pair.

---

## 7. First supervised live trade (Phase 5 Plan 05-02)

This is the only live-fire gate that places a real trade. You must be present.

```bash
# Ensure you're looking at /ops in a browser with ARM button in reach
docker compose -f docker-compose.prod.yml exec arbiter-api-prod \
  pytest -m live --live arbiter/live/test_first_live_trade.py -v -s
```

What happens (the test is verbose — read the output):
1. Runs preflight; aborts if any blocker.
2. Selects an opportunity from the price store where `mapping.allow_auto_trade=true`.
3. Writes `pre_trade_requote.json` to `evidence/05/first_live_trade_<ts>/`.
4. **Sleeps 60 seconds** — this is your abort window. If you hit ARM in the dashboard during this window, the trade does NOT fire.
5. Places both legs via `engine.execute_opportunity`.
6. Waits `POLYGON_SETTLEMENT_WAIT_SECONDS=60` for Polymarket settlement.
7. Runs `reconcile_post_trade`: compares expected vs actual fees + PnL within ±$0.01.
8. `wire_auto_abort_on_reconcile`: if reconcile breach, `trip_kill` fires and cancels any remaining open orders. Telegram fires.
9. Dumps evidence to `evidence/05/first_live_trade_<ts>/`.

**Pass conditions** — either:
- Reconcile PASS: both legs within ±$0.01 fee/PnL tolerance → test passes, phase gate flips to PASS.
- Reconcile breach triggers auto-abort correctly: trip_kill fires, all open orders cancelled, Telegram alert received → test also passes (proves the safety path).

**Fail conditions:**
- Reconcile breach but auto-abort didn't fire → test fails, investigate.
- Orders hang in SUBMITTED for >60s → test fails, kill-switch manually.

---

## 8. Flip to auto-mode

Once Step 7 passes cleanly:

```bash
# Edit .env.production:
AUTO_EXECUTE_ENABLED=true

# Restart the arbiter container (DB + Redis keep running)
docker compose -f docker-compose.prod.yml restart arbiter-api-prod

# Watch logs
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod | grep -E "auto_executor"

# Watch metrics
watch -n 5 'curl -s http://localhost:8080/api/metrics | grep auto_executor'
```

In the logs you'll see `auto_executor.started enabled=True max_position_usd=10.0 bootstrap_trades=5`. The first 5 persisted opportunities on an `allow_auto_trade=true` mapping will auto-execute (respecting all safety gates), then the bootstrap cap kicks in.

**Operator watch for the first hour:**
- `/ops` dashboard stays green (kill-switch disarmed, rate-limit pills ok, no incidents).
- Telegram silent (no `kill_armed`, no `one_leg`).
- `/api/metrics` shows `arbiter_auto_executor_executed` incrementing with `arbiter_pnl_total` trending ≥ 0.
- Account balances on both platforms match what the dashboard claims.

## 9. Lift the bootstrap cap (optional, after several hours clean)

```bash
# .env.production: remove or unset PHASE5_BOOTSTRAP_TRADES
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

From this point the system trades continuously within the `MAX_POSITION_USD` cap.

---

## 10. Rollback / abort procedures

**Instant (one-click in dashboard):**
- `/ops` → Arm Kill Switch → all open orders cancelled within 5s; AutoExecutor refuses new trades.

**Scripted:**
```bash
curl -X POST http://localhost:8080/api/kill-switch \
  -H "Content-Type: application/json" \
  -H "Cookie: arbiter_session=<get-from-login>" \
  -d '{"action":"arm","reason":"manual abort"}'
```

**Graceful shutdown (preserves state):**
```bash
docker compose -f docker-compose.prod.yml down
# Sends SIGTERM → arbiter.main runs SAFE-05 shutdown sequence:
#   1. shutdown_state="shutting_down" broadcast to dashboard
#   2. cancel_all runs against both venues
#   3. clean exit with 35s grace
```

**Hard stop (emergency):**
```bash
docker compose -f docker-compose.prod.yml kill
# Immediate SIGKILL. Orders may remain open on the exchange.
# Recovery: restart the stack; arbiter.main calls reconcile_non_terminal_orders
# on startup which cancels any orphans.
```

---

## 11. Troubleshooting matrix

| Symptom | Diagnostic | Fix |
|---|---|---|
| `/api/health` returns 502 | `docker compose logs arbiter-api-prod` shows startup error | Missing env var or unreadable key. Check `.env.production` + `./keys/kalshi_private.pem` |
| `/api/readiness` `ready=false` | `blocking_reasons[]` lists each blocker | Fix each listed item; re-query |
| Preflight #7 fails | `mapping_has_allow_auto_trade: false` | In `/ops` → Mappings, Enable auto-trade on at least one confirmed pair |
| Preflight #10 fails | `telegram_configured: false` | Run `python -m arbiter.notifiers.telegram`; fix token/chat id |
| AutoExecutor never fires | `/api/metrics` shows `arbiter_auto_executor_skipped{reason=...}` high | Reason tells you which gate blocked — `disabled` → env var; `not_allowed` → mapping; `over_cap` → `MAX_POSITION_USD` too low |
| Kill-switch stuck ARMED | `/api/safety/status` shows `cooldown_remaining > 0` | Wait cooldown, then POST reset |
| Telegram silent after kill | Check `/api/safety/events` | If event logged, Telegram token stale — regenerate via @BotFather |
| Orders stuck SUBMITTED after crash | On restart, check logs for `reconcile.started` | Automatic on startup; if stuck, manual cancel via exchange portal |
| `/api/metrics` missing `auto_executor_*` lines | Running in api-only mode (no scanner) | Not an error — metrics exist when the full stack runs |
| PnL reconcile mismatches | `/api/reconciliation` shows discrepancies | Check platform fee rates — Kalshi quadratic, Polymarket per-category. File as incident. |

---

## 12. Secrets hygiene

- `.env.production` and `keys/*.pem` are `.gitignore`d. NEVER commit them.
- On the host, restrict permissions: `chmod 600 .env.production keys/*.pem`.
- Rotate `UI_SESSION_SECRET` quarterly (forces re-login, invalidates stolen tokens).
- Rotate Kalshi API key quarterly (Account → API Keys → Revoke → New).
- Never reuse the Polymarket private key anywhere else. Treat it as single-purpose.
- If a key leaks: trip kill-switch, revoke on-platform, withdraw funds, rotate.

---

## 13. When to stop and ask for help

Do not push through these:
- Auto-executor `skipped{reason="over_cap"}` appears on a real opportunity. The scanner's `suggested_qty` is too large — either tune `MAX_POSITION_USD` or investigate why the opportunity is oversized.
- Reconcile breach that wasn't auto-aborted. The safety net has a hole; stop and investigate.
- One-leg exposure (SAFE-03 incident) that auto-recovery couldn't unwind. Telegram will page you with unwind instructions; execute manually on the exchange.
- Any discrepancy > $1 between dashboard `realized_pnl` and actual platform balances. Reconciler thinks the trade is clean but money is missing; stop everything.

In all of these: arm kill-switch, `docker compose down`, dump `evidence/05/*/`, analyze.
