# Arbiter — Handoff

**Purpose:** Full current context for whoever (human or AI) picks up next. Read end-to-end once, then work through §3.

**Last update:** 2026-04-21 — after the Polymarket US pivot + scale-to-thousands work landed on `main` (25 commits). Previous handoff's §0 "URGENT — Polymarket US pivot" and §7 "for AI agents" sections are obsolete and were replaced by this doc; the pivot is done and live-fire is unblocked at the code level.

---

## 1. Current state (as of commit `de244b0`)

### Code complete
- **Phases 1–6** plus the new **Polymarket US pivot + scale work**. 495 tests passing (`pytest -q`), 87 skipped (all opt-in via `--live` or `--run-slow`), zero failures. `npx tsc --noEmit` clean.
- **Polymarket integration** now targets `api.polymarket.us/v1` (CFTC-regulated US DCM), auth via Ed25519 header signing. Legacy `clob.polymarket.com` path is preserved behind `POLYMARKET_VARIANT=legacy` for test fixtures / non-US operators.
- **Scanner** rewritten from O(n²) tick loop to event-driven O(1)-per-quote matcher with bounded queue + per-canonical debounce + emit throttle. Scale-tested at 1000 canonical pairs × 3 updates/sec → **0.01 ms p99 match-to-emit latency, zero backpressure drops**.
- **Mapping pipeline** now supports auto-discovery of live markets from both platforms with a 3-layer resolution-equivalence gate (structured fields + LLM verifier + 20+20 hand-labeled fixture corpus) and an 8-condition auto-promote gate.
- **Safety invariants preserved:** kill-switch, `AUTO_EXECUTE_ENABLED=false` default, PHASE4 + PHASE5 hard-locks enforced BEFORE signing (with explicit ordering tests that monkeypatch `_sign_and_send` and assert `call_count == 0` when a gate trips), supervisor-armed gate, SAFE-01..06, D-17 tolerance — all intact.
- **Observability** extended: 9 new Prometheus metrics (matched-pair latency histogram, backpressure drops, auto-promote rejection reason labels, Ed25519 sign failures, WS sub count, etc.). Telegram heartbeat every 15 min while `AUTO_EXECUTE_ENABLED=true`.
- **Operational tooling:** `scripts/setup/check_polymarket_us.py` signed round-trip validator (secret never leaks to stdout/stderr — subprocess-tested); `scripts/setup/onboard_polymarket_us.py` Playwright-driven dev-portal flow to capture an Ed25519 keypair.
- **Rollback plan:** `POLYMARKET_VARIANT=legacy` or `disabled` — no code revert needed, config flip, < 2 min turn-around. Smoke-tested in `arbiter/live/test_rollback_variants.py`.

### What still needs a human

These steps require the operator because they involve identity, real money, or a platform UI gate that does not have a public automation path today:

1. **Polymarket US API keys** — generate at `polymarket.us/developer` (operator completed iOS-app KYC on 2026-04-21). The Playwright script `scripts/setup/onboard_polymarket_us.py` can drive this if the operator logs into the browser it opens; otherwise copy/paste the key ID and base64 secret directly into `.env.production`.
2. **Kalshi production API key + funding** — kalshi.com (NOT `demo-api`), create key, fund via ACH/wire.
3. **Telegram bot** — `@BotFather` + `@userinfobot`, then message the bot once so it can DM you.
4. **Mapping review** — `/ops/mappings` in the dashboard. When `AUTO_PROMOTE_ENABLED=false` (default), candidates surface here for click-confirm. When `AUTO_PROMOTE_ENABLED=true`, all 8 conditions in `arbiter/mapping/auto_promote.py` must pass; promoted pairs still go advisory-only for the first 30 scans (cooling-off) before `allow_auto_trade` flips True at runtime.

Everything else is automated.

---

## 2. Architecture at a glance

```
                    ┌──────────────────┐
Kalshi REST/WS ────▶│                  │    matched pair   ┌──────────────────┐
                    │ MatchedPairStream├──────────────────▶│  AutoExecutor    │
Polymarket US WS ──▶│ (O(1) per quote, │  (bounded queue)  │  (7 policy gates)│
                    │  debounce,       │                   └────────┬─────────┘
                    │  backpressure)   │                            │
                    └────────┬─────────┘                            ▼
                             │                            ┌──────────────────┐
                             ▼                            │ ExecutionEngine  │
                    ┌──────────────────┐                  │  place/fill/     │
                    │ ArbitrageScanner │                  │  cancel          │
                    │ (emits opp via   │◀─────────────────┤  PHASE4+PHASE5   │
                    │  subscribers)    │                  │  hard-locks      │
                    └────────┬─────────┘                  └─────────┬────────┘
                             │                                      │
                             ▼                                      ▼
                    ┌──────────────────┐               ┌──────────────────────┐
                    │ SafetySupervisor │◀──────────────┤ PolymarketUSAdapter  │
                    │ (kill-switch,    │  is_armed     │  + Ed25519 signer    │
                    │  one-leg recover,│  gate         │  + REST + WS         │
                    │  rate-limit)     │               │                      │
                    └────────┬─────────┘               │ KalshiAdapter        │
                             │                        │  (unchanged)         │
                             ▼                        └──────────────────────┘
                    ┌──────────────────┐
                    │ TelegramNotifier │──▶ operator phone
                    │ (retry + dedup   │    15-min heartbeat when auto-exec=on
                    │  + heartbeat)    │
                    └──────────────────┘
```

### The 7 AutoExecutor policy gates (in order)

1. `AUTO_EXECUTE_ENABLED=false` (global kill; default OFF)
2. `supervisor.is_armed` (kill-switch held)
3. `opportunity.requires_manual` (SAFE-06 operator review required)
4. `mapping.allow_auto_trade` (per-pair allow-list; default False per mapping)
5. Duplicate in 5s window (scanner re-emit dedup)
6. Notional > `MAX_POSITION_USD` (position cap)
7. Executed >= `PHASE5_BOOTSTRAP_TRADES` (rollout cap)

### The 8 auto-promote gate conditions

New pipeline in `arbiter/mapping/auto_promote.py` — each condition fails fast and emits the reason to `auto_promote_rejections_total{reason=...}`:

1. `AUTO_PROMOTE_ENABLED=true`
2. `score >= 0.85`
3. `resolution_check() == IDENTICAL`
4. `llm_verifier() == YES`
5. Liquidity ≥ `PHASE5_MAX_ORDER_USD × 2` on both sides (arithmetic test on depth)
6. `resolution_date` within 90 days
7. Daily auto-promote count < `AUTO_PROMOTE_DAILY_CAP` (default 20)
8. Advisory-only cooling-off (first 30 scans after promotion)

### The 3-layer SAFE-06 resolution-equivalence gate

- **Layer 1:** `arbiter/mapping/resolution_check.py` — structured-field equivalence (date within 24h, source allow-list, tie-break, category, outcome_set). Verified against `arbiter/mapping/fixtures/known_{equivalent,divergent}_pairs.json` (22 equivalent + 21 divergent hand-labeled examples).
- **Layer 2:** `arbiter/mapping/llm_verifier.py` — Claude Haiku 4.5 with prompt caching. Fail-safe: any exception → MAYBE, and "not YES" rejects promotion. In-memory LRU cache by pair keyset.
- **Layer 3:** the fixture corpus itself is a CI regression guard — adding a new known-divergent case breaks the merge until the structured-field check is extended.

---

## 3. Handoff checklist

### Step 1 — Sanity check
```bash
git checkout main
git pull origin main
cat STATUS.md | head -30       # current test counts + commit SHAs
cat .planning/STATE.md         # last session's position
```

Expected: on `main`, up to date, `STATUS.md` shows 495 pass / 87 skip / 0 fail.

### Step 2 — Provision credentials (human-only, ~1.5h)

| Step | Portal | Output |
|---|---|---|
| 2A | kalshi.com | `KALSHI_API_KEY_ID` + `./keys/kalshi_private.pem` + ≥$100 balance |
| 2B | polymarket.us/developer (after iOS-app KYC) | `POLYMARKET_US_API_KEY_ID` + `POLYMARKET_US_API_SECRET` (base64 Ed25519) + ≥$20 balance |
| 2C | @BotFather + @userinfobot on Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| 2D | local | `UI_SESSION_SECRET=$(openssl rand -hex 32)` |

Options for Step 2B:
- Run `python scripts/setup/onboard_polymarket_us.py` — opens headful Chromium, you log in, it captures the secret via Playwright's `input_value()` (never via screenshot), writes both env vars, closes the page. The script never echoes the secret.
- Or paste directly into `.env.production`.

Secrets hygiene (enforced by tests, documented here for humans too):
- `.env.production` and `keys/*.pem` are gitignored — keep it that way.
- `chmod 600 .env.production` after editing.
- The setup checks (`check_*.py`) never print secret values; if you see one in your terminal output, something regressed.

### Step 3 — Fill `.env.production`

```bash
cp .env.production.template .env.production
# Edit each <placeholder>. The template has the new Polymarket US section
# (POLYMARKET_VARIANT=us is the default) plus a commented-out legacy section.
chmod 600 .env.production
```

After editing, confirm no `<` characters remain — that's how we detect unfilled placeholders.

### Step 4 — One-shot orchestrator

```bash
./scripts/setup/go_live.sh
```

In order, stopping on first failure:
1. `validate_env.py` — shape/sanity of `.env.production`
2. `docker compose -f docker-compose.prod.yml up -d` — stack up
3. `check_kalshi_auth.py` — signed round-trip + balance
4. `check_polymarket_us.py` (or `check_polymarket.py` if `POLYMARKET_VARIANT=legacy`) — signed round-trip + balance
5. `check_telegram.py` — bot dry-test
6. `check_mapping_ready.py` — verifies ≥1 mapping has `allow_auto_trade=true`
7. `python -m arbiter.live.preflight` (with `PREFLIGHT_ALLOW_LIVE=1`) — the 16-item preflight; checks 5a (credentials-only, CI-safe) and 5b (live `GET /v1/account/balances`, only runs with the allow-live flag)

Expected: all pass, "ALL CHECKS PASSED" banner.

Common failures + fixes:
- `check_polymarket_us.py` HTTP 401 → key ID and base64 secret don't match (regenerate at the dev portal).
- `check_polymarket_us.py` balance < $20 → deposit more through the Polymarket US iOS app or web portal.
- `check_telegram.py` "Telegram disabled" → bot token/chat id wrong, or you haven't DMed the bot yet (Telegram bots can't DM you first).
- `check_mapping_ready.py` "no mapping ready" → open `http://localhost:8080/ops` → Mappings → pick a pair → Confirm → Enable auto-trade.

### Step 5 — First supervised live trade

```bash
docker compose -f docker-compose.prod.yml exec arbiter-api-prod \
    pytest -m live --live arbiter/live/test_first_live_trade.py -v -s
```

Sequence:
1. Preflight runs (should pass).
2. Opportunity detected. Pre-trade requote written to `evidence/05/first_live_trade_<ts>/pre_trade_requote.json`.
3. 60-second abort window. Arm Kill Switch in `/ops` to skip execution.
4. If you don't abort, both legs fire FOK (Kalshi + Polymarket US), capped at $10 per leg.
5. 60-second Polymarket settlement wait.
6. `reconcile_post_trade` checks fees and PnL within ±$0.01.
7. Reconcile breach → `wire_auto_abort_on_reconcile` trips kill-switch and pages operator.

**Pass = either** reconcile within tolerance OR auto-abort fired correctly on breach. Both prove the safety path.

Kill-switch is always in reach at `http://localhost:8080/ops`. Keep it open in a browser tab during this step.

### Step 6 — Flip to auto-mode

```bash
# .env.production:
AUTO_EXECUTE_ENABLED=true

docker compose -f docker-compose.prod.yml restart arbiter-api-prod
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod | grep auto_executor
```

Watch for the first hour:
- Dashboard `/ops` stays green
- `curl http://localhost:8080/api/metrics | grep auto_executor`
- Telegram heartbeat lands every 15 min with `realized_pnl` + `open_order_count`
- Kalshi + Polymarket US balances match dashboard `realized_pnl`

`PHASE5_BOOTSTRAP_TRADES=5` caps to the first 5 auto-trades. After that clears, unset the env var to lift the cap.

### Step 7 — Scale up

After Step 6 has been clean for several hours:

```bash
# .env.production:
PHASE5_BOOTSTRAP_TRADES=  # unset
AUTO_PROMOTE_ENABLED=true  # opt-in to auto-promote candidate mappings
AUTO_PROMOTE_DAILY_CAP=20
AUTO_PROMOTE_ADVISORY_SCANS=30
```

Auto-discovery polls both platforms at 2 rps (doesn't starve quoting/trading). Candidates that pass all 8 conditions flip to `allow_auto_trade=True` after their advisory-only scan window. `MAX_POSITION_USD=$10` per leg still caps total exposure regardless of the number of active pairs.

### Rollback

Any time, 2-minute revert to legacy CLOB or Kalshi-only:
```bash
# .env.production:
POLYMARKET_VARIANT=legacy    # back to clob.polymarket.com
# or
POLYMARKET_VARIANT=disabled  # Kalshi-only, no Polymarket trading

docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

No code revert needed. `arbiter/live/test_rollback_variants.py` pins this behavior.

---

## 4. Observability

| URL | Purpose |
|---|---|
| `http://localhost:8080/ops` | Operator dashboard + kill-switch |
| `http://localhost:8080/api/health` | `{"status":"ok"}` when alive |
| `http://localhost:8080/api/readiness` | go/no-go + `blocking_reasons[]` |
| `http://localhost:8080/api/metrics` | Prometheus text — scrape config in `deploy/README.md` |
| `http://localhost:8080/api/safety/status` | Kill-switch + cooldown |
| `http://localhost:8080/api/safety/events` | Recent safety events (kill_armed, one_leg, etc.) |
| `http://localhost:8080/api/market-mappings/{canonical_id}/audit` | Per-mapping audit log |

New metrics (post-pivot):

- `polymarket_us_rest_latency_p99_ms` (gauge)
- `polymarket_us_ws_reconnects_total` (counter)
- `matched_pair_stream_events_total` (counter)
- `matcher_backpressure_drops_total` (counter)
- `matched_pair_latency_seconds` (histogram)
- `auto_discovery_candidates_pending` (gauge)
- `auto_promote_rejections_total{reason}` (counter — 8 reason labels)
- `ed25519_sign_failures_total` (counter)
- `ws_subscription_count{platform}` (gauge)

---

## 5. Key decisions + why

- **Default `AUTO_EXECUTE_ENABLED=false`.** Flipping it requires explicit action; a freshly cloned repo never auto-trades. Design invariant.
- **`MAX_POSITION_USD=$10`.** Per-leg cap. Small enough that a bug costs $10. Raise only after weeks of clean auto-mode.
- **`PHASE5_BOOTSTRAP_TRADES=5`.** First N auto-trades get extra logging, then the cap kicks in. Lift after manual inspection.
- **Both adapter-layer hard-locks (`PHASE4_MAX_ORDER_USD` + `PHASE5_MAX_ORDER_USD`) enforced in sequence.** Defense in depth. Tests explicitly verify that a trip at either level happens BEFORE `_sign_and_send` is called (monkeypatched assertion on `call_count == 0`).
- **One-leg recovery automatic** (SAFE-03). If one leg fills and the other errors, the supervisor unwinds the filled leg via the opposite-platform counter-order within the SAFE-03 timeout, or pages the operator with manual-unwind instructions.
- **`POLYMARKET_VARIANT` is a runtime flag, not a build flag.** A single container image runs against either variant; rollback is a config + restart.
- **Ed25519 signing payload excludes body.** `{timestamp_ms}{METHOD}{path}`. This is the Polymarket US spec — body is NOT signed. A regression test in `arbiter/auth/test_ed25519_signer.py` pins this by signing the same `{ts, method, path}` with two different bodies and asserting identical signatures.
- **Polymarket US fee curve is quadratic and asymmetric:** `θ × C × p × (1−p)` with `θ_taker = 0.05` and `θ_maker = −0.0125` (rebate, signed negative). `ArbitrageOpportunity.total_fees` handles this correctly.
- **LLM verifier fail-safes to MAYBE.** Anthropic SDK exception → MAYBE → promotion rejected. A flaky network never accidentally flips `allow_auto_trade`.

---

## 6. When to stop and escalate

Any of these means pause and ask:

- Validator in `go_live.sh` fails with a message not in GOLIVE.md §11 troubleshooting or this doc.
- `check_kalshi_auth.py` shows balance < $10 (Kalshi min varies; $100 recommended).
- `check_polymarket_us.py` shows balance < $20 or HTTP 401.
- Reconcile breach after Step 5 that did NOT trigger auto-abort.
- Dollar discrepancy > $1 between dashboard `realized_pnl` and actual exchange balances.
- Kill-switch trips during auto-mode for a reason the metrics don't explain.
- `matcher_backpressure_drops_total` rising — scanner is losing quotes under load.
- `auto_promote_rejections_total{reason="llm_no"}` spiking — LLM layer rejecting pairs that Layer 1 passed (signal of drift between structured-fields check and semantic meaning).

In any of these: ARM the kill-switch, `docker compose down`, dump the evidence dir (`evidence/05/` or `evidence/06/`), write a 5-line summary.

---

## 7. File map

| Path | Purpose | Committed? |
|---|---|---|
| `GOLIVE.md` | Full 13-section operator runbook | yes |
| `HANDOFF.md` | This file | yes |
| `STATUS.md` | Last-known test + commit status | yes |
| `docs/superpowers/specs/2026-04-21-polymarket-us-pivot-and-scale-design.md` | Design spec for the pivot | yes |
| `docs/superpowers/plans/2026-04-21-polymarket-us-pivot-and-scale.md` | 21-task implementation plan | yes |
| `.env.production.template` | Template with US (default) + legacy (commented) sections | yes |
| `.env.production` | Real credentials | **no (gitignored)** |
| `keys/kalshi_private.pem` | Kalshi RSA private key | **no (gitignored)** |
| `docker-compose.prod.yml` | Production stack | yes |
| `deploy/systemd/arbiter.service` | Bare-metal systemd unit | yes |
| `deploy/README.md` | Deployment operator runbook | yes |
| `scripts/setup/go_live.sh` | Orchestrator | yes |
| `scripts/setup/check_polymarket_us.py` | US signed round-trip validator | yes |
| `scripts/setup/check_polymarket.py` | Legacy CLOB validator (still used when `POLYMARKET_VARIANT=legacy`) | yes |
| `scripts/setup/onboard_polymarket_us.py` | Playwright onboarding script | yes |
| `arbiter/auth/ed25519_signer.py` | Ed25519 signer (reusable) | yes |
| `arbiter/collectors/polymarket_us.py` | US REST client | yes |
| `arbiter/collectors/polymarket_us_ws.py` | US WebSocket multiplex | yes |
| `arbiter/collectors/polymarket.py` | Legacy CLOB collector (preserved) | yes |
| `arbiter/execution/adapters/polymarket_us.py` | US execution adapter | yes |
| `arbiter/execution/adapters/polymarket.py` | Legacy execution adapter (preserved) | yes |
| `arbiter/execution/adapters/exceptions.py` | Shared `OrderRejected` | yes |
| `arbiter/scanner/matched_pair_stream.py` | Event-driven matcher | yes |
| `arbiter/mapping/resolution_check.py` | 3-layer gate Layer 1 | yes |
| `arbiter/mapping/llm_verifier.py` | 3-layer gate Layer 2 | yes |
| `arbiter/mapping/fixtures/*.json` | 3-layer gate Layer 3 (fixture corpus) | yes |
| `arbiter/mapping/auto_discovery.py` | Auto-discovery pipeline | yes |
| `arbiter/mapping/auto_promote.py` | 8-condition promote gate | yes |
| `arbiter/notifiers/heartbeat.py` | 15-min Telegram heartbeat | yes |
| `evidence/05/first_live_trade_*/` | Live-fire evidence dumps | **no (gitignored)** |

---

## 8. Glossary

| Term | Meaning |
|---|---|
| SAFE-01 | Kill-switch invariant: within 5s of trip, all open orders cancelled |
| SAFE-03 | One-leg recovery: if one leg fills and other fails, unwind within timeout |
| SAFE-04 | Rate-limit backoff + operator UI pills (ok/warn/crit) |
| SAFE-05 | Graceful shutdown: SIGTERM cancels open orders before process exit |
| SAFE-06 | Market mapping resolution criteria (identical/similar/divergent/pending) |
| D-17 | ±$0.01 PnL + fee tolerance for reconciliation |
| D-19 | Phase gate: any real-tagged scenario with breach blocks the next phase |
| MARKET_MAP | Canonical pair registry; today hand-seeded + Postgres-backed, scaling to auto-discovered thousands |
| `allow_auto_trade` | Per-mapping flag; AutoExecutor's gate G4 |
| `POLYMARKET_VARIANT` | Runtime selector: `us` (default) / `legacy` / `disabled` |

---

## 9. Useful commands

```bash
# Full test suite
pytest -q

# Including slow scale test (30s)
pytest --run-slow -q

# Only the live-fire scenarios (opt-in; real API calls)
pytest -m live --live

# Preflight dry-run (safe, no network)
POLYMARKET_VARIANT=disabled PREFLIGHT_ALLOW_LIVE=0 python -m arbiter.live.preflight

# Preflight live (requires .env.production + PREFLIGHT_ALLOW_LIVE=1)
PREFLIGHT_ALLOW_LIVE=1 python -m arbiter.live.preflight

# TypeScript type-check
npx tsc --noEmit

# Format / lint (if configured)
make lint         # if Makefile target exists; otherwise skip

# One-shot orchestrator (runs all the check_*.py + preflight)
./scripts/setup/go_live.sh

# Playwright onboarding for Polymarket US keys
python scripts/setup/onboard_polymarket_us.py
```

---

**End of handoff.** Start with §3 Step 1.
