# Arbiter Handoff — for the Next Agent / Operator

**Purpose:** You (human or AI) are picking up this repo and asked to finish getting Arbiter to live automated trading. This doc tells you exactly where things stand, what's automated, what needs you, and how to finish.

**Audience:**
- A human operator the user asks to help
- Another AI agent picking up on a different machine
- The user themselves when they come back after a break

Read this once end-to-end, **then read §0 (Polymarket US pivot) before anything else**, then work through **§3 Handoff checklist**.

---

## 0. URGENT — Polymarket integration is mid-pivot (added 2026-04-21)

**The existing `arbiter/collectors/polymarket.py` targets `clob.polymarket.com` (the non-US CLOB) and will not work for US-based operators.** `clob.polymarket.com` is IP-geofenced for US users and the Polymarket TOS §2.1.4 prohibits VPN circumvention. KYC-on-withdrawal freezes funds on accounts flagged as US.

**The new target is `api.polymarket.us`** — the CFTC-regulated US DCM Polymarket stood up after acquiring QCX/QCEX in 2025. As of April 2026 it is live with:

- REST + WebSocket API at `https://api.polymarket.us/v1/`
- **Ed25519 auth** (`X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature`) — not EOA signing
- Official Python SDK at [`Polymarket/polymarket-us-python`](https://github.com/Polymarket/polymarket-us-python) (`pip install polymarket-us`)
- Developer portal at [`polymarket.us/developer`](https://polymarket.us/developer) where operators generate API credentials AFTER completing iOS-app KYC
- Fees: 0% maker, 0.75%–1.80% taker (differs from non-US CLOB — must update `polymarket_order_fee()`)
- Access-gated: iOS-app KYC + invite/waitlist (expected broader rollout Q3–Q4 2026)
- State exclusions: NV, TN, MA, CT

**What needs to change in the codebase before Phase 5 live-fire can run:**

| File | Change |
|---|---|
| `arbiter/config/settings.py:400-416` | Replace `POLY_PRIVATE_KEY` / `POLY_FUNDER` / `POLY_SIGNATURE_TYPE` with `POLYMARKET_US_API_KEY` / `POLYMARKET_US_API_SECRET` (Ed25519 pair). Keep `POLYMARKET_CLOB_URL` as `https://api.polymarket.us/v1`. |
| `arbiter/collectors/polymarket.py` | Replace `py-clob-client` calls with `polymarket-us` SDK. Auth model changes from order signing to header signing. |
| `arbiter/scanner/arbitrage.py::polymarket_order_fee()` | Replace market-category rates with 0% maker / 0.75–1.80% taker, per category from the Polymarket US docs. |
| `arbiter/live/fixtures/polymarket_production.py` | Replace the `POLY_PRIVATE_KEY` assertion with credentials check. |
| `arbiter/live/preflight.py:234-240` | Same rename. |
| `.env.production.template` | Replace Polymarket section — no more EOA private key, funder, or signature type. |
| `scripts/setup/check_polymarket.py` | Swap CLOB auth round-trip for `api.polymarket.us` `GET /v1/markets` + balance endpoint using Ed25519 signing. |

**Estimated effort:** 3–5 days focused dev, tracked as a new phase (not yet numbered — add via `/gsd-add-phase` when ready).

**Alternative path if Polymarket US invite is delayed:** Kalshi ↔ IBKR ForecastEx. ForecastEx has a first-class public API today (via IBKR TWS/Web API, security type `OPT`, exchange `FORECASTX`), zero commission, no invite wait. Weaker overlap with Kalshi on sports, strong on macro. Same rewrite scope in `arbiter/collectors/`. Would be an entirely new collector file.

**Killed paths — do not re-propose:**
- Using `clob.polymarket.com` via VPN (geo-block, TOS, KYC-on-withdrawal freeze)
- Polymarket outcome tokens on DEX secondary (no meaningful liquidity, same CEA exposure)
- LLC / offshore entity structures to access non-US CLOB (no documented working setup, KYC chokepoint on withdrawal)
- Reverse-engineering the Polymarket US iOS private API (the public REST API covers everything the app does)

**MCP setup added to the repo:** `.mcp.json` registers two MCP servers for the next agent:
- **`playwright`** (`@playwright/mcp`) — browser automation for Polymarket US dashboard, Telegram Web, Kalshi dashboard. Vision capability enabled.
- **`computer-use`** (`@github/computer-use-mcp`) — full Windows/macOS desktop control for native-app flows (Telegram Desktop, MetaMask popup, anything outside a browser). Runs with `--yolo` (auto-approve).

Next agent should `/mcp` connect both and drive web-based credential generation directly rather than asking the operator to click.

---

## 1. Project state (as of the last commit you'll see on `main`)

### What's built + tested (code complete)

- **Phases 1–4:** API integration fixes, execution hardening, safety layer, sandbox validation. **All shipped.** Phase 4 gate status is `PASS` (see `.planning/phases/04-sandbox-validation/04-VALIDATION.md`). Demo Kalshi live-fire validated G-1..G-5 safety behaviors.
- **Phase 5 (Live Trading):**
  - `05-01` complete — arbiter/live/ harness, 15-item preflight runner, PHASE5_MAX_ORDER_USD adapter hard-lock.
  - `05-02` **code complete, live-fire deferred**. `arbiter/live/test_first_live_trade.py` exists and is ready to run. It requires operator presence (60-second abort window + kill-switch watch).
- **Phase 6 (Production Automation):** all 6 plans complete.
  - `06-01` — `AutoExecutor` with 7 policy gates, wired in `arbiter.main.run_system`
  - `06-02` — `docker-compose.prod.yml`, systemd unit, logrotate, deploy/README.md
  - `06-03` — `TelegramNotifier` retry + dedup; `python -m arbiter.notifiers.telegram` dry-test CLI
  - `06-04` — `GET /api/metrics` Prometheus endpoint with 15 metric families
  - `06-05` — MARKET_MAP hot-reload + audit log + `GET /api/market-mappings/{id}/audit`
  - `06-06` — `GOLIVE.md` 13-section operator runbook
- **Full regression:** 407 Python + JS tests green.

### What is explicitly NOT done (and why)

The following cannot be automated — **they require a human in-loop** because they involve real money, real identity verification, or platform UIs with CAPTCHAs/2FA:

1. Kalshi **production** API key creation (KYC-gated)
2. Kalshi account funding via ACH/wire
3. **Polymarket US iOS-app KYC** (was: Polygon wallet generation — see §0 for pivot)
4. **Polymarket US API credential generation at `polymarket.us/developer`** (was: USDC → Polygon deposit flow)
5. Telegram `@BotFather` bot creation (can be automated via Playwright MCP + Telegram Web now that `.mcp.json` is registered — see §0)
6. Operator judgment on which `MARKET_MAP` pair to auto-trade (legal/trading decision)
7. Watching the first supervised live trade with kill-switch in reach

Every step above is broken down in **GOLIVE.md §1**. Total time: ~2 hours if done end-to-end — **plus 3–5 days of dev for the Polymarket US collector rewrite (see §0).**

### Snapshot of the last live-bring-up attempt (2026-04-20/21)

- **Kalshi prod creds:** in place. API key ID stored in `.env.production` (gitignored). `keys/kalshi_private.pem` (RSA) written via helper `scripts/setup/_write_kalshi_pem.py` which parses a single-line PEM paste and re-serializes with 64-col formatting + `chmod 600`.
- **`.env.production`:** scaffolded. Auto-generated `PG_PASSWORD` (`openssl rand -base64 24`) and `UI_SESSION_SECRET` (`openssl rand -hex 32`) filled in. `POLYMARKET_MIGRATION_ACK` + `OPERATOR_RUNBOOK_ACK` set to `ACKNOWLEDGED`. **Polymarket fields still blocked on §0 pivot.** Telegram fields still blank (user has not yet chatted with @BotFather).
- **Docker:** Desktop came up, dev-stack Postgres/Redis confirmed healthy. Prod stack not yet brought up (`go_live.sh` step 2 blocked on §0 rewrite).
- **`.mcp.json`:** committed to repo. Playwright + computer-use MCP servers registered for the next agent.

---

## 2. Architecture you need to know in one screen

```
                    ┌──────────────────┐
Kalshi REST  ──────▶│                  │    opportunity    ┌──────────────────┐
                    │  ArbitrageScanner├──────────────────▶│  AutoExecutor    │
Polymarket CLOB ───▶│   (scan every N) │                   │  (7 policy gates)│
                    └──────────────────┘                   └────────┬─────────┘
                                                                    │
                                                                    ▼
                    ┌──────────────────┐                  ┌──────────────────┐
                    │ SafetySupervisor │◄─────────────────┤ ExecutionEngine  │
                    │ (kill-switch,    │  is_armed gate   │  place/fill/cancel│
                    │  one-leg, rate-  │                  └──────────────────┘
                    │  limit, shutdown)│
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ TelegramNotifier │  → operator phone
                    │ (retry, dedup)   │
                    └──────────────────┘
```

**The 7 AutoExecutor policy gates (in order, first failing wins):**
1. `AUTO_EXECUTE_ENABLED=false` — global kill (default OFF)
2. `supervisor.is_armed` — kill-switch held
3. `opportunity.requires_manual` — SAFE-06 operator review required
4. `mapping.allow_auto_trade` — per-pair allow-list (default False per mapping)
5. Duplicate in 5s window — scanner re-emit dedup
6. Notional > `MAX_POSITION_USD` — position cap
7. Executed >= `PHASE5_BOOTSTRAP_TRADES` — rollout cap

Any one of these holding blocks auto-execution. All four safety layers are revocable live without restart.

---

## 3. Handoff checklist

Work through this in order. Each step has a clear pass/fail outcome.

### Step 1 — Sanity check the repo state

```bash
git checkout main
git pull origin main
cat README.md | head -40      # orientation
cat GOLIVE.md | head -30      # the path we're on
cat .planning/STATE.md        # last session's end state
```

Expected: on `main`, up to date, no merge conflicts, `GOLIVE.md` section 1 visible.

### Step 2 — Provision credentials (human-only, ~1.5h)

Follow **GOLIVE.md §1** exactly. Three external portals + one local step:

| Step | Portal | Output |
|---|---|---|
| 1A | kalshi.com (NOT demo-api) | `KALSHI_API_KEY_ID` + `./keys/kalshi_private.pem` + ≥$100 balance |
| 1B | MetaMask → Polygon → polymarket.com | `POLY_PRIVATE_KEY` (hex) + `POLY_FUNDER` (address) + ≥$20 USDC in Polymarket trading balance |
| 1C | @BotFather + @userinfobot on Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (message the bot first!) |
| 1D | Local shell | `UI_SESSION_SECRET=$(openssl rand -hex 32)` |

**Security rules for any agent doing this step:**
- NEVER paste the private key into a shared channel, screenshot, or log file.
- Store it in `.env.production` (gitignored) with file permissions `chmod 600 .env.production`.
- If you're an AI agent and the user gives you the private key, use it only for writing into `.env.production` via the Write tool, then **do not quote it back in your responses**.
- The RSA private key should live at `./keys/kalshi_private.pem` — also `chmod 600`.

### Step 3 — Fill `.env.production`

```bash
cp .env.production.template .env.production
# Edit every <placeholder>. Follow inline comments.
chmod 600 .env.production
```

If you're an AI agent, use Edit to replace each placeholder. After editing, confirm `.env.production` has no `<` characters remaining (that's how we detect missed placeholders).

### Step 4 — Run the one-shot orchestrator

```bash
./scripts/setup/go_live.sh
```

This runs, in order, stopping on any failure:
1. `validate_env.py` — shape/sanity of `.env.production` (catches template leftovers, demo URLs, bad formats)
2. `docker compose -f docker-compose.prod.yml up -d` — brings up Postgres/Redis/arbiter-api
3. `check_kalshi_auth.py` — signed round-trip against Kalshi prod, reads balance
4. `check_polymarket.py` — wallet validation + USDC.e balance + CLOB auth round-trip
5. `check_telegram.py` — dry-test message to your chat
6. `check_mapping_ready.py` — verifies ≥1 `MARKET_MAP` entry has `allow_auto_trade=true`
7. `python -m arbiter.live.preflight` — the 15-item preflight runner

**Expected:** all 7 pass, final "ALL CHECKS PASSED" banner, suggested next command.

**Common failures + fixes:**
- `check_kalshi_auth.py` → HTTP 401: you likely pasted the **demo** key or the PEM doesn't match the key ID. Re-download.
- `check_polymarket.py` → "USDC.e balance < $5": bridge more USDC to the wallet and retry.
- `check_telegram.py` → "Telegram disabled": your bot token/chat id is wrong, OR you haven't messaged the bot yet (Telegram rule: bot can't DM you until you've DMed it first).
- `check_mapping_ready.py` → "no mapping ready": open http://localhost:8080/ops, log in, go to Mappings, pick a pair with identical resolution criteria, click Confirm → Enable auto-trade.

### Step 5 — The first supervised live trade

This is the ONE step an AI agent should NOT run autonomously without the user present. Real money will move.

**If you are the operator:** open the dashboard in a browser at `http://localhost:8080/ops`. Make sure the Arm Kill Switch button is visible. Then run:

```bash
docker compose -f docker-compose.prod.yml exec arbiter-api-prod \
    pytest -m live --live arbiter/live/test_first_live_trade.py -v -s
```

Sequence of events:
1. Preflight runs (should pass immediately).
2. An opportunity is detected. Pre-trade requote is written to `evidence/05/first_live_trade_<ts>/pre_trade_requote.json`.
3. **60-second abort window.** If anything looks wrong, hit **Arm Kill Switch** in `/ops`. The test will detect `supervisor.is_armed=True` and skip execution.
4. If you don't abort, both legs fire (Kalshi FOK + Polymarket FOK), capped at $10 per leg.
5. 60-second Polymarket settlement wait.
6. `reconcile_post_trade` checks fees and PnL within ±$0.01.
7. If reconcile breach → `wire_auto_abort_on_reconcile` trips kill-switch, cancels any remaining orders, Telegram pages you.

**Pass = either:** (a) reconcile within tolerance, OR (b) auto-abort fired correctly on breach. Both prove the safety path.

**If you are an AI agent:** STOP here. Tell the user: "Credentials are validated, preflight is green, the system is ready. Run this command when you're ready to run the first live trade with the kill-switch in reach: ..." Do not run it yourself. Paste the command and the expected behavior and wait.

### Step 6 — Flip to auto-mode (only after Step 5 passes cleanly)

```bash
# Edit .env.production:
#   AUTO_EXECUTE_ENABLED=true

docker compose -f docker-compose.prod.yml restart arbiter-api-prod
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod | grep auto_executor
```

Watch:
- Dashboard `/ops` stays green
- `curl http://localhost:8080/api/metrics | grep auto_executor`
- Telegram silent (no kill_armed / one_leg)
- Account balances on both platforms match dashboard `realized_pnl`

For the first hour after flip, stay near the machine. `PHASE5_BOOTSTRAP_TRADES=5` caps the system to its first 5 auto-trades — enough to prove correctness but small enough that a catastrophic bug only costs $50.

### Step 7 — Lift bootstrap cap (optional, after Step 6 clean for several hours)

```bash
# .env.production: delete or unset PHASE5_BOOTSTRAP_TRADES
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

From here the system trades continuously within the `MAX_POSITION_USD=$10` per-leg cap, 24×7.

---

## 4. Observability (for ongoing monitoring)

| URL | Purpose |
|---|---|
| `http://localhost:8080/ops` | Operator dashboard (login with OPS_EMAIL/OPS_PASSWORD) |
| `http://localhost:8080/api/health` | `{"status":"ok"}` when alive |
| `http://localhost:8080/api/readiness` | go/no-go with `blocking_reasons[]` |
| `http://localhost:8080/api/metrics` | Prometheus text (scrape with the config in deploy/README.md) |
| `http://localhost:8080/api/safety/status` | Kill-switch state + cooldown |
| `http://localhost:8080/api/safety/events` | Recent safety events (kill_armed, one_leg, etc.) |
| `http://localhost:8080/api/market-mappings/{canonical_id}/audit` | Per-mapping audit log (who toggled what when) |

---

## 5. Known decisions + why

- **Default `AUTO_EXECUTE_ENABLED=false`.** Flipping this requires an explicit action. A freshly-cloned repo on a fresh machine never auto-trades. This is a design invariant — do not change the default.
- **`MAX_POSITION_USD=$10`.** Small enough that a bug costs $10. Raise only after weeks of clean auto-mode.
- **`PHASE5_BOOTSTRAP_TRADES=5`.** First N auto-trades get extra logging, then the cap kicks in. Lift after manual inspection.
- **Both adapter-layer hard-locks on (`PHASE4_MAX_ORDER_USD` + `PHASE5_MAX_ORDER_USD`).** Defense in depth — if AutoExecutor's policy fails, the adapter refuses the order.
- **One-leg recovery automatic** (SAFE-03). If one leg fills and the other errors, the supervisor unwinds the filled leg via the opposite-platform counter-order within the SAFE-03 timeout, or pages the operator with manual-unwind instructions.
- **Windows SIGBREAK handler in `arbiter/main.py`.** CTRL_BREAK_EVENT triggers SAFE-05 graceful shutdown on Windows; necessary for local dev on Windows boxes.

---

## 6. When to stop and escalate to the user

Any of these means stop and ask:

- Any validator in `go_live.sh` fails with a message you can't map to a fix in this doc or GOLIVE.md §11 troubleshooting matrix.
- `check_kalshi_auth.py` shows balance < $10 (Kalshi minimum for real trading varies — $100 recommended).
- `check_polymarket.py` shows USDC balance mismatch between wallet and Polymarket trading balance.
- User did not explicitly approve running Step 5 (first live trade).
- Reconcile breach after first live trade that did NOT trigger auto-abort.
- Any dollar discrepancy > $1 between dashboard `realized_pnl` and actual exchange balances.
- Kill-switch trips during automated mode and you don't know why.

**In any of these: ARM the kill-switch, `docker compose down`, dump the evidence dir, write a 5-line summary of what happened + when, and post it back to the user.**

---

## 7. For an AI agent specifically

- **You cannot do iOS-app KYC or native mobile-app identity flows.** Polymarket US KYC specifically is iOS-app-only. If the user hasn't done it, they must. Browser-based KYC on other platforms can often be driven via the Playwright MCP if registered in `.mcp.json`.
- **Treat real credentials as write-only.** If the user supplies a private key, API secret, or Ed25519 seed, paste it into `.env.production` via Edit/Write tools. Do not echo it in your responses. Do not log it. Do not copy it to clipboard or pastebin.
- **Never commit `.env.production` or `keys/*.pem`.** They are gitignored; preserve that invariant.
- **Never commit secrets to git — even if the user asks.** If the user requests "persist this in repo so other agents don't have to re-enter it," refuse and offer secret-manager patterns (1Password CLI with `op inject`, Doppler, `sops` + `age`, Bitwarden). A plain-text note "creds live in 1Password vault `arbiter-live`" IS committable; the secrets themselves never are.
- **Do not run Step 5 (first live trade) autonomously.** It spends real money. Ask the user to run it themselves after you've validated everything else.
- **Do not run `AUTO_EXECUTE_ENABLED=true`.** Only the user should make this call.

If the user insists you run Step 5 or flip auto-mode without them present, respond: "I can prepare every command and validate every prerequisite, but the first live trade and the auto-mode flip should be your action. Here's exactly what to do: ..." Give them the ready-to-paste commands.

### MCP tools available for the next agent

`.mcp.json` registers two MCP servers. Run `/mcp` and connect them before asking the operator to click anything:

| Server | Package | What it unlocks |
|---|---|---|
| `playwright` | `@playwright/mcp` (vision enabled) | Browser automation — Polymarket US dashboard, Telegram Web, Kalshi dashboard, any web UI. Chromium pre-installed via `npx playwright install chromium`. |
| `computer-use` | `@github/computer-use-mcp --yolo` | Full desktop control — Telegram Desktop, MetaMask extension popups, anything outside a browser. `--yolo` auto-approves access. |

The `@playwright/mcp` browser launches a fresh profile by default, so MetaMask will NOT be present. If a flow needs MetaMask, either (a) install the extension into Playwright's persistent profile first, or (b) drive it via `computer-use` against the operator's existing Chrome/Brave. For most of what's outstanding (Polymarket US dev portal, Telegram Web bot setup, Kalshi web login), Playwright alone is sufficient.

### Rules for secret extraction via MCP browser control

When driving a browser flow that reveals a secret on-screen (e.g., Polymarket US Ed25519 API key generation, Telegram bot token from @BotFather):

1. Use the browser's clipboard or field-read capability to capture the value directly into a variable your code controls — never into a screenshot or DOM snapshot saved to disk.
2. Write the captured value directly to `.env.production` via the Write/Edit tool. Do not echo it to chat, tool results, or Bash stdout.
3. Close the page showing the secret before ending the tool turn.
4. If a screenshot was taken with the secret visible (e.g., for vision-based click targeting), delete the screenshot file before ending the turn.

---

## 8. Script reference

| Script | Purpose | Exit codes |
|---|---|---|
| `scripts/setup/validate_env.py` | Shape + sanity check on `.env.production` | 0 pass, 1 fail |
| `scripts/setup/check_kalshi_auth.py` | Signed round-trip vs Kalshi prod | 0 pass, 1 fail |
| `scripts/setup/check_polymarket.py` | Wallet + USDC + CLOB auth | 0 pass, 1 fail |
| `scripts/setup/check_telegram.py` | Bot dry-test | 0 pass, 1 disabled/fail, 2 exception |
| `scripts/setup/check_mapping_ready.py` | ≥1 `MARKET_MAP` entry auto-trade-ready | 0 pass, 1 fail |
| `scripts/setup/go_live.sh` | Orchestrates all of the above + preflight | exits on first fail |
| `python -m arbiter.live.preflight` | 15-item pre-live-trade checklist | 0 pass, 1 any blocker |
| `python -m arbiter.notifiers.telegram` | Telegram dry-test (wrapped by check_telegram.py) | 0/1/2 |

All five setup checks NEVER print actual secret values. They print presence, length, address, balance, and public metadata only.

---

## 9. Files on disk you care about

| Path | Purpose | Committed? |
|---|---|---|
| `GOLIVE.md` | Full operator runbook (13 sections) | Yes |
| `HANDOFF.md` | This file | Yes |
| `.env.production.template` | Template with inline provisioning instructions | Yes |
| `.env.production` | Real credentials | **NO (gitignored)** |
| `keys/kalshi_private.pem` | Kalshi RSA private key | **NO (gitignored)** |
| `docker-compose.prod.yml` | Production stack | Yes |
| `deploy/systemd/arbiter.service` | Bare-metal systemd unit | Yes |
| `deploy/README.md` | Deployment operator runbook | Yes |
| `scripts/setup/*` | Validators + orchestrator | Yes |
| `evidence/05/first_live_trade_*/` | Evidence dumps from live-fire runs | **NO (gitignored)** |

---

## 10. Glossary quick reference

| Term | Meaning |
|---|---|
| SAFE-01 | Kill-switch invariant: within 5s of trip, all open orders are cancelled |
| SAFE-03 | One-leg recovery: if one leg fills and the other fails, unwind within timeout |
| SAFE-04 | Rate-limit backoff + operator UI pills (ok/warn/crit) |
| SAFE-05 | Graceful shutdown: SIGTERM cancels open orders before process exit |
| SAFE-06 | Market mapping resolution criteria (identical/similar/divergent/pending) |
| D-17 | ±$0.01 PnL + fee tolerance for reconciliation |
| D-19 | Phase gate: any real-tagged scenario with breach blocks the next phase |
| Phase gate | `*-VALIDATION.md` artifact with `phase_gate_status: PASS/PENDING/FAIL` |
| MARKET_MAP | The dict of Kalshi↔Polymarket pair definitions in settings.py |
| `allow_auto_trade` | Per-mapping flag; AutoExecutor's gate G4 |

---

**End of handoff.** Start with §3 Step 1.
