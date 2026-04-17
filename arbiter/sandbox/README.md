# ARBITER -- Phase 4 Sandbox Validation Harness

## Purpose

This harness validates the full arbitrage pipeline against **Kalshi demo**
(`demo-api.kalshi.co`) plus **Polymarket production** (real USDC, $1--5 orders)
before Phase 5 live trading. No real money is at risk on Kalshi; belt-and-suspenders
caps on Polymarket keep exposure bounded even if the adapter misbehaves (test wallet
funded with <= $10 USDC, plus adapter-level `PHASE4_MAX_ORDER_USD=5` hard-lock).

Run this harness once per scenario, inspect the resulting `evidence/04/...` directory,
and archive the curated evidence into `.planning/phases/04-sandbox-validation/evidence/`
for 04-VALIDATION.md citations.

## Prerequisites

- **Docker Desktop** running (for the `arbiter_sandbox` Postgres database)
- **`.env.sandbox`** file created and populated at the repo root (see Credential Setup below)
- **Python venv** active with project requirements installed (`pip install -r requirements.txt`)
- **Separate Kalshi RSA key** at `./keys/kalshi_demo_private.pem` -- do NOT reuse the
  production `kalshi_private.pem` key (demo accounts use a different key pair)
- **Throwaway Polygon wallet** funded with a small amount of USDC (target: ~$10)

## Credential Setup

### Kalshi demo

1. Sign up at <https://demo.kalshi.co> for a dedicated demo account (do not reuse
   your production login).
2. Inside the demo Kalshi dashboard, go to **API keys** and generate a new RSA
   key pair. Copy the API Key ID -- you will need it in `.env.sandbox` as
   `KALSHI_API_KEY_ID`.
3. Save the private key on disk at `./keys/kalshi_demo_private.pem` (note: this
   is a SEPARATE file from `./keys/kalshi_private.pem` which holds your
   production key, per Pitfall 7 -- mismatching key and URL causes 401 on every
   authenticated call).
4. Fund the demo account: Kalshi demo uses manual test-card funding (Pitfall 6).
   Use Visa test card `4000 0566 5566 5556` or Mastercard `5200 8282 8282 8210`
   via the demo site's deposit UI. Fund to ~$100 so every scenario has headroom.
5. Verify funding via Kalshi's demo portfolio page, or wait for the first
   scenario (which exercises authenticated calls and will report errors if
   the key or balance is wrong).

### Polymarket test wallet

Polymarket has **no sandbox**; TEST-02 places real $1--5 USDC orders on the
production CLOB. Belt-and-suspenders:

- **Physical cap:** keep the test wallet funded with <=$10 USDC total. If the
  adapter ever goes rogue it can only lose the wallet contents.
- **Adapter cap:** `PHASE4_MAX_ORDER_USD=5` in `.env.sandbox` triggers an early
  `_failed_order` return inside `polymarket.py::place_fok` when `qty * price > 5`.
  The `poly_test_adapter` fixture refuses to build without this env var set.

Steps:

1. Create a **throwaway EOA wallet** (do NOT use your prod wallet). In MetaMask,
   click the account dropdown -> **Add account** -> **Create account**; then
   export the private key via **Account details** -> **Show private key**.
2. Bridge ~$10 USDC to this wallet on **Polygon** (Polymarket deposit minimum is
   $3 per Polymarket docs). Sources: the Polymarket deposit UI directly,
   Coinbase withdrawal to Polygon, or any Polygon-USDC-capable exchange.
3. Record the wallet's public address as `POLY_FUNDER` in `.env.sandbox`.
4. Verify on <https://polygonscan.com> that the address shows a non-zero USDC
   balance.

**NEVER edit `.env.sandbox` to remove `PHASE4_MAX_ORDER_USD`.** The
`poly_test_adapter` fixture refuses to construct the adapter without it,
making this a second line of defense against an accidental large order.

## Database Setup

With `docker-compose up -d`, the `arbiter_sandbox` database is auto-created
by the init script (Plan 04-02). Verify and apply schema:

```bash
# Verify the sandbox DB exists
docker exec arbiter-postgres psql -U arbiter -c \
  "SELECT datname FROM pg_database WHERE datname = 'arbiter_sandbox';"

# Apply the schema to arbiter_sandbox
DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_sandbox \
  python -m arbiter.sql.migrate
```

## Running Scenarios

```bash
# Source sandbox env into your shell (bash / zsh)
set -a; source .env.sandbox; set +a

# Smoke test (no real API calls; safe to run anywhere)
pytest arbiter/sandbox/test_smoke.py -v

# All live scenarios (requires everything above)
pytest -m live --live arbiter/sandbox/ -v

# Single scenario
pytest -m live --live arbiter/sandbox/test_kalshi_happy_path.py -v
```

The `@pytest.mark.live` marker is an opt-in gate. Without `-m live` or `--live`
every live-marked test is SKIPPED with reason
`"Use -m live or --live to run Phase 4 scenarios"`. This prevents accidental
live-fire from a casual `pytest` invocation.

## Evidence Output

Each scenario writes to `evidence/04/<scenario>_<UTC timestamp>/`:

- `run.log.jsonl` -- structlog output for the test (captured from the `arbiter`
  logger namespace while the test runs; torn down after)
- `execution_orders.json`, `execution_fills.json`,
  `execution_incidents.json`, `execution_arbs.json` -- per-table dumps of the
  `arbiter_sandbox` database after the scenario completes
- `balances_pre.json`, `balances_post.json` -- snapshot of `BalanceMonitor`
  output before and after the scenario (drives TEST-03 reconciliation)

`evidence/04/` is gitignored. Curate the subset of runs you want to preserve
into `.planning/phases/04-sandbox-validation/evidence/` and reference them
in `04-VALIDATION.md`.

## Troubleshooting

- **"401 on every Kalshi call"** -- `KALSHI_BASE_URL` and `KALSHI_PRIVATE_KEY_PATH`
  are mismatched (Pitfall 7). Verify both point at demo: `KALSHI_BASE_URL` should
  contain `demo-api.kalshi.co` and `KALSHI_PRIVATE_KEY_PATH` should point at
  `./keys/kalshi_demo_private.pem`. The `demo_kalshi_adapter` fixture already
  asserts the URL; double-check the key path if the adapter builds but calls 401.

- **"PHASE4_MAX_ORDER_USD hard-lock: notional $X > $5"** -- you selected a
  Polymarket market where `qty * price > 5`. Lower the quantity, pick a market
  with a cheaper price, or check that you are not accidentally quoting in
  basis points instead of decimals. See Pitfall 5: Polymarket's `min_order_size`
  is per-market; pre-flight with `client.get_order_book(token_id)` to read it
  before placing.

- **"SAFETY: DATABASE_URL must point at arbiter_sandbox"** -- `.env.sandbox` is
  not sourced into your shell, or it is pointing at `arbiter_dev`. Fix the
  `DATABASE_URL` line in `.env.sandbox` and re-source.

- **"SAFETY: PHASE4_MAX_ORDER_USD must be set..."** -- you sourced a
  `.env.sandbox` that omits the hard-lock. Re-add `PHASE4_MAX_ORDER_USD=5`
  and re-source. Never run live without this.

- **"INVALID_ORDER_MIN_SIZE" on Polymarket** -- Pitfall 5: the chosen market
  has a `min_order_size > $5`. Pick a lower-minimum market, or quote
  `client.get_order_book(token_id)` first and verify the notional will fit
  under `PHASE4_MAX_ORDER_USD` AND above the market's minimum.

- **"Polymarket wallet not configured"** in adapter output -- `POLY_PRIVATE_KEY`
  is empty or unreadable. Re-check `.env.sandbox`. Never commit this key.

## Acceptance

Phase 4 is complete when `04-VALIDATION.md` shows **PASS** for all 9 scenarios
with cited evidence paths, zero PnL reconciliation breaches beyond +/-$0.01,
and zero fee reconciliation breaches beyond +/-$0.01 (D-19 hard gate).
