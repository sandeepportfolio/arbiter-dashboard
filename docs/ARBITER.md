# ARBITER Handover And Live-Status Context

> **Historical snapshot from 2026-04-15.** This document predates PredictIt removal and the completed Polymarket US pivot. Do not use it for current operator decisions. Use `HANDOFF.md`, `STATUS.md`, and `GOLIVE.md` instead.

Last updated: 2026-04-15

## What This Pass Changed

- Fixed runtime `.env` discovery so ARBITER loads credentials from the repository root instead of looking only inside the Python package tree.
- Fixed relative Kalshi key-path resolution so `./keys/kalshi_private.pem` resolves from the repo root.
- Hardened the Kalshi collector so it can parse live `*_dollars` fields from the current API payload.
- Hardened the Kalshi collector so it does not blindly overwrite one canonical market with every submarket returned under a shared event ticker.
- Added regression tests for config loading and Kalshi live-payload parsing.

## Verified Working

### Full verification suite

- `npm run verify:full` passes.
- Python test suite: `87 passed`
- Vitest suite: `16 passed`
- UI smoke: passed
- Static cross-origin smoke: passed

### Browser/UI behavior

- Public desk loads in browser and mobile smoke tests.
- Operator desk login works in browser smoke tests.
- Manual workflow actions work in browser smoke tests.
- Incident resolution works in browser smoke tests.
- Activity explorer filters/search work in browser smoke tests.

### Runtime / collectors

- `python3 -m arbiter.main --host 127.0.0.1 --port 8096` boots cleanly in dry-run mode.
- Kalshi RSA private key loads successfully at runtime.
- Kalshi authenticated balance read works.
  - Verified balance endpoint returned `$100.00`.
- Kalshi authenticated order-list read works.
  - `GET /trade-api/v2/portfolio/orders` returned `200` with an empty order list.
- PredictIt public market ingestion works.
  - The public endpoint returned `257` markets during validation.
  - ARBITER extracted `8` tracked PredictIt price points from the current seed mappings.
- Readiness and health endpoints respond correctly in live runtime.

### Live runtime truth observed from this machine

- `/api/health` returned `ok`
- `/api/readiness` reported:
  - Kalshi authenticated: `true`
  - Polymarket private key present: `false`
  - PredictIt manual-only: `true`
- `/api/system` reported:
  - active opportunities: `0`
  - total executions: `0`
  - manual positions: `0`
  - profitability verdict: `collecting_evidence`

## What Does Not Work Yet

### Real live trading is still blocked

`python3 -m arbiter.main --live --api-only --host 127.0.0.1 --port 8095` still exits with startup blockers:

- `No confirmed auto-trade mappings are enabled`
- `Polymarket private key is not configured`

Once the env-loading bug was fixed, `Kalshi API credentials are not configured` stopped being a blocker on this machine.

### No real live trades were placed in this pass

I did **not** place a live buy or sell order because that would be a real-money side effect on the linked trading account. I validated safe read-only authenticated Kalshi endpoints instead:

- balance read: working
- order-list read: working

The code path for live Kalshi order submission still exists in:

- `arbiter/execution/engine.py`

But it was **not** executed against the live account in this pass.

### PredictIt is not automatable via a supported trade API here

Current product state:

- PredictIt public market data ingestion works.
- PredictIt balance API does not exist in this codebase.
- PredictIt buy/sell execution is still manual workflow only.
- Manual positions, reminders, unwind instructions, and close tracking exist.

Relevant files:

- `arbiter/collectors/predictit.py`
- `arbiter/workflow/predictit_workflow.py`
- `arbiter/execution/engine.py`

### Current Kalshi mappings do not produce a usable live opportunity

Important nuance:

- Kalshi account auth works.
- Kalshi collector connectivity works.
- But the current seed mapping that points to Kalshi is still not a trustworthy tradable mapping.

Observed behavior:

- The only current Kalshi-linked seed is `DEM_HOUSE_2026 -> KXPRESPARTY-2028`
- That Kalshi event is a 2028 presidency party event, not a 2026 House market.
- ARBITER now safely skips this ambiguous low-confidence submarket match instead of ingesting the wrong live market.
- Result: `fetch_markets()` returns `0` tracked Kalshi prices for current seed mappings, which is safer than publishing incorrect data.

### Current Polymarket configuration is incomplete

- `POLY_PRIVATE_KEY` is empty on this machine.
- Live Polymarket trade submission is therefore not possible.

Additional observation from public Gamma lookups performed during this pass:

- the current seed slugs tested for the mapped 2026/2028 political markets returned empty arrays from the Gamma `/markets` lookup used in the probe

This means Polymarket mapping/slug validation needs a fresh pass before claiming live production readiness.

### Profitability is still not validated

Observed live runtime state:

- profitability verdict: `collecting_evidence`
- completed executions: `0`
- published opportunities: `0`

The system therefore cannot honestly claim profitable live execution yet.

### Telegram is still not configured

- `TELEGRAM_BOT_TOKEN` missing
- `TELEGRAM_CHAT_ID` missing

This blocks the alerting/readiness path from being fully production-ready.

### Operator mapping actions are not durable across restart

Current code observation:

- operator mapping actions in `arbiter/api.py` call `update_market_mapping(...)`
- `update_market_mapping(...)` mutates the in-memory `MARKET_MAP`
- there is no persistence path wired into startup for those edits in the current runtime

Implication:

- confirming a mapping or enabling auto-trade from the dashboard is not durable across restart unless durable mapping storage is wired in

## Highest-Priority Remaining Work

1. Replace placeholder / stale market mappings with real current cross-venue mappings.
2. Add a durable mapping store to the runtime path so operator approvals survive restart.
3. Re-validate Polymarket slugs against current live markets and add the missing wallet key.
4. Configure Telegram alerting.
5. Prove at least one safe, verified end-to-end live trade path with micro size.
6. Accumulate enough completed executions to move profitability beyond `collecting_evidence`.

## Recommended Next Validation Sequence

1. Verify current market identity across venues before enabling any auto-trade mapping.
2. Add one confirmed Kalshi <-> Polymarket or Kalshi <-> manual PredictIt mapping that matches live, liquid markets now.
3. Set `POLY_PRIVATE_KEY`.
4. Set Telegram credentials.
5. Start ARBITER in dry-run and confirm real opportunities appear from live collectors.
6. Start ARBITER in live mode only after readiness turns green for the exact venues involved.
7. Place one micro-size live trade only after verifying the exact market ids and hedge path.
8. Confirm the execution appears in:
   - `/api/trades`
   - `/api/reconciliation`
   - `/api/system`
   - dashboard trade history / activity feed

## Commands Used In This Pass

### Full verification

```bash
npm run verify:full
```

### Real dry-run runtime

```bash
python3 -m arbiter.main --host 127.0.0.1 --port 8096
```

### Live startup preflight

```bash
python3 -m arbiter.main --live --api-only --host 127.0.0.1 --port 8095
```

### Safe authenticated Kalshi probes

```bash
# balance
GET /trade-api/v2/portfolio/balance

# order list
GET /trade-api/v2/portfolio/orders
```

## Run From Another Machine

After pulling the latest `main`:

```bash
cd /path/to/arbiter
python3 -m arbiter.main --host 0.0.0.0 --port 8090
```

Open:

- `http://HOST:8090/`
- `http://HOST:8090/ops`

If you want the static shell:

```bash
python3 -m http.server 8092
```

Then open:

- `http://HOST:8092/?api=http://HOST:8090`

## Honest Bottom Line

This repo is now in a better state to continue from another machine:

- tests are green
- the env/credential loading bug is fixed
- Kalshi auth works for read-only account endpoints
- PredictIt public ingestion works
- the browser dashboard flows pass

But it is **not yet a proven live profitable trading system**. The remaining blockers are not cosmetic:

- no durable confirmed auto-trade mappings
- no Polymarket wallet configured
- no Telegram configuration
- no real completed live executions
- no validated profitability
- no supported automated PredictIt trade API path
