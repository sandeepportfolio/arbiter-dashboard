# ARBITER -- Phase 5 Live Trading Operator Runbook

## Purpose

This directory is the Phase 5 live-trading harness: the thin go-live layer
that wraps phases 1-4 for real-money arbitrage. The heavy lifting (FOK
adapters, kill-switch, balance monitor, readiness gate, structlog JSONL
evidence) all landed in phases 1-4. Phase 5 adds three things:

1. An **adapter-layer `$10` notional hard-lock** (`PHASE5_MAX_ORDER_USD`)
   enforced on every Kalshi and Polymarket order.
2. An **operator-facing 15-item preflight checklist** runnable as
   `python -m arbiter.live.preflight` -- exits 0 only when every blocking
   check is green.
3. A **post-trade reconciliation helper** (`reconcile_post_trade`) that
   compares platform-reported fees to the fee model and returns
   discrepancies; Plan 05-02 wires the auto-abort that arms the kill-switch
   on any breach > $0.01 (D-17 tolerance, inherited from Phase 4).

> **WARNING:** Setting `DRY_RUN=false` and running the preflight in your
> shell places REAL money at risk. Read this entire document before
> copying `.env.production.template` -> `.env.production`.

## Prerequisites

Hard prerequisites checked by `python -m arbiter.live.preflight` (15 items,
all blocking unless noted):

| # | Check | Source |
|---|-------|--------|
|  1 | Phase 4 D-19 gate PASSED | `.planning/phases/04-sandbox-validation/04-VALIDATION.md` frontmatter `phase_gate_status: PASS` |
|  2 | Phase 4 all 9 scenarios observed | Same file, `total_scenarios_observed >= 9`, `scenarios_missing == 0` |
|  3 | Phase 4 review warnings resolved or advisory | `04-REVIEW.md` free of `status: blocking`/`status: open` |
|  4 | Kalshi production credentials loaded | `KALSHI_API_KEY_ID` set, `KALSHI_PRIVATE_KEY_PATH` not `demo`, key file exists |
|  5 | Polymarket wallet credentials present | `POLY_PRIVATE_KEY`, `POLY_FUNDER` set (on-chain balance is a manual step) |
|  6 | Kalshi account funded | Operator verifies via kalshi.com UI; preflight checks creds presence as proxy |
|  7 | `DATABASE_URL` points at `arbiter_live` | Not `arbiter_sandbox`, not `arbiter_dev` |
|  8 | `PHASE5_MAX_ORDER_USD` set to `<=$10` | Parseable float in `(0, 10]` |
|  9 | `PHASE4_MAX_ORDER_USD` polarity sane | Absent is expected in production; set-and-tighter-than-PHASE5 is an unsafe inversion (blocks) |
| 10 | Telegram alerting configured | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` set; dry-test is manual |
| 11 | Dashboard kill-switch endpoint reachable | `GET /api/kill-switch` returns 200 or 405 |
| 12 | Readiness endpoint reports ready | `GET /api/readiness` returns JSON with `ready_for_live_trading: true` |
| 13 | Polymarket April 2026 migration compatible | `POLYMARKET_MIGRATION_ACK=ACKNOWLEDGED` after operator reads py-clob-client changelog + Polymarket blog |
| 14 | At least one MARKET_MAP entry has `resolution_match_status=identical` | First live trade cannot target a non-identical mapping |
| 15 | Operator acknowledged this runbook | `OPERATOR_RUNBOOK_ACK=ACKNOWLEDGED` in env |

Checks 11 and 12 are non-blocking when the `arbiter.main` process is not
yet running (they report "manual — start arbiter.main and re-run").

## Setup (One-time)

1. `cp .env.production.template .env.production`
2. Fill in your real Kalshi production API key id + path to
   `./keys/kalshi_private.pem`. This key is DIFFERENT from the demo key
   used in Phase 4; do NOT reuse the demo RSA file.
3. Fund your Polymarket wallet with `<=$50` USDC (or Polymarket USD
   post-April-2026 migration — check Pitfall 7 below).
4. Fund your Kalshi account with `<=$100`.
5. Create the `arbiter_live` Postgres database and apply migrations:
   ```bash
   createdb -U arbiter arbiter_live
   DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_live \
     python -m arbiter.sql.migrate
   ```
6. Verify Polymarket April 2026 migration compatibility (Pitfall 7).
   When verified, set `POLYMARKET_MIGRATION_ACK=ACKNOWLEDGED` in
   `.env.production`.
7. Run preflight: `python -m arbiter.live.preflight` — all 15 items must
   pass. If check 11 or 12 reports "unreachable", start `arbiter.main`
   first and re-run.
8. Once all 15 items are green, set `OPERATOR_RUNBOOK_ACK=ACKNOWLEDGED`
   in `.env.production` (check 15) and re-source.

## Go-Live Procedure

Once preflight reports `OVERALL: PASS`:

1. Source the production env:
   ```bash
   set -a; source .env.production; set +a
   ```
2. Start arbiter in live mode:
   ```bash
   python -m arbiter.main --live
   ```
3. In a separate terminal, open the dashboard
   (`http://localhost:8080`). Arm the kill-switch as a precaution while
   you verify the readiness banner shows green; reset it once satisfied
   (respect the `min_cooldown_seconds` window).
4. Watch the opportunity feed. Accept the system will execute
   automatically when an opportunity on a `resolution_match_status=identical`
   mapping clears every gate (readiness + safety + PHASE5 hard-lock).
5. The scanner sizes opportunities using `MAX_POSITION_USD=10` (B-5 — the
   scanner-level belt; without it the scanner sizes at its default $100
   and every trade hits the adapter hard-lock). First trade target is
   `<=$10` total notional across both legs.
6. Verify post-trade:
   - `reconcile_post_trade` ±$0.01 on both fee and PnL
   - Kalshi portfolio UI shows the fill
   - Polymarket UI (or polygonscan with the funder address) shows the
     position / USDC change
   - Dashboard shows both legs as terminal

## Abort Procedure (mid-trade)

Two options, each idempotent and fast:

- Dashboard: click ARM. The kill-switch state machine fans out
  `cancel_all` across both adapters within the 5s timeout budget.
- Programmatic:
  ```bash
  curl -X POST localhost:8080/api/kill-switch \
       -d '{"action":"arm","reason":"manual"}'
  ```

Expected outcome:

1. All open orders on both platforms are cancelled (batched DELETE on
   Kalshi, per-order cancel on Polymarket) within 5s.
2. A Telegram message arrives: `[ARBITER] Kill switch armed`.
3. No new orders are accepted until RESET is called (respecting
   `min_cooldown_seconds`).
4. Dashboard banner turns red and persists until RESET.

## Rollback to Dry-Run

If anything about the first live trade worries you (non-deterministic
fills, unexpected incidents, latency spikes, weird Telegram messages),
stop trading and fall back to dry-run:

1. Arm the kill-switch first (belt-and-suspenders):
   ```bash
   curl -X POST localhost:8080/api/kill-switch \
        -d '{"action":"arm","reason":"rollback"}'
   ```
2. SIGINT the `arbiter.main` process (Ctrl-C in the terminal running it).
   SAFE-05 graceful shutdown fires automatically on the signal.
3. Flip `DRY_RUN` back to true in `.env.production`:
   ```bash
   sed -i 's/^DRY_RUN=false/DRY_RUN=true/' .env.production
   ```
4. Re-source and restart without `--live`:
   ```bash
   set -a; source .env.production; set +a
   python -m arbiter.main
   ```
5. Verify dry-run mode is active:
   ```bash
   curl localhost:8080/api/readiness | jq '.mode'
   # -> "dry-run"
   ```

## Troubleshooting

### 401 on every Kalshi call (Pitfall 1)

Symptom: every authenticated Kalshi request returns 401. Root cause is
almost always a key/URL mismatch.

Fix: verify that `KALSHI_BASE_URL` points at production
(`https://api.elections.kalshi.com/trade-api/v2`), NOT the demo host, AND
that `KALSHI_PRIVATE_KEY_PATH` points at the PRODUCTION RSA key (not
`./keys/kalshi_demo_private.pem`). Do a read-only
`GET /portfolio/balance` test first to confirm auth is working before
placing any order.

### Polymarket order rejected "unsupported collateral" (Pitfall 7)

Polymarket went through a USDC.e -> Polymarket USD collateral migration
in April 2026. Our pinned `py-clob-client` version must still support
the collateral type in your wallet. If you see "unsupported collateral"
errors:

1. Stop trading immediately (arm the kill-switch).
2. Check the Polymarket developer blog for the current collateral state.
3. Check py-clob-client's changelog for the version that supports the
   current collateral.
4. Do NOT bump py-clob-client mid-Phase-5 without re-running the Phase 4
   sandbox suite against the new version. OPS-04 tracks this bump.

### Reconciliation breach > ±$0.01 (Pitfall 2)

Symptom: `reconcile_post_trade` returns a non-empty list and Plan 05-02's
auto-abort arms the kill-switch.

Root cause is often a fee-rate mismatch: Polymarket's fee rates are
market-category-specific, and our fallback rates (in
`arbiter/config/settings.py::polymarket_order_fee`) may not match the
market you traded. Before every live trade you care about, pre-fetch the
market's actual fee rate via `client.get_market(condition_id)` and
compare to the fallback category rate you would have used.

If the fee model and platform agree but PnL still breaches, there may
be a math bug in the engine. Do NOT restart live until a post-mortem is
written and the fix is tested in sandbox.

### `PHASE5_MAX_ORDER_USD` hard-lock rejected

This is an INTENTIONAL cap. Verify that your scanner sizing satisfies
`qty * price <= PHASE5_MAX_ORDER_USD` (default $10). If the scanner is
sizing above $10 on every opportunity, you forgot to set
`MAX_POSITION_USD=10` in `.env.production` (B-5: the scanner-level belt
above the adapter cap — without it the scanner defaults to $100 and
every trade trips the adapter).

### Readiness gate says "collecting_evidence" forever

Phase 5 bootstrap-mode resolves this chicken-and-egg. Set
`PHASE5_BOOTSTRAP_TRADES=1` (or up to 5) in `.env.production` to bypass
the profitability gate for the first N trades. Once N real trades
complete, the gate re-engages normally. See B-1 Q6 in 05-RESEARCH.md.

## Success Criteria (from `TEST-05`)

Phase 5 Plan 05-02 TEST-05 passes when the first live cross-platform
arbitrage trade either:

- **CLEAN-PASS:** both legs fill, PnL reconciles within ±$0.01 on BOTH
  platforms, fee reconciles within ±$0.01 on BOTH platforms, positive
  realized P&L observed; OR
- **AUTO-ABORT-FIRED:** reconcile breach is caught by
  `wire_auto_abort_on_reconcile`; kill-switch arms within <5s; Telegram
  alert received; all open orders cancelled.

A reconcile breach without the auto-abort path firing FAILS the gate.

See `.planning/phases/05-live-trading/05-VALIDATION.md` for the
D-19-analog validation contract and operator attestation template.

## Files in This Directory

| File | Purpose |
|------|---------|
| `conftest.py` | Shared fixtures: `evidence_dir`, `--live` opt-in gate, fixture plugin loader |
| `fixtures/production_db.py` | asyncpg pool asserting `arbiter_live` DB (not sandbox, not dev) |
| `fixtures/kalshi_production.py` | `KalshiAdapter` fixture refusing demo URL or demo key path |
| `fixtures/polymarket_production.py` | `PolymarketAdapter` fixture refusing construction without `PHASE5_MAX_ORDER_USD <= 10` |
| `evidence.py` | Re-exports `dump_execution_tables`, `write_balances` from sandbox |
| `reconcile.py` | Re-exports tolerance helpers + new `reconcile_post_trade` async helper |
| `preflight.py` | 15-item preflight checklist runner (CLI entry: `python -m arbiter.live.preflight`) |
| `test_reconcile.py` | 4 non-live unit tests for `reconcile_post_trade` |
| `test_preflight.py` | 34 non-live unit tests + 3 integration tests for `run_preflight` |
| `README.md` | This file |

## Related Documents

- `.planning/phases/05-live-trading/05-RESEARCH.md` — full Phase 5 research
  (pitfalls, threat model, position sizing rationale)
- `.planning/phases/05-live-trading/05-VALIDATION.md` — D-19-analog
  validation strategy + operator attestation template
- `.planning/phases/05-live-trading/05-01-PLAN.md` — Plan 05-01 build spec
  (this file + adapter hard-lock implementation)
- `.planning/phases/04-sandbox-validation/04-VALIDATION.md` — Phase 4 gate
  that must PASS before Phase 5 live-fire can run
- `arbiter/sandbox/README.md` — Phase 4 sandbox runbook (mirror of this doc)
