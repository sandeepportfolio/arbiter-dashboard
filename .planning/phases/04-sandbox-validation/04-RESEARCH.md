# Phase 4: Sandbox Validation - Research

**Researched:** 2026-04-16
**Domain:** Live-fire validation harness against Kalshi demo + Polymarket production ($1–5), end-to-end pipeline (collect → scan → execute → monitor → reconcile)
**Confidence:** HIGH for Kalshi demo + fill fee fields; HIGH for py-clob-client 0.34.6 surface (inspected locally); MEDIUM for Polymarket trade-record fee shape (SDK wraps raw HTTP response — requires sandbox observation); HIGH for harness infrastructure patterns (pytest, docker-compose multi-DB)

## Summary

Phase 4 must live-fire the full pipeline against real platform APIs with no real capital at Kalshi (demo env) and ≤$5 real capital at Polymarket (no sandbox exists). All major locking decisions are already made in `04-CONTEXT.md`; research fills in the concrete field names, URLs, and mechanics the planner needs to write unambiguous tasks.

Four findings are load-bearing for the plan:

1. **Kalshi fee field is `fee_cost`, not `realized_fee`.** [VERIFIED: docs.kalshi.com changelog + docs.kalshi.com/api-reference/portfolio/get-fills.md] `04-CONTEXT.md` D-18 says "Kalshi `realized_fee`" — this is incorrect as of the Jan 27–29, 2026 API migration. The fills endpoint and the fill WebSocket message carry `fee_cost` as a fixed-point dollars string. The order-create response exposes `taker_fees_dollars` and `maker_fees_dollars` (split, since Oct 2025). Plan must thread `fee_cost` (post-fill) as the canonical compare-against-`kalshi_order_fee()` value.
2. **Kalshi FOK rejection is HTTP 201 with `status: canceled`**, not an HTTP error. [CITED: docs.kalshi.com/api-reference/orders/create-order.md] The FOK-rejection scenario must assert on the response body's `status` field, not the HTTP status code. The existing `KalshiAdapter._FOK_STATUS_MAP` already handles this (`canceled → OrderStatus.CANCELLED`).
3. **Polymarket has no `fee` field in the order response.** [VERIFIED: py_clob_client/client.py source inspection, v0.34.6] The order response shape from `post_order()` is `{success, errorMsg, orderID, takingAmount, makingAmount, status, transactionsHashes, tradeIDs}`. The `fee_rate_bps` field is on the **request** (OrderArgs) and is resolved dynamically by `create_order()` via `__resolve_fee_rate` (GET `/fee-rate`). To compare platform-charged fees against `polymarket_order_fee()`, the plan must (a) capture `fee_rate_bps` from the dynamic rate call, (b) query `client.get_trades(TradeParams(...))` after fill to recover each Trade's `fee_rate_bps` + `size` + `price`, and (c) compute `fee = C × rate × p × (1-p)` and compare. There is no direct `fee` field — it must be reconstructed from the rate.
4. **Polymarket FOK on an illiquid market fails with `FOK_ORDER_NOT_FILLED_ERROR`.** [CITED: docs.polymarket.com/developers/CLOB/orders] The `place_fok` adapter currently treats any non-success response as a generic "order rejected." The rejection-scenario test must match on this specific error string to distinguish real FOK kill from transient/auth/429 failures.

**Primary recommendation:** Build the harness as a new `arbiter/sandbox/` pytest package using the existing `@pytest.mark.live` opt-in pattern; piggyback on the existing project `conftest.py` async runner (no pytest-asyncio markers needed). Create a second `arbiter_sandbox` database inside the existing docker-compose Postgres service via a `/docker-entrypoint-initdb.d/` sidecar SQL file — do **not** add a second Postgres service. Parameterize `KalshiConfig.base_url` and `PolymarketConfig.clob_url` via env vars (`KALSHI_BASE_URL`, `POLYMARKET_CLOB_URL`) — currently both are hardcoded defaults at `settings.py:365, 376`. Reconstruct Polymarket fees from `get_trades()`'s `fee_rate_bps` + `size` + `price` (no direct field). Use Kalshi's `fee_cost` field (NOT `realized_fee`) for fill-level fee verification.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Test harness orchestration | Test (pytest in `arbiter/sandbox/`) | — | Not production code; isolated to sandbox package per D-15 |
| Env-var-driven URL selection | Config (`arbiter/config/settings.py`) | — | Minimal production-code change per D-01 |
| Blast-radius hard-lock ($5 max) | Adapter (`arbiter/execution/adapters/polymarket.py`) | — | D-02 mandates adapter-layer enforcement above SAFE-02 RiskManager |
| Demo DB isolation | Infrastructure (`docker-compose.yml` + initdb.d) | — | D-03 requires separate DB; sql/migrate.py is tier-agnostic |
| Fill-level fee capture | Adapter returns Order; Store persists fill | Audit (`math_auditor.py` cross-check) | Fee field lives in adapter response path |
| PnL reconciliation | Audit (`pnl_reconciler.py` consumes snapshots) | Monitor (`balance.py` fetches snapshots) | Existing split; Phase 4 only wires pre/post capture |
| Fault injection (one-leg, rate-limit burst) | Test fixtures (pytest fixtures + `unittest.mock.patch`) | — | Must not touch production code — D-11 |
| Evidence artifact capture | Test fixtures | DB (dump execution_* tables), structlog output redirect | Sink is test-owned; produces artifacts under `evidence/04/` |
| Kill-switch / shutdown observation | Integration (out-of-process test driver subprocess) | Dashboard WS event capture | D-09 scenarios exercise running process, not fixture-mocked one |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | 8.3.4 | Test runner | Project baseline; 137 tests already run on it [VERIFIED: pip show] |
| pytest-asyncio | 0.25.0 | Async test support | Installed; HOWEVER project uses custom `conftest.py` `pytest_pyfunc_call` hook that calls `asyncio.run()` directly. Follow that pattern, not `@pytest.mark.asyncio`. [VERIFIED: conftest.py source] |
| py-clob-client | 0.34.6 | Polymarket CLOB SDK | Already used in `engine.py`; FOK/FAK/GTC/GTD OrderType enum confirmed in 0.34.6 [VERIFIED: pip show + clob_types.py inspection] |
| asyncpg | 0.31.0 | PostgreSQL async driver | Already in use for ExecutionStore; supports multi-DB via `database` param [VERIFIED: pip show] |
| structlog | 25.5.0 | Structured logs for evidence | Already produces JSON per OPS-01; redirect to per-scenario file [VERIFIED: pip show] |
| aiohttp | 3.13.5 | HTTP for Kalshi demo calls | Already wired through KalshiAdapter.session [VERIFIED: pip show] |
| tenacity | 9.1.4 | Retry policy | Already in `retry_policy.py`; Phase 4 should NOT suppress it [VERIFIED: pip show] |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `unittest.mock` (stdlib) | — | Patch adapter methods for fault injection | D-11 one-leg and rate-limit-burst scenarios |
| `subprocess` (stdlib) | — | Launch real `arbiter.main` for SAFE-05 shutdown scenario | Pattern already used in `test_api_integration.py:40` |
| `socket` (stdlib) | — | Pick free port for subprocess | Same pattern as `test_api_integration.py:free_port()` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `@pytest.mark.live` skipped-by-default | Separate CI job | User's D-12 locks in pytest marker pattern; matches existing `test_api_integration.py` test layout |
| Second Postgres service in compose | Second DB on same service | User's D-03 allows either; single-service+initdb.d is simpler and matches existing compose shape |
| Interactive setup script | `.env.sandbox.template` + README | D-04 locked in template approach |

**Installation:** No new dependencies required. Every library above is already installed.

**Version verification commands run during research:**
```bash
pip show py-clob-client  # 0.34.6
pip show asyncpg         # 0.31.0
pip show structlog       # 25.5.0
pip show pytest          # 8.3.4
pip show pytest-asyncio  # 0.25.0
pip show tenacity        # 9.1.4
```

## User Constraints (from CONTEXT.md)

### Locked Decisions (must honor verbatim)

**Carried forward:**
- D-CF-01..09: Kalshi dollar-string pricing, Polymarket `py-clob-client` + `signature_type`/`funder`, PredictIt read-only, FOK both layers, PostgreSQL persistence with full audit trail, structlog JSON, tenacity+CircuitBreaker, safety layer complete (SAFE-01..06), client-order-id persistence

**Environment & credential isolation:**
- **D-01:** `KALSHI_BASE_URL` env var overrides the hardcoded default at `settings.py:365`. Default stays production.
- **D-02:** Polymarket blast-radius = **both** (a) dedicated test wallet funded with ~$10 USDC hardware cap AND (b) adapter-layer `PHASE4_MAX_ORDER_USD=5` hard-lock. Belt-and-suspenders mandatory.
- **D-03:** Separate Postgres database `arbiter_sandbox`. Same Postgres instance OK; schema via `arbiter/sql/migrate.py`.
- **D-04:** `.env.sandbox.template` + README bootstrap. No interactive script.

**Scenario coverage (all required):**
- **D-05:** Kalshi demo happy-path lifecycle (TEST-01)
- **D-06:** Polymarket real-$1 happy-path lifecycle (TEST-02)
- **D-07:** FOK rejection on both Kalshi demo and Polymarket (thin-liquidity market)
- **D-08:** Execution-timeout + cancel-on-timeout on Kalshi demo (CR-01 live validation)
- **D-09:** Four Phase 3 live safety scenarios: SAFE-01 kill-switch, SAFE-03 one-leg, SAFE-04 rate-limit, SAFE-05 graceful shutdown
- **D-10:** Single-platform only — no cross-platform arb (belongs to Phase 5)
- **D-11:** Mix of real and fault-injected triggers; each scenario tagged `real` or `injected`

**Test harness & artifacts:**
- **D-12:** `@pytest.mark.live` suite, skipped by default, opt-in via `pytest -m live`
- **D-13:** Acceptance artifact = `04-VALIDATION.md` structured like `03-VERIFICATION.md`
- **D-14:** Evidence = structlog JSON + `execution_*` DB dumps + `balances.json`, stored under `evidence/04/<scenario>/`. No HTTP recorder, no VCR.
- **D-15:** Harness lives in new `arbiter/sandbox/` package

**Reconciliation:**
- **D-16:** Pre/post balance snapshots via existing `BalanceMonitor.fetch_balance()` + existing `pnl_reconciler.py`
- **D-17:** PnL tolerance **±1¢ absolute** — hard gate
- **D-18:** Kalshi `realized_fee` and Polymarket CLOB `fee` field vs fee functions in `settings.py` — **research finding below corrects these field names**
- **D-19:** Any scenario exceeding PnL or fee tolerance = Phase 5 blocked

### Claude's Discretion
- Exact pytest fixture internals for sandbox DB bootstrap / teardown
- Thin-liquidity market selection logic for FOK rejection
- Structured incident payload shape for fee-discrepancy logs
- `evidence/04/<scenario>/` schema and naming conventions
- Fault injection mechanics for one-leg and rate-limit-burst
- Aggressive-limit price strategy for timeout-cancel scenario
- docker-compose layout (second DB vs second service)

### Deferred Ideas (OUT OF SCOPE)
- Cross-platform arb execution — Phase 5
- Simulated cross-platform arb on Kalshi demo alone
- Adapter-level HTTP response recorder
- VCR cassette replay
- pytest-html CI gating
- Interactive `scripts/setup_sandbox.py`
- Polling-every-N-seconds balance timeline
- Tiered pass/soft-flag/hard-fail reconciliation

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| TEST-01 | End-to-end pipeline validated on Kalshi demo sandbox with real API calls | Demo base URL `https://demo-api.kalshi.co/trade-api/v2` confirmed; auth is identical RSA-PSS flow with separate-from-prod keys; demo supports FOK + dollar-string pricing per API-wide parity; **funding is manual via test cards** (no auto-faucet) |
| TEST-02 | Polymarket minimum-size ($1–5) real order lifecycle validated | Polymarket `min_order_size` per-market (typically 5 USDC or 5 contracts); minimum USDC deposit is $3 via Polygon bridge; py-clob-client 0.34.6 OrderType.FOK confirmed; two-phase `create_order → post_order` already implemented |
| TEST-03 | Recorded PnL matches platform balance changes (±1¢ absolute) | BalanceMonitor.fetch_balance already exists; Kalshi reports balance in cents (÷100); Polymarket balance via Polygon USDC contract ERC20 `balanceOf` |
| TEST-04 | Platform-reported fees match system's fee calculations | **Kalshi field is `fee_cost` (not `realized_fee`)** in fills endpoint + WS; **Polymarket has no `fee` field — reconstruct from `fee_rate_bps` × `size` × `price` × (1-price)** on each Trade from `get_trades()`; compare against `kalshi_order_fee()` / `polymarket_order_fee()` in `settings.py` |

## Architecture Patterns

### System Architecture Diagram

```
Operator
   │
   ▼
pytest -m live ──► arbiter/sandbox/test_*.py
   │                    │
   │                    ├─── arbiter/sandbox/conftest.py (fixtures)
   │                    │        │
   │                    │        ├── sandbox_db (asyncpg → arbiter_sandbox)
   │                    │        ├── demo_kalshi_client (KALSHI_BASE_URL=demo-api)
   │                    │        ├── poly_test_wallet (throwaway private key, ≤$10 USDC)
   │                    │        ├── balance_snapshot (pre/post)
   │                    │        └── evidence_dir (evidence/04/<scenario>/)
   │                    │
   │                    └─── scenario flow
   │                           │
   │                           ▼
   │                    ExecutionEngine (+ SafetySupervisor + RiskManager)
   │                           │
   │                           ▼
   │                    Adapters (Kalshi → demo, Polymarket → prod + $5 lock)
   │                           │
   │                           ├─► Real API calls
   │                           └─► ExecutionStore → arbiter_sandbox DB
   │
   ▼
04-VALIDATION.md (per-scenario pass/fail + evidence links)
   │
   ▼
evidence/04/<scenario>/
   ├── run.log.jsonl          (structlog JSON)
   ├── execution_orders.json  (DB dump)
   ├── execution_fills.json   (DB dump)
   ├── execution_incidents.json
   ├── balances_pre.json
   └── balances_post.json
```

### Recommended Project Structure
```
arbiter/sandbox/
├── __init__.py
├── conftest.py           # live fixtures + evidence capture
├── fixtures/
│   ├── kalshi_demo.py    # demo-URL KalshiAdapter builder
│   ├── polymarket_test.py # test-wallet PolymarketAdapter + $5 lock assertion
│   ├── sandbox_db.py     # arbiter_sandbox connection + table dumps
│   └── evidence.py       # per-scenario directory, log redirect, DB dump
├── scenarios/
│   ├── test_kalshi_happy.py       # D-05 TEST-01
│   ├── test_polymarket_happy.py   # D-06 TEST-02
│   ├── test_fok_rejection.py      # D-07 (both platforms)
│   ├── test_timeout_cancel.py     # D-08 (Kalshi demo, CR-01 live)
│   ├── test_safety_killswitch.py  # D-09 SAFE-01
│   ├── test_safety_oneleg.py      # D-09 SAFE-03 (injected)
│   ├── test_safety_ratelimit.py   # D-09 SAFE-04 (injected)
│   └── test_safety_shutdown.py    # D-09 SAFE-05 (subprocess + SIGINT)
├── runbook.py            # helpers for manual-observation scenarios
└── __env__.sandbox.md    # operator README (or link to .env.sandbox.template)

.env.sandbox.template     # template with DATABASE_URL → arbiter_sandbox, KALSHI_BASE_URL → demo-api, POLY_PRIVATE_KEY placeholder, PHASE4_MAX_ORDER_USD=5
evidence/04/<scenario>/   # gitignored; artifacts per-run

arbiter/sql/migrations/
└── 002_sandbox_init.sql  # OPTIONAL — only if arbiter_sandbox needs distinct schema extensions; otherwise same migrations apply

docker-compose.yml         # MODIFIED — add POSTGRES_MULTIPLE_DATABASES env + init script to create arbiter_sandbox

arbiter/config/settings.py
  # MODIFIED lines 365, 376:
  base_url: str = field(default_factory=lambda: os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"))
  clob_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"))

arbiter/execution/adapters/polymarket.py
  # MODIFIED: in place_fok, before submit, if PHASE4_MAX_ORDER_USD set and qty * price > it → return _failed_order("PHASE4_MAX_ORDER_USD hard-lock exceeded")
```

### Pattern 1: `@pytest.mark.live` opt-in marker

**What:** Tests are skipped by default; run with `pytest -m live`. Required to avoid burning real $$$ / hitting real APIs during normal CI/dev.

**When to use:** Every Phase 4 scenario test.

**Example:**
```python
# arbiter/sandbox/conftest.py — add to existing project conftest.py behavior
# Source: https://til.simonwillison.net/pytest/only-run-integration (VERIFIED 2026-04)
import pytest

def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run Phase 4 sandbox live-fire scenarios (real API calls, real money on Polymarket)",
    )

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: Phase 4 sandbox live-fire scenario — requires real API creds + --live flag",
    )

def pytest_collection_modifyitems(config, items):
    if config.getoption("--live") or config.getoption("-m") == "live":
        return
    skip_live = pytest.mark.skip(reason="Use -m live or --live to run Phase 4 scenarios")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
```

```python
# arbiter/sandbox/scenarios/test_kalshi_happy.py
@pytest.mark.live
async def test_kalshi_happy_lifecycle(demo_kalshi_adapter, sandbox_db, evidence_dir):
    # ... test body ...
```

**Note on project's existing async runner:** Project's root `conftest.py` uses a custom `pytest_pyfunc_call` that calls `asyncio.run()` on coroutine tests. Sandbox tests should be plain `async def` functions — do NOT add `@pytest.mark.asyncio`. The root hook handles it.

### Pattern 2: Subprocess-based SAFE-05 shutdown test

**What:** Launch `python -m arbiter.main --api-only --port X` in a subprocess, wait for `/api/health` 200, place a demo order, send SIGINT, assert dashboard WS emits `shutdown_state` with `phase=shutting_down` **before** the process terminates.

**When to use:** D-09 SAFE-05 graceful-shutdown scenario. Cannot be exercised in-process because the shutdown path involves real signal handlers.

**Example pattern already exists in `arbiter/test_api_integration.py`:**
```python
# Source: arbiter/test_api_integration.py:40-46 + free_port helper
# Adapt: use .env.sandbox, capture subprocess stdout to evidence_dir/run.log.jsonl,
# use os.kill(proc.pid, signal.SIGINT) to trigger shutdown
```

### Pattern 3: Fault injection via `monkeypatch` on adapter methods

**What:** Wrap `KalshiAdapter.place_fok` (or `PolymarketAdapter.place_fok`) such that the second call raises — cleanly simulates one-leg failure without touching production code.

**When to use:** D-09 SAFE-03 one-leg recovery (inject second-leg failure); D-09 SAFE-04 rate-limit burst (flood `RateLimiter.acquire`).

**Example:**
```python
# arbiter/sandbox/scenarios/test_safety_oneleg.py
@pytest.mark.live
async def test_one_leg_recovery_injected(
    demo_kalshi_adapter, poly_test_adapter, monkeypatch, evidence_dir
):
    # First leg succeeds (real Kalshi demo call).
    # Second leg: patch PolymarketAdapter.place_fok to raise on call #1.
    call_count = {"n": 0}
    original = poly_test_adapter.place_fok

    async def flaky_place_fok(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("INJECTED: simulated Polymarket failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(poly_test_adapter, "place_fok", flaky_place_fok)
    # ... run arb that exercises one-leg recovery path ...
    # Assert: one_leg_exposure incident emitted; naked position cancelled; no real $$$ burned beyond Kalshi demo leg
```

### Pattern 4: Reconcile Polymarket fees via `get_trades()` after fill

**What:** Polymarket's `post_order` response does not include fees. To verify TEST-04, query `client.get_trades(TradeParams(maker_address=..., market=token_id))` shortly after fill and reconstruct the platform-charged fee from `fee_rate_bps × size × price × (1 - price)`.

**Example:**
```python
# arbiter/sandbox/fixtures/polymarket_test.py or inline in test
from py_clob_client.clob_types import TradeParams

async def verify_polymarket_fee(client, token_id, expected_fee_fn):
    trades = await loop.run_in_executor(
        None,
        lambda: client.get_trades(TradeParams(
            maker_address=client.get_address(),
            market=token_id,
        )),
    )
    # Each trade record carries fee_rate_bps, size, price
    # Source: py_clob_client/client.py::get_trades (VERIFIED 2026-04 locally)
    for trade in trades:
        rate_bps = int(trade.get("fee_rate_bps", 0))
        size = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        platform_fee = (rate_bps / 10_000.0) * size * price * (1.0 - price)
        predicted_fee = expected_fee_fn(price, quantity=int(size))
        assert abs(platform_fee - predicted_fee) <= 0.01, (
            f"Fee mismatch: platform={platform_fee:.4f} predicted={predicted_fee:.4f}"
        )
```

### Pattern 5: Evidence fixture (per-scenario directory + structlog redirect)

**What:** A single `evidence_dir` fixture that:
1. Creates `evidence/04/<scenario>/` on test start
2. Adds a structlog processor that writes to `run.log.jsonl` in that directory
3. On test teardown, dumps `execution_orders`, `execution_fills`, `execution_incidents` via `asyncpg.fetch('SELECT * FROM ...') → json.dump`
4. Writes `balances_pre.json` and `balances_post.json` from BalanceMonitor

**When to use:** Every live scenario.

### Anti-Patterns to Avoid
- **Writing to prod DB under any circumstance.** Always assert `DATABASE_URL` contains `arbiter_sandbox` before any write in a `@pytest.mark.live` test. Fail loudly in the fixture if not.
- **Running the Polymarket test wallet without the `PHASE4_MAX_ORDER_USD` lock.** D-02 mandates belt-and-suspenders — the fixture should refuse to build the adapter if `PHASE4_MAX_ORDER_USD` is unset.
- **Skipping the cancel-on-timeout live scenario because it "feels covered" by unit tests.** D-08 exists precisely because CR-01 has only been verified against mocks, never against real Kalshi demo behavior.
- **Treating `fee_rate_bps` as a post-trade field.** It is a request field. Post-trade fee recovery requires `get_trades()` + arithmetic reconstruction.
- **Using `@pytest.mark.asyncio`.** Project's custom `conftest.py` handles coroutine tests via `asyncio.run()`; adding the marker will double-wrap and break.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| pytest opt-in marker | Custom env-var gate | `pytest_collection_modifyitems` + `@pytest.mark.live` | Standard pytest pattern — future-proof, interoperates with `-m` selector |
| Multi-DB Postgres in compose | Second postgres service | `POSTGRES_MULTIPLE_DATABASES` env + initdb.d script | Single source of truth, single backup, single port |
| Async event-loop runner in tests | pytest-asyncio decorators | Existing `conftest.py pytest_pyfunc_call` hook | Project already has it; adding asyncio markers conflicts |
| Fee-math duplication for verification | Re-implementing fee formula in tests | Import `kalshi_order_fee` / `polymarket_order_fee` from `settings.py`; also import `_kalshi_fee` / `_polymarket_fee` from `math_auditor.py` for cross-check | These are the two independent implementations; ±1¢ tolerance applies to both |
| Balance capture | New balance fetcher | Existing `BalanceMonitor.fetch_balance()` — Kalshi via `/portfolio/balance`, Polymarket via USDC ERC20 `balanceOf` | Already implemented, already tested |
| DB schema for sandbox | New migration | Same `arbiter/sql/migrate.py` against `arbiter_sandbox` | Migrations are append-only and DB-agnostic; run `migrate.py` with `DATABASE_URL` pointed at sandbox |
| Demo Kalshi client | New SDK binding | Existing `KalshiCollector` + `KalshiAdapter` with `KALSHI_BASE_URL` env var | Auth flow is identical between demo and prod (same RSA-PSS signing, same headers) |
| HTTP recording | VCR / cassettes | structlog JSON + DB dumps | D-14 rejected recorder approach; sufficient audit trail already |

**Key insight:** Phase 4 is an integration layer. **No new production code beyond (a) the two config lines at `settings.py:365,376` and (b) the `PHASE4_MAX_ORDER_USD` hard-lock in `polymarket.py`.** Everything else is test-only infrastructure in `arbiter/sandbox/`.

## Runtime State Inventory

Phase 4 is additive. It introduces a second database and a test harness. Runtime state to check:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `arbiter_dev` DB should NEVER receive Phase 4 writes | Fixture asserts `DATABASE_URL` contains `arbiter_sandbox` before any write |
| Live service config | Kalshi demo account credentials (separate from prod); Polymarket test wallet address | Stored in `.env.sandbox` (NEW); operator must generate demo RSA key + create throwaway Polymarket wallet |
| OS-registered state | None — Phase 4 does not register background services, pm2 processes, or scheduled tasks | None |
| Secrets/env vars | New env vars: `KALSHI_BASE_URL`, `POLYMARKET_CLOB_URL`, `PHASE4_MAX_ORDER_USD`, `POLY_PRIVATE_KEY` (overridden to test wallet in sandbox), `DATABASE_URL` (overridden to `arbiter_sandbox`) | Document in `.env.sandbox.template`; existing `.env.template` stays untouched |
| Build artifacts | None — no new compiled packages, no egg-info | None |

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Postgres 16 via docker-compose | Sandbox DB (D-03) | ✓ | compose pins 16-alpine | — |
| py-clob-client | Polymarket tests | ✓ | 0.34.6 | — |
| asyncpg | Sandbox DB access | ✓ | 0.31.0 | — |
| pytest / pytest-asyncio | Harness | ✓ | 8.3.4 / 0.25.0 | — |
| Kalshi demo account + RSA key | TEST-01 | ✗ (operator provisions) | — | Operator must sign up at demo.kalshi.co, generate API key, save private key to `keys/kalshi_demo_private.pem`, fund via test card (NOT auto-faucet) |
| Polymarket test wallet + USDC | TEST-02, TEST-04 | ✗ (operator provisions) | — | Operator must create throwaway wallet, bridge ≤$10 USDC to Polygon (minimum $3 per docs); this is a one-time setup |
| Polygon RPC | Polymarket balance fetch | ✓ | Existing `polygon-rpc.com`, `rpc.ankr.com/polygon`, `polygon.llamarpc.com` in `collectors/polymarket.py:415` | Public RPC — should Just Work |

**Missing dependencies with no fallback:** None blocking planner. Operator provisioning of demo accounts / test wallet is a Phase 4 Task 1 prerequisite and should be called out in the README.

**Missing dependencies with fallback:** Kalshi demo funding is not automated — operator funds via test card per Kalshi help docs. Planner should include a "fund your demo account to ~$100 before running scenarios" step in the README.

## Common Pitfalls

### Pitfall 1: Assuming Kalshi fill response has `realized_fee`
**What goes wrong:** Tests assert on `order_data["realized_fee"]` and always fail with `KeyError`.
**Why it happens:** `04-CONTEXT.md` D-18 names the field `realized_fee`. This was incorrect even as of Phase 4 context gathering. The actual field as of the January 2026 API migration is `fee_cost` (on fill objects from `/portfolio/fills` and the fill WebSocket) and `taker_fees_dollars` / `maker_fees_dollars` (on create-order responses since Oct 2025).
**How to avoid:** TEST-04 fee verification must consume the fill endpoint (`GET /portfolio/fills?order_id=...`) and read `fee_cost` as a dollar-string. Parse with `Decimal(fill["fee_cost"])`.
**Warning signs:** Any test code containing the literal string `realized_fee` in the Kalshi path.
**Source:** [VERIFIED: docs.kalshi.com/changelog "fee_cost fixed-point dollars string added to fill WebSocket messages (Jan 29, 2026)"; docs.kalshi.com/api-reference/portfolio/get-fills.md confirms field is `fee_cost`]

### Pitfall 2: Treating Polymarket order response as fee-bearing
**What goes wrong:** Tests try to read `response["fee"]` or `response["fee_rate_bps"]` from `post_order()` — both are absent.
**Why it happens:** `fee_rate_bps` is a **request** field on `OrderArgs`, resolved dynamically by `create_order()` via `GET /fee-rate`. It does NOT appear in the post_order response. The response shape is `{success, errorMsg, orderID, takingAmount, makingAmount, status, transactionsHashes, tradeIDs}`.
**How to avoid:** Reconstruct fee after fill by calling `client.get_trades(TradeParams(maker_address=..., market=token_id))`. Each trade record has `fee_rate_bps`, `size`, `price`. Compute `fee = (fee_rate_bps / 10_000) * size * price * (1 - price)` and compare against `polymarket_order_fee()`.
**Warning signs:** Code that tries to index `response.get("fee")` after `post_order`.
**Source:** [VERIFIED: local inspection of py_clob_client v0.34.6 `client.py` `post_order` and `create_order`; docs.polymarket.com/developers/CLOB/orders/create-order confirms response fields]

### Pitfall 3: Expecting HTTP non-2xx on FOK rejection
**What goes wrong:** Test asserts `response.status_code != 200` for Kalshi FOK rejection; never fires because Kalshi returns HTTP 201 with `status: canceled` in body.
**Why it happens:** Kalshi treats FOK rejection as successful API call with a rejected order, not an API error.
**How to avoid:** Parse response body. Assert `order_data["status"] == "canceled"`. KalshiAdapter's `_FOK_STATUS_MAP` already handles this — tests should read the returned `Order.status == OrderStatus.CANCELLED`.
**Warning signs:** Test code that inspects `response.status` (HTTP) as the rejection gate instead of the returned `Order.status`.
**Source:** [CITED: docs.kalshi.com/api-reference/orders/create-order.md "Successful FoK: 201 executed; Rejected FoK: 201 canceled"]

### Pitfall 4: Polymarket thin-liquidity FOK may return `FOK_ORDER_NOT_FILLED_ERROR` via exception, not success=false
**What goes wrong:** Test expects `response["success"] == False`, but py-clob-client surfaces the rejection as a thrown exception from `post_order`.
**Why it happens:** The SDK raises on 4xx / error responses; the error body carries `FOK_ORDER_NOT_FILLED_ERROR` but the SDK does not return `{success: false}` — it raises.
**How to avoid:** Wrap `post_order` in try/except; inspect `str(exc)` for `FOK_ORDER_NOT_FILLED_ERROR` substring. Note: `PolymarketAdapter._place_fok_reconciling` already has broad exception handling but does NOT distinguish this specific error — the test will need to check logs for the specific rejection reason rather than rely on adapter output.
**Warning signs:** Test that only checks `Order.status == OrderStatus.FAILED` without inspecting the error string.
**Source:** [CITED: docs.polymarket.com/developers/CLOB/orders/orders "FOK failures raise FOK_ORDER_NOT_FILLED_ERROR"]

### Pitfall 5: Polymarket `min_order_size` is per-market, not global
**What goes wrong:** Plan picks a $1 order size; platform rejects with `INVALID_ORDER_MIN_SIZE` because the target market's `min_order_size` is 5 contracts.
**Why it happens:** Polymarket publishes `min_order_size` per market in the order book summary response (observed values: `"5"` for contracts, `"0.001"` shows up on some markets for base-unit denominated sizes).
**How to avoid:** Before placing the TEST-02 happy-path order, query `client.get_order_book(token_id)` and read `min_order_size`. Choose a target market where `min_order_size` × `price` ≤ 5 USDC so the $5 hard-lock is not tripped. Common safe choice: markets with `min_order_size=5` and price ≤ $0.20 → notional $1.
**Warning signs:** Plan hardcodes a qty/price without a pre-flight `get_order_book` check.
**Source:** [CITED: docs.polymarket.com/api-reference/orderbook/get-order-book-summary (field `min_order_size`); Polymarket community docs confirm typical `"5"` value]

### Pitfall 6: Kalshi demo test card funding is manual
**What goes wrong:** Plan assumes demo accounts come pre-funded; first scenario fails with `insufficient_funds`.
**Why it happens:** Kalshi demo accounts arrive with $0. Funding requires adding a test card (Visa `4000 0566 5566 5556` or Mastercard `5200 8282 8282 8210`) via the demo UI — no API faucet.
**How to avoid:** Include a manual "fund demo account to ≥$100" step in the operator README (D-04). Planner should not attempt to automate demo funding.
**Warning signs:** Any task that tries to fund Kalshi demo programmatically.
**Source:** [VERIFIED: help.kalshi.com/en/articles/13823775-demo-account]

### Pitfall 7: `KALSHI_BASE_URL` change requires matching private key swap
**What goes wrong:** Operator sets `KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2` but leaves `KALSHI_PRIVATE_KEY_PATH` pointed at the prod key. Every request 401s.
**Why it happens:** Demo and prod use separate API keys (same RSA-PSS signing mechanism, but key IDs registered against different environments).
**How to avoid:** `.env.sandbox.template` must set both `KALSHI_API_KEY_ID` (operator fills in demo key ID) and `KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_demo_private.pem`. README must tell operator to save the demo key separately.
**Warning signs:** A test setup that only swaps `KALSHI_BASE_URL` without verifying auth with a `GET /portfolio/balance` probe.
**Source:** [VERIFIED: docs.kalshi.com/getting_started/api_keys.md "Demo and Production: The signing process is the same, but keys are registered per-environment"]

### Pitfall 8: Polymarket order-size hard-lock must compute notional, not raw qty
**What goes wrong:** `PHASE4_MAX_ORDER_USD=5` but the lock compares `qty > 5` → blocks a 100-contract order at $0.04 (notional $4, safe) and lets a 10-contract order at $0.60 (notional $6, unsafe) through.
**Why it happens:** Notional = qty × price for prediction markets. Raw qty is meaningless as a risk limit.
**How to avoid:** In `PolymarketAdapter.place_fok`, compute `notional_usd = float(qty) * float(price)` and compare that against `PHASE4_MAX_ORDER_USD`. Reject with a clear error string if exceeded.
**Warning signs:** Hard-lock code that looks at `qty` alone, not `qty * price`.

## Code Examples

### Env-var override for base URLs (D-01)

```python
# arbiter/config/settings.py — replace lines 363-366 and 373-381
# Source: existing file (VERIFIED — current defaults are hardcoded at lines 365, 376)

@dataclass
class KalshiConfig:
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "KALSHI_BASE_URL",
            "https://api.elections.kalshi.com/trade-api/v2",
        )
    )
    ws_url: str = field(
        default_factory=lambda: os.getenv(
            "KALSHI_WS_URL",
            "wss://api.elections.kalshi.com/trade-api/ws/v2",
        )
    )
    # ... rest unchanged


@dataclass
class PolymarketConfig:
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = field(
        default_factory=lambda: os.getenv(
            "POLYMARKET_CLOB_URL",
            "https://clob.polymarket.com",
        )
    )
    # ... rest unchanged
```

### `.env.sandbox.template`

```bash
# arbiter_sandbox environment — Phase 4 live-fire scenarios
# Copy to .env.sandbox and fill in values. Source with `set -a; source .env.sandbox; set +a`
# before running `pytest -m live`.

# --- Core ---------------------------------------------------------
DRY_RUN=false

# --- Postgres (SEPARATE DB from prod) -----------------------------
DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_sandbox

# --- Kalshi (DEMO — separate key from prod) -----------------------
KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
KALSHI_WS_URL=wss://demo-api.kalshi.co/trade-api/ws/v2
KALSHI_API_KEY_ID=<demo-api-key-id-from-demo.kalshi.co>
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_demo_private.pem

# --- Polymarket (PRODUCTION — throwaway wallet, ≤$10 USDC) --------
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLY_PRIVATE_KEY=<TEST-WALLET-PRIVATE-KEY-ONLY>
POLY_SIGNATURE_TYPE=2
POLY_FUNDER=<test-wallet-funder-address>

# --- Phase 4 hard-lock (belt-and-suspenders with $10 wallet cap) --
PHASE4_MAX_ORDER_USD=5

# --- Telegram (optional) ------------------------------------------
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### docker-compose multi-database init

```yaml
# docker-compose.yml — modify existing postgres service
# Source: https://www.bitdoze.com/multiple-postgres-databases-docker/ +
#         https://github.com/mrts/docker-postgresql-multiple-databases (VERIFIED 2026-04)

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${PG_DATABASE:-arbiter_dev}
      POSTGRES_USER: ${PG_USER:-arbiter}
      POSTGRES_PASSWORD: ${PG_PASSWORD:-arbiter_secret}
      POSTGRES_MULTIPLE_DATABASES: arbiter_sandbox   # NEW for Phase 4
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./arbiter/sql/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
      - ./arbiter/sql/init-sandbox.sh:/docker-entrypoint-initdb.d/init-sandbox.sh:ro  # NEW
    # ... rest unchanged
```

```bash
#!/bin/bash
# arbiter/sql/init-sandbox.sh (NEW)
# Creates the arbiter_sandbox database alongside arbiter_dev.
set -e
if [ -n "$POSTGRES_MULTIPLE_DATABASES" ]; then
    for db in $(echo $POSTGRES_MULTIPLE_DATABASES | tr ',' ' '); do
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
            CREATE DATABASE $db;
            GRANT ALL PRIVILEGES ON DATABASE $db TO $POSTGRES_USER;
EOSQL
    done
fi
```

### Polymarket adapter hard-lock (D-02)

```python
# arbiter/execution/adapters/polymarket.py — modify place_fok
# Add after config.polymarket.private_key check, before circuit.can_execute()

async def place_fok(self, arb_id, market_id, canonical_id, side, price, qty):
    now = time.time()

    # ... existing auth / circuit checks ...

    # Phase 4 blast-radius hard-lock (D-02): applies in both prod and sandbox,
    # no-op when the env var is unset. Notional > lock → FAILED, logged.
    max_order_usd_raw = os.getenv("PHASE4_MAX_ORDER_USD")
    if max_order_usd_raw:
        try:
            max_order_usd = float(max_order_usd_raw)
        except (TypeError, ValueError):
            max_order_usd = 0.0
        notional_usd = float(qty) * float(price)
        if notional_usd > max_order_usd:
            log.warning(
                "polymarket.phase4_hardlock.rejected",
                arb_id=arb_id, notional=notional_usd, max=max_order_usd,
                qty=qty, price=price,
            )
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"PHASE4_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
            )
    # ... rest of existing code ...
```

### Evidence fixture

```python
# arbiter/sandbox/fixtures/evidence.py
import json
import pathlib
import pytest
import structlog
from datetime import datetime

EVIDENCE_ROOT = pathlib.Path("evidence/04")

@pytest.fixture
async def evidence_dir(request, sandbox_db):
    scenario = request.node.name  # e.g., "test_kalshi_happy_lifecycle"
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    directory = EVIDENCE_ROOT / f"{scenario}_{timestamp}"
    directory.mkdir(parents=True, exist_ok=True)

    # Redirect structlog to this directory's run.log.jsonl
    log_file = directory / "run.log.jsonl"
    log_fh = log_file.open("w", encoding="utf-8")

    def file_writer(logger, method_name, event_dict):
        log_fh.write(json.dumps(event_dict, default=str) + "\n")
        log_fh.flush()
        return event_dict

    # Snapshot processors, inject file writer
    original_processors = structlog.get_config().get("processors", [])
    structlog.configure(processors=original_processors + [file_writer])

    yield directory

    # Teardown: DB dumps
    try:
        for table in ("execution_orders", "execution_fills", "execution_incidents", "execution_arbs"):
            rows = await sandbox_db.fetch(f"SELECT * FROM {table}")
            (directory / f"{table}.json").write_text(
                json.dumps([dict(r) for r in rows], indent=2, default=str),
                encoding="utf-8",
            )
    finally:
        log_fh.close()
        structlog.configure(processors=original_processors)
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.4 + pytest-asyncio 0.25.0 (installed, but not used — project conftest.py owns coroutine dispatch) |
| Config file | Project root `conftest.py` (existing); NEW `arbiter/sandbox/conftest.py` for `--live` opt-in |
| Quick run command | `pytest arbiter/ -q` (skips `@pytest.mark.live` by default) |
| Full suite command | `pytest arbiter/ -q && pytest arbiter/sandbox/ -m live --live` (live suite requires `.env.sandbox` sourced) |
| Phase gate command | `pytest arbiter/sandbox/ -m live --live -v` (all 8 scenarios must pass + `04-VALIDATION.md` filled) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Scenario Tag | Evidence | File (Wave 0) |
|--------|----------|-----------|--------------|----------|---------------|
| TEST-01 | Kalshi demo happy-path lifecycle (submit → fill → record) | live | `real` | structlog + DB rows + balance delta | `arbiter/sandbox/scenarios/test_kalshi_happy.py::test_kalshi_happy_lifecycle` |
| TEST-01 | Kalshi demo FOK rejection (thin-liquidity) | live | `real` | structlog (status:canceled) + DB rows | `arbiter/sandbox/scenarios/test_fok_rejection.py::test_kalshi_fok_rejected` |
| TEST-01 | Kalshi demo execution-timeout + cancel (CR-01 live) | live | `real` | list_open_orders_by_client_id result + cancel-success log | `arbiter/sandbox/scenarios/test_timeout_cancel.py::test_kalshi_timeout_cancel` |
| TEST-02 | Polymarket real-$1–5 happy-path lifecycle | live | `real` | structlog + DB rows + USDC balance delta | `arbiter/sandbox/scenarios/test_polymarket_happy.py::test_polymarket_happy_lifecycle` |
| TEST-02 | Polymarket FOK rejection | live | `real` | exception-match on FOK_ORDER_NOT_FILLED_ERROR | `arbiter/sandbox/scenarios/test_fok_rejection.py::test_polymarket_fok_rejected` |
| TEST-03 | Pre/post balance snapshots match recorded PnL ±1¢ | live (assertion within every scenario's teardown) | `real` | `balances_pre.json` + `balances_post.json` + pnl_reconciler.reconcile() report | Assertion helper in `arbiter/sandbox/fixtures/reconcile.py` called from each scenario |
| TEST-04 | Kalshi `fee_cost` from GET /portfolio/fills matches `kalshi_order_fee()` ±1¢ | live | `real` | fill JSON + computed fee | Part of `test_kalshi_happy.py` assertions |
| TEST-04 | Polymarket reconstructed fee (rate_bps × size × price × (1-price)) matches `polymarket_order_fee()` ±1¢ | live | `real` | trade record from get_trades + computed fee | Part of `test_polymarket_happy.py` assertions |
| SAFE-01 (live validation via D-09) | Kill-switch cancels open demo order within 5s | live | `real` (manual arm + auto observe) | supervisor log + adapter.cancel_all return | `arbiter/sandbox/scenarios/test_safety_killswitch.py::test_kill_switch_cancels_open_kalshi_demo_order` |
| SAFE-03 | One-leg exposure detected + Telegram + WS event | live | `injected` (second-leg mock raises) | one_leg_exposure incident in execution_incidents + WS payload | `arbiter/sandbox/scenarios/test_safety_oneleg.py::test_one_leg_recovery_injected` |
| SAFE-04 | Rate-limit backoff under burst; rate_limit_state WS reflects penalty | live | `injected` (flood RateLimiter.acquire from test) | rate_limiter.stats snapshot showing remaining_penalty_seconds > 0 | `arbiter/sandbox/scenarios/test_safety_ratelimit.py::test_rate_limit_backoff_and_ws` |
| SAFE-05 | SIGINT → graceful shutdown cancels open orders before exit | live (subprocess) | `real` (SIGINT real, open order on Kalshi demo) | subprocess stderr shutdown_state phase sequence | `arbiter/sandbox/scenarios/test_safety_shutdown.py::test_sigint_cancels_open_kalshi_demo_orders` |

### Sampling Rate
- **Per task commit:** `pytest arbiter/ -q` (excludes `@pytest.mark.live`; < 30s local)
- **Per wave merge:** Same as above (live suite is cost-gated — cannot run on every commit)
- **Phase gate:** Full live suite (`pytest arbiter/sandbox/ -m live --live -v`) with `.env.sandbox` sourced; ALL 12 scenarios pass OR `04-VALIDATION.md` documents every failure with evidence and mitigation plan. D-19 hard-gates Phase 5 on this.

### Wave 0 Gaps
- [ ] `arbiter/sandbox/__init__.py` — empty package marker
- [ ] `arbiter/sandbox/conftest.py` — `--live` option + marker registration
- [ ] `arbiter/sandbox/fixtures/kalshi_demo.py` — demo adapter builder (asserts KALSHI_BASE_URL contains "demo-api.kalshi.co")
- [ ] `arbiter/sandbox/fixtures/polymarket_test.py` — test-wallet adapter builder (asserts PHASE4_MAX_ORDER_USD is set)
- [ ] `arbiter/sandbox/fixtures/sandbox_db.py` — asyncpg pool against arbiter_sandbox (asserts DATABASE_URL contains "arbiter_sandbox")
- [ ] `arbiter/sandbox/fixtures/evidence.py` — per-scenario dir + structlog redirect + DB dump teardown
- [ ] `arbiter/sandbox/fixtures/reconcile.py` — TEST-03/04 assertion helpers (±1¢ tolerance)
- [ ] `arbiter/sandbox/scenarios/test_kalshi_happy.py`
- [ ] `arbiter/sandbox/scenarios/test_polymarket_happy.py`
- [ ] `arbiter/sandbox/scenarios/test_fok_rejection.py`
- [ ] `arbiter/sandbox/scenarios/test_timeout_cancel.py`
- [ ] `arbiter/sandbox/scenarios/test_safety_killswitch.py`
- [ ] `arbiter/sandbox/scenarios/test_safety_oneleg.py`
- [ ] `arbiter/sandbox/scenarios/test_safety_ratelimit.py`
- [ ] `arbiter/sandbox/scenarios/test_safety_shutdown.py`
- [ ] `.env.sandbox.template` — operator bootstrap
- [ ] `arbiter/sql/init-sandbox.sh` + docker-compose.yml update for `arbiter_sandbox` DB
- [ ] Production-code changes: `arbiter/config/settings.py:365,376` (env-var defaults); `arbiter/execution/adapters/polymarket.py` (PHASE4_MAX_ORDER_USD hard-lock in `place_fok`)
- [ ] README section on sandbox bootstrap (generate demo Kalshi key, fund demo, create test wallet, bridge USDC)

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes (sandbox uses real Kalshi RSA keys + Polymarket wallet keys) | Demo key in separate file (`keys/kalshi_demo_private.pem`), `.env.sandbox` gitignored, test wallet private key never committed |
| V3 Session Management | no | Phase 4 runs as test process; no operator session flows |
| V4 Access Control | yes | Sandbox fixtures MUST reject prod DB writes; hard-lock enforces max notional |
| V5 Input Validation | yes | Existing FOK price validation in KalshiAdapter; PHASE4_MAX_ORDER_USD notional check (NEW) |
| V6 Cryptography | yes | Reuse existing RSA-PSS signing (KalshiAuth) and Polymarket EIP-712 (py-clob-client); no new crypto |

### Known Threat Patterns for This Phase

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Test writes to prod DB by mistake | Tampering | Fixture assertion on `DATABASE_URL` substring `arbiter_sandbox` before any session.execute |
| Prod Polymarket wallet key leaks into sandbox run | Information Disclosure | `.env.sandbox` sources a SEPARATE `POLY_PRIVATE_KEY`; README warns against copy-pasting prod wallet; `.gitignore` covers `.env.sandbox` |
| Order notional exceeds intended cap | Tampering (of intent) | Belt-and-suspenders: (a) ~$10 wallet funding cap, (b) `PHASE4_MAX_ORDER_USD` adapter hard-lock; either alone is insufficient per D-02 |
| Replay of Kalshi demo client_order_id on prod | Tampering | Different `ARB-` sequence in demo (empty execution_orders in `arbiter_sandbox`); no cross-env contamination |
| 429 from rate-limit burst test floods real Kalshi prod | DoS against Kalshi | Scenarios pin `KALSHI_BASE_URL` to demo-api.kalshi.co; fixture assertion enforces this |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `POSTGRES_MULTIPLE_DATABASES` init-script pattern is compatible with postgres:16-alpine | Architecture — docker-compose | LOW. Widely-used pattern (mrts/docker-postgresql-multiple-databases has 1k+ stars); if it somehow breaks, fall back to explicit `CREATE DATABASE` executed from `arbiter/sql/migrate.py` startup |
| A2 | Polymarket `get_trades()` returns `fee_rate_bps` on each trade record | Common Pitfalls 2, Code Examples | MEDIUM. py-clob-client 0.34.6 source confirms `get_trades` returns raw dict via `response["data"]`, but the exact fields were not inspectable without a live call. If `fee_rate_bps` is absent per-trade, fallback is to call `GET /fee-rate?token_id=...` at test time and use that rate for reconstruction. Validation: first live Polymarket run reveals the shape; plan Task 1 should print `trades[0].keys()` before asserting. |
| A3 | Kalshi demo and prod have the same rate-limit tier | Security | LOW. Docs don't explicitly state tier parity but no evidence of divergence. Worst case: demo is lower tier and SAFE-04 injected scenario trips 429 faster — which is a feature, not a bug |
| A4 | Kalshi demo supports `yes_price_dollars` / `count_fp` fields introduced Jan 2026 | Phase Requirements — TEST-01 | LOW. Changelog describes these as API-wide migrations, not prod-only. Demo mirrors prod behavior per docs. Validation: the TEST-01 happy-path scenario IS the validation. |
| A5 | Polymarket test wallet with ≤$10 USDC can place a $1 FOK on a low-price market | Phase Requirements — TEST-02 | LOW. Polymarket minimum USDC deposit is $3; `min_order_size` varies per market but `5 × $0.04 = $0.20` notional fits within $5 hard-lock. Operator picks target market in Task 1 discovery step. |
| A6 | The existing `conftest.py pytest_pyfunc_call` hook works correctly when combined with `pytest.mark.live` + custom `pytest_collection_modifyitems` | Architecture Pattern 1 | LOW. Both are pytest hooks that operate at different phases (collection modify vs function call); no ordering conflict documented. Validation: Task 0 regression — run `pytest -m live --live arbiter/sandbox/test_smoke.py::test_noop` (an empty async test) and confirm it runs. |

**If this table is empty:** It is not empty — 6 assumptions flagged. A2 is the most material for the plan. Plan Task 1 (Polymarket happy-path) should include a discovery step that prints the first trade's field list to confirm A2 before asserting on fee reconstruction.

## Open Questions

1. **Does Kalshi demo rate limit respect the same per-tier levels as production?**
   - What we know: Rate-limits doc lists Basic/Advanced/Premier/Prime tiers but doesn't mention demo.
   - What's unclear: Whether a new demo account gets Basic or something even lower.
   - Recommendation: Start with SafetyConfig defaults (Kalshi 10 writes/sec, which matches Basic). If SAFE-04 injected-burst scenario trips 429 before expected, lower the config defaults for the demo env only.

2. **Does `get_trades()` field shape match py-clob-client's server spec, or are there wrapper transforms?**
   - What we know: The SDK returns `response["data"]` as a raw list of dicts from the CLOB HTTP API.
   - What's unclear: Exact field names in a live trade record (docs don't publish schema).
   - Recommendation: First Polymarket live run should log `json.dumps(trades[0], indent=2)` to evidence dir; plan's Task 1 includes this discovery step and adjusts fee reconstruction if `fee_rate_bps` is named differently.

3. **Should the `PHASE4_MAX_ORDER_USD` hard-lock stay in production code after Phase 4, or be removed in Phase 5?**
   - What we know: CONTEXT.md D-02 says the hard-lock is "enforced before every Polymarket submit" during Phase 4 runs.
   - What's unclear: Phase 5 policy.
   - Recommendation: Keep the check but make it env-driven — no env var = no lock. Phase 5 can set a different `MAX_ORDER_USD` budget or unset it entirely. The code stays as a general-purpose safety.

4. **Where do the `@pytest.mark.live` tests find the existing `SafetySupervisor` + `ExecutionEngine`?**
   - What we know: SAFE-* scenarios need real engine + supervisor, not mocks.
   - What's unclear: Whether to spin up a full `arbiter.main` subprocess per scenario, or construct engine+supervisor in-process per test.
   - Recommendation: In-process construction for SAFE-01/03/04 (faster, reuses fixtures); subprocess for SAFE-05 only (signal handlers require a real process). The test_api_integration.py subprocess pattern is the template for SAFE-05.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Kalshi fills had `realized_pnl`/`fees_paid` | Kalshi fills have `fee_cost` (fixed-point dollar string) + `yes_price_dollars`/`no_price_dollars` | Jan 2026 API migration | CONTEXT.md D-18 name is stale; use `fee_cost` |
| Kalshi order response had `fee_cost` | Kalshi order response has `taker_fees_dollars` + `maker_fees_dollars` (split) | Oct 2025 | If you want fee at submit time, it's two fields now; fills endpoint keeps combined `fee_cost` |
| py-clob-client was 0.25.x (README snippets) | py-clob-client 0.34.6 | Feb 2026 | OrderType enum has FOK/GTC/GTD/FAK; two-phase create_order+post_order is standard |

**Deprecated/outdated:**
- Kalshi legacy `trading-api.kalshi.com` endpoint — replaced by `api.elections.kalshi.com` for prod and `demo-api.kalshi.co` for demo. Current `settings.py:365` is correct.
- Kalshi integer-cent `yes_price` field — removed; dollar-string only (Phase 1 already migrated).

## Sources

### Primary (HIGH confidence)
- Local: `py_clob_client` v0.34.6 source inspection via `inspect.getsource()` — post_order, create_order, get_order, get_trades signatures verified
- Local: `pip show` verification for all 7 dependencies (versions locked in RESEARCH table)
- Local: `arbiter/execution/adapters/kalshi.py` + `polymarket.py` + `arbiter/execution/engine.py` + `arbiter/execution/store.py` code inspection
- Local: `arbiter/test_api_integration.py` — subprocess + free_port pattern for SAFE-05
- Local: `arbiter/safety/test_supervisor.py` + `arbiter/safety/conftest.py` — existing fixture patterns
- [Kalshi demo environment docs](https://docs.kalshi.com/getting_started/demo_env) — `https://demo-api.kalshi.co/trade-api/v2` verified
- [Kalshi API Keys docs](https://docs.kalshi.com/getting_started/api_keys.md) — demo/prod auth parity
- [Kalshi API changelog](https://docs.kalshi.com/changelog) — `fee_cost` field (Jan 29, 2026); `taker_fees_dollars`/`maker_fees_dollars` (Oct 9, 2025); `yes_price_dollars`/`no_price_dollars` naming convention
- [Kalshi Get Fills endpoint](https://docs.kalshi.com/api-reference/portfolio/get-fills.md) — Fill object schema with `fee_cost`
- [Kalshi Create Order endpoint](https://docs.kalshi.com/api-reference/orders/create-order.md) — FOK returns HTTP 201 with `status: executed` or `status: canceled`
- [Kalshi Rate Limits](https://docs.kalshi.com/getting_started/rate_limits.md) — Basic/Advanced/Premier/Prime tier table
- [Kalshi Demo Account help](https://help.kalshi.com/en/articles/13823775-demo-account) — manual funding via test cards (no auto-faucet)

### Secondary (MEDIUM confidence)
- [Polymarket Order Types doc](https://docs.polymarket.com/developers/CLOB/orders/orders) — OrderType semantics; `FOK_ORDER_NOT_FILLED_ERROR`; `INVALID_ORDER_MIN_SIZE`
- [Polymarket Create Order doc](https://docs.polymarket.com/developers/CLOB/orders/create-order) — response shape `{success, errorMsg, orderID, takingAmount, makingAmount, status, transactionsHashes, tradeIDs}`; statuses `live`/`matched`/`delayed`/`unmatched`
- [Polymarket deposit doc](https://docs.polymarket.com/trading/bridge/deposit) — $3 minimum Polygon USDC
- [py-clob-client README](https://github.com/Polymarket/py-clob-client/blob/main/README.md) — v0.34.6 existence + example sections
- [pytest custom markers docs](https://docs.pytest.org/en/stable/example/markers.html) — opt-in pattern
- [Simon Willison TIL: opt-in integration tests](https://til.simonwillison.net/pytest/only-run-integration) — exact boilerplate for `--live` flag

### Tertiary (LOW confidence)
- [mrts/docker-postgresql-multiple-databases](https://github.com/mrts/docker-postgresql-multiple-databases) — init-script pattern for `POSTGRES_MULTIPLE_DATABASES` (widely referenced but not official Postgres docs; flagged A1)
- [AgentBets Kalshi API Guide](https://agentbets.ai/guides/kalshi-api-guide/) — cross-reference for demo/prod base URLs
- Community reports on Polymarket `min_order_size: "5"` per-market typical value

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all versions verified via local `pip show`
- Architecture: HIGH — existing patterns reused (subprocess test, async conftest, structlog, BalanceMonitor, ExecutionStore)
- Pitfalls: HIGH for Pitfalls 1, 3, 6, 7; MEDIUM for Pitfall 2 (fee reconstruction depends on A2); HIGH for Pitfalls 4, 5, 8
- Fee-field research: HIGH for Kalshi (`fee_cost`), MEDIUM for Polymarket (reconstruction formula is deterministic but requires live verification of `get_trades` schema — A2)

**Research date:** 2026-04-16
**Valid until:** 2026-05-16 for Kalshi/Polymarket API details (fast-moving); stable thereafter for pytest/asyncpg/structlog patterns

---

*Phase: 04-sandbox-validation*
*Research completed: 2026-04-16*
