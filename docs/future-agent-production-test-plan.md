# ARBITER Future Agent Prompt

Generated: 2026-04-15  
Workspace: `/Users/rentamac/Documents/arbiter`  
Pinned commit: `8c0003748dee80fb1b14e72871dc3b50ec9154af`

## Mission

You are continuing ARBITER, a cross-platform prediction-market arbitrage system.

The product goal is not just "green tests." The product goal is:

1. keep the software healthy and production-test ready,
2. prove the route inventory is actually safe and profitable under real conditions,
3. advance only when the evidence supports a live micro-trade,
4. stop immediately when the evidence says the system is not ready.

Do not optimize for superficial completion. Optimize for truthful progress toward profitable live trading.

## Project Context

ARBITER is an operator-first arbitrage system across:

- Kalshi
- Polymarket
- PredictIt

Important product surfaces:

- `/` is the public read-only trading desk
- `/ops` is the authenticated operator desk
- repo-root `index.html` is the static frontend that can talk to a separate backend API base
- `404.html` supports GitHub Pages-style deep linking for ops mode

Core runtime modules:

- [arbiter/main.py](/Users/rentamac/Documents/arbiter/arbiter/main.py)
- [arbiter/api.py](/Users/rentamac/Documents/arbiter/arbiter/api.py)
- [arbiter/scanner/arbitrage.py](/Users/rentamac/Documents/arbiter/arbiter/scanner/arbitrage.py)
- [arbiter/execution/engine.py](/Users/rentamac/Documents/arbiter/arbiter/execution/engine.py)
- [arbiter/profitability/validator.py](/Users/rentamac/Documents/arbiter/arbiter/profitability/validator.py)
- [arbiter/readiness.py](/Users/rentamac/Documents/arbiter/arbiter/readiness.py)
- [arbiter/portfolio/monitor.py](/Users/rentamac/Documents/arbiter/arbiter/portfolio/monitor.py)
- [arbiter/collectors/kalshi.py](/Users/rentamac/Documents/arbiter/arbiter/collectors/kalshi.py)
- [arbiter/collectors/polymarket.py](/Users/rentamac/Documents/arbiter/arbiter/collectors/polymarket.py)
- [arbiter/collectors/predictit.py](/Users/rentamac/Documents/arbiter/arbiter/collectors/predictit.py)

Key browser and verification files:

- [arbiter/web/dashboard.html](/Users/rentamac/Documents/arbiter/arbiter/web/dashboard.html)
- [arbiter/web/dashboard.js](/Users/rentamac/Documents/arbiter/arbiter/web/dashboard.js)
- [scripts/quick-check.sh](/Users/rentamac/Documents/arbiter/scripts/quick-check.sh)
- [scripts/ui-smoke.sh](/Users/rentamac/Documents/arbiter/scripts/ui-smoke.sh)
- [scripts/static-smoke.sh](/Users/rentamac/Documents/arbiter/scripts/static-smoke.sh)
- [package.json](/Users/rentamac/Documents/arbiter/package.json)
- [.github/workflows/ci.yml](/Users/rentamac/Documents/arbiter/.github/workflows/ci.yml)

## Current Truth

What is working right now:

- public desk renders on `/`
- operator desk renders on `/ops`
- same-origin dashboard smoke passes
- static cross-origin dashboard smoke passes
- auth-protected ops actions work in browser smoke:
  - manual queue actions
  - incident resolution
  - mapping actions
- readiness state exists and is exposed by the API:
  - `/api/readiness`
  - `/api/health`
  - `/api/system`
- live execution is now gated by readiness instead of trusting informational state alone
- startup no longer uses the broken nonexistent worker modules from `scripts/start-arbiter.sh`
- PredictIt cached fallback data now preserves original source timestamps instead of pretending stale data is fresh

Latest verified commands and outcomes:

```bash
cd /Users/rentamac/Documents/arbiter
python3 -m pytest -q arbiter
```

Result:

- `77 passed`

```bash
cd /Users/rentamac/Documents/arbiter
npm run verify:full
```

Result:

- Python package tests passed
- TypeScript typecheck passed
- Vitest passed
- API smoke passed
- same-origin browser smoke passed
- static cross-origin browser smoke passed

## What Still Does Not Work Or Is Not Yet Proven

Be honest about these. Do not blur "implemented" with "proven."

### External / Real-Money Gaps

- real Kalshi credentials have not been validated in production conditions
- real Polymarket credentials have not been validated in production conditions
- real Telegram delivery has not been validated end to end
- a 72-hour dry-run soak has not been completed
- one-leg recovery has not been validated under real venue conditions
- no live micro-trade has been completed and reconciled
- profitability is not yet a live-market fact; it is still guarded by software rules and simulated evidence

### Known Product / System Limitations

- PredictIt remains manual-only; it does not have an execution API in this system
- the PnL reconciler exists but is not yet wired into the main runtime control loop or surfaced as a first-class operational gate
- portfolio monitoring is still partial:
  - it relies mainly on in-memory execution history
  - unrealized PnL is still rough
  - durable-ledger integration is not the main source of truth yet
- collector identity matching still deserves suspicion in edge cases:
  - ambiguous Kalshi event fan-out can still mis-map markets
  - Polymarket market matching can still be brittle when question matching is weak
- live order submission is safer than before, but still not proven in real-world fills, cancellations, and latency conditions
- no agent should claim the system is "error free"; the strongest truthful statement right now is that the current automated verification stack is green

### Operational Consequence

If you are in live mode and any of the above remains unresolved, assume ARBITER is still in staged production-test mode, not validated live operation.

## Rules For Future Agents

Follow these rules every time:

1. Do not revert unrelated user changes.
2. Do not weaken readiness or profitability gates just to make live trading easier.
3. Do not call the system profitable without new evidence.
4. Do not treat browser pass/fail as enough. Runtime and market truth matter more.
5. If any gate fails, fix it and restart from the earliest affected gate.
6. If you change the desk UI, update the smoke scripts in the same turn.
7. If you change execution or collector math, update the related Python tests in the same turn.
8. If you make progress, update this plan or a sibling handoff doc with exact new truths.

## Test Plan

Run these gates in order.

### Gate 0: Repo Health

Goal: prove the repo is locally healthy before runtime testing.

Run:

```bash
cd /Users/rentamac/Documents/arbiter
npm run verify:full
```

This currently covers:

- Python tests
- critical Python syntax checks
- dashboard JavaScript syntax check
- TypeScript typecheck
- Vitest
- API smoke
- same-origin browser smoke
- static cross-origin browser smoke

Pass condition:

- every sub-step passes

Failure response:

- fix the failing step
- rerun `npm run verify:full`

### Gate 1: API Contract Verification

Goal: verify the app contract, not just that pages load.

Run:

```bash
cd /Users/rentamac/Documents/arbiter
python3 -m pytest -q arbiter/test_api_integration.py arbiter/test_readiness.py
```

Key endpoints to verify:

- `/api/health`
- `/api/system`
- `/api/readiness`
- `/api/opportunities`
- `/api/trades`
- `/api/errors`
- `/api/manual-positions`
- `/api/market-mappings`
- `/api/profitability`
- `/api/portfolio`
- `/api/portfolio/violations`
- `/api/portfolio/positions`
- `/api/portfolio/summary`
- `/api/auth/login`
- `/api/auth/me`
- `/ws`

Pass condition:

- endpoint shapes match expectations
- no 500s
- readiness payload is populated
- auth still protects mutating ops actions

Failure response:

- fix the API contract
- rerun Gate 0 and Gate 1

### Gate 2: Execution, Readiness, And Freshness Logic

Goal: verify the most safety-sensitive backend logic directly.

Run:

```bash
cd /Users/rentamac/Documents/arbiter
python3 -m pytest -q \
  arbiter/execution/test_engine.py \
  arbiter/profitability/test_validator.py \
  arbiter/collectors/test_predictit_collector.py
```

Focus:

- readiness gate blocks live execution when not ready
- manual position lifecycle updates execution state correctly
- manual close/cancel releases risk exposure
- profitability verdicts behave as expected
- cached PredictIt data keeps original timestamps

Pass condition:

- all targeted safety tests pass

Failure response:

- fix the safety logic
- rerun Gate 0 through Gate 2

### Gate 3: Runtime Smoke In Dry Run

Goal: prove the runtime can start and hold state cleanly outside tests.

Run:

```bash
cd /Users/rentamac/Documents/arbiter
python3 -m arbiter.main --port 8090
```

Check manually or with curl:

```bash
curl -s http://127.0.0.1:8090/api/health | jq
curl -s http://127.0.0.1:8090/api/system | jq
curl -s http://127.0.0.1:8090/api/readiness | jq
```

Confirm:

- process stays up
- collectors publish
- scanner populates opportunities
- profitability publishes snapshots
- readiness publishes blocking reasons instead of silently allowing live behavior

Pass condition:

- no crash
- readiness is coherent
- runtime data is fresh enough to look believable

Failure response:

- fix runtime bug
- rerun Gate 0 through Gate 3

### Gate 4: Credential And Venue Reality Validation

Goal: validate real venue access without jumping to real trading.

Before running this gate:

- have real Kalshi credentials
- have a real Polymarket private key
- have real Telegram bot credentials
- keep exposure at zero

Run live preflight:

```bash
cd /Users/rentamac/Documents/arbiter
DRY_RUN=false python3 -m arbiter.main --live --api-only --port 8090
```

Inspect:

- `/api/readiness`
- `/api/health`
- collector stats
- readiness blocking reasons

Pass condition:

- startup succeeds in live mode
- credentials are accepted
- readiness blocks or allows for truthful reasons
- no fake "ready" state appears due to missing credentials

Failure response:

- do not bypass the gate
- fix config, auth, or readiness logic
- rerun Gate 0 through Gate 4

### Gate 5: Soak And Recovery Validation

Goal: prove the system is operationally stable.

Run dry-run soak:

```bash
cd /Users/rentamac/Documents/arbiter
python3 -m arbiter.main --port 8090
```

Target:

- 72 continuous hours

Track:

- uptime
- collector degradation
- stale quotes
- WebSocket continuity
- readiness state drift
- profitability state drift
- incident creation and resolution
- manual queue behavior
- memory growth
- crash count

Pass condition:

- zero process crashes
- no silent dead state
- readiness and profitability remain coherent

Failure response:

- record timestamp
- record subsystem
- record logs
- record exact reproduction steps
- fix and restart soak from zero

### Gate 6: One-Leg Recovery Validation

Goal: prove the recovery path is real, not theoretical.

You may need a controlled venue scenario or a simulation harness if real reproduction is not yet safe.

Verify:

- a partially filled or failed leg creates an incident
- the system attempts cancellation/recovery
- the operator desk reflects the incident
- unwind or manual follow-up instructions are actionable

Pass condition:

- incident is visible
- recovery path is explicit
- operator can follow the path without guesswork

Failure response:

- fix execution/recovery state handling
- rerun Gate 0 through Gate 6

### Gate 7: First Live Micro-Trade

Goal: reach the first real trade only after every previous gate is green.

Requirements before attempting:

- live readiness says ready
- at least one auto-trade mapping is intentionally enabled
- credentials are confirmed
- balances are above thresholds
- Telegram delivery is real
- soak and recovery work are complete enough to trust

Trade policy:

- smallest possible safe exposure
- one route only
- record everything

Record:

- timestamp
- venue identifiers
- prices
- quantity
- fills
- fees
- realized PnL
- any slippage
- any operator intervention

Pass condition:

- both legs complete safely
- reconciliation matches reality
- no unexplained incident

Failure response:

- disable live trading
- preserve logs
- write a precise incident summary
- do not attempt a second live trade until root cause is fixed

## What To Inspect First If Something Breaks

If public or ops desk breaks:

- [arbiter/web/dashboard.html](/Users/rentamac/Documents/arbiter/arbiter/web/dashboard.html)
- [arbiter/web/dashboard.js](/Users/rentamac/Documents/arbiter/arbiter/web/dashboard.js)
- [scripts/ui-smoke.sh](/Users/rentamac/Documents/arbiter/scripts/ui-smoke.sh)
- [scripts/static-smoke.sh](/Users/rentamac/Documents/arbiter/scripts/static-smoke.sh)

If live gating looks wrong:

- [arbiter/readiness.py](/Users/rentamac/Documents/arbiter/arbiter/readiness.py)
- [arbiter/main.py](/Users/rentamac/Documents/arbiter/arbiter/main.py)
- [arbiter/execution/engine.py](/Users/rentamac/Documents/arbiter/arbiter/execution/engine.py)
- [arbiter/test_readiness.py](/Users/rentamac/Documents/arbiter/arbiter/test_readiness.py)

If pricing/freshness looks suspicious:

- [arbiter/collectors/predictit.py](/Users/rentamac/Documents/arbiter/arbiter/collectors/predictit.py)
- [arbiter/collectors/kalshi.py](/Users/rentamac/Documents/arbiter/arbiter/collectors/kalshi.py)
- [arbiter/collectors/polymarket.py](/Users/rentamac/Documents/arbiter/arbiter/collectors/polymarket.py)
- [arbiter/utils/price_store.py](/Users/rentamac/Documents/arbiter/arbiter/utils/price_store.py)

If profitability or audit looks wrong:

- [arbiter/profitability/validator.py](/Users/rentamac/Documents/arbiter/arbiter/profitability/validator.py)
- [arbiter/audit/math_auditor.py](/Users/rentamac/Documents/arbiter/arbiter/audit/math_auditor.py)
- [arbiter/audit/pnl_reconciler.py](/Users/rentamac/Documents/arbiter/arbiter/audit/pnl_reconciler.py)

## Required Honesty In Final Reports

Future agents must explicitly distinguish between:

- implemented
- tested locally
- smoke-verified in browser
- validated with real credentials
- validated in soak
- validated with real money

Do not use words like "done," "production ready," or "fully working" unless you can defend them against that full ladder.

## Current Bottom Line

ARBITER is in a much stronger production-test state than before:

- verification is broader
- browser coverage matches the current UI
- readiness is a real live gate
- API reporting is stronger

But ARBITER is still not proven as a live profitable trading system.

That remains the next honest frontier.
