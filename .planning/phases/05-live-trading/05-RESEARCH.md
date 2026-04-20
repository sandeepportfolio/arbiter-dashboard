# Phase 5: Live Trading - Research

**Researched:** 2026-04-20
**Domain:** Production go-live for cross-platform prediction-market arbitrage (Kalshi + Polymarket)
**Confidence:** HIGH (domain + code surface), MEDIUM (Polymarket April 2026 upgrade tail), MEDIUM (operator workflow — new surface)

## Summary

Phase 5 is a **thin go-live layer**, not a feature phase. The heavy lifting (FOK adapters, kill-switch, per-platform exposure, one-leg recovery, rate limiting, graceful shutdown, readiness gate, ExecutionStore, structlog JSON, sandbox harness) was delivered in phases 1-4. What Phase 5 adds is (1) the operator-facing workflow to **enter** live mode safely, (2) the **sizing + abort rules** for the very first real trade, and (3) the **evidence + reconciliation** needed to declare TEST-05 done.

The critical design insight: the system already BLOCKS live startup when credentials are missing, mappings aren't confirmed, profitability is still collecting, or balances are unobserved (`OperationalReadiness.startup_failures` in `arbiter/readiness.py:105-126`). That safety net is the foundation. Phase 5 must not weaken it; it must add **temporary** Phase-5-specific layers on top (a notional hard-lock analog to `PHASE4_MAX_ORDER_USD`, an operator-approval gate, and a known-ticker allowlist) that can be removed once confidence is proven.

**Primary recommendation:** Introduce a `PHASE5_MAX_ORDER_USD` adapter-layer hard-lock (identical pattern to `PHASE4_MAX_ORDER_USD`) set to $5-$10, require operator-confirmation-per-trade via a new readiness check or dashboard button for the first N trades, and enforce a post-trade reconcile-or-abort gate (±$0.01 like Phase 4's D-17). Reuse the Phase 4 sandbox/evidence pattern — `arbiter/live/` harness, `scenario_manifest.json`, structlog JSONL, ±$0.01 reconcile helpers — adapted for production venues. Do NOT build new execution primitives; the execution engine already has the live path wired at `engine.py:467-470`.

## User Constraints (from Project)

_CONTEXT.md has not been written for Phase 5 yet (no discuss phase run). The following constraints come from `CLAUDE.md` and `PROJECT.md`; the planner should treat them as locked until a CONTEXT.md supersedes them._

### Locked Decisions (from CLAUDE.md + PROJECT.md)

- **Capital ceiling: <$1K per platform initially.** The system must handle small position sizes. First live trade sizing MUST be dramatically smaller than this ceiling (see §Position Sizing below — $10 notional target).
- **Timeline: ASAP.** Get to live trades fast, even with manual monitoring. Interpretation: Phase 5 is not a place to refactor for elegance; it is a place to cross the go-live line safely.
- **Risk tolerance: low.** Cannot afford to lose capital to bugs. **Safety > speed.** When these conflict, safety wins.
- **Platforms: Kalshi + Polymarket only** (PredictIt removed in Phase 4.1; locked in PROJECT.md Key Decisions).
- **Dependency gate: Phase 4 D-19 must be PASS** (`04-VALIDATION.md` `phase_gate_status` flips to `PASS`) before any Phase 5 work is permitted. 9 live-fire scenarios + terminal reconciliation. Currently `PENDING`.

### Claude's Discretion (planner/researcher freedom)

- Naming and placement of the `PHASE5_*` env-var hard-lock (mirror Phase 4's pattern or introduce new names)
- Whether the first-trade approval gate lives in `OperationalReadiness` (new check), `SafetySupervisor` (new auto-arm rule), or as a new `PHASE5_MAX_FIRST_TRADES` counter in the execution path
- Operator-supervision protocol specifics (Telegram confirm-per-trade? dashboard "approve next trade" button? 30s manual cooldown between trades?)
- Whether to require an allowlist of canonical_ids for the first N trades (extending `allow_auto_trade` in `MARKET_MAP`) or trust the existing mapping status
- Exact abort-threshold values (recommended defaults below; planner may tune after reading Phase 4 scenario telemetry when available)

### Deferred Ideas (OUT OF SCOPE for Phase 5)

- OPT-01..05 (WebSocket feeds, liquidity-aware sizing, annualized-return scoring, automated daily-loss kill switch, Telegram `/kill` command) — explicitly v2 in REQUIREMENTS.md
- MON-01..04 (settlement divergence monitoring, dynamic fee rates via SDK, automated reconciliation scheduling, latency percentiles) — v2
- Automated position scaling (PROJECT.md "Out of Scope": manual capital allocation initially)
- Multi-user dashboard, mobile app, backtesting engine, additional platforms, HFT strategies (REQUIREMENTS.md "Out of Scope")

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| TEST-05 | First live arbitrage trade executed successfully with small capital ($10-50) under operator supervision | §Operator Supervision, §Position Sizing, §Reconciliation & Settlement, §Evidence & Post-Trade Recording, §Minimum Viable Scope |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Go-live gating | Configuration + readiness (`arbiter/readiness.py`) | `main.py` startup | `startup_failures()` already blocks live mode for missing creds/mappings. Phase 5 extends `allow_execution()` with a new first-trade-approval check. |
| Per-trade notional cap | Adapter layer (`arbiter/execution/adapters/{kalshi,polymarket}.py`) | Env-var (`PHASE5_MAX_ORDER_USD`) | Phase 4 proved this pattern. Cap belongs at the last mile so no code path can bypass it, including recovery/retry. |
| Operator approval loop | API layer (new `/api/live-approve-trade` endpoint?) OR SafetySupervisor state | Dashboard UI | Keeps the human in the loop at the exact moment an opportunity reaches execution. Supervisor already owns armed/disarmed state. |
| Post-trade reconciliation | `arbiter/audit/pnl_reconciler.py` + new `arbiter/live/reconcile.py` helpers | `arbiter/live/` harness | Reuse `reconcile.assert_pnl_within_tolerance` pattern from sandbox. |
| Evidence capture | Scenario-style JSONL + structlog + Postgres dumps | `arbiter/live/` harness | Mirrors Phase 4 `evidence/04/<scenario>_<ts>/` layout. |
| Abort-and-freeze | SafetySupervisor.trip_kill (`arbiter/safety/supervisor.py:125`) | Auto-trigger from reconcile failure | `trip_kill` is the canonical abort mechanism; Phase 5 adds an automated caller that invokes it when reconciliation breaches ±$0.01. |
| Dashboard visibility | Existing WS events (`kill_switch`, `shutdown_state`, `rate_limit_state`, `one_leg_exposure`) | `/api/readiness` | Already wired in Phase 3. Phase 5 may add a `live_trade_approval` WS event. |

## Standard Stack

_No new libraries are required. Phase 5 reuses what already landed in phases 1-4._

### Core (already installed)
| Library | Version (installed) | Purpose | Why Standard |
|---------|---------------------|---------|--------------|
| py-clob-client | 0.25.x (OPS-04 may bump to 0.34.x) | Polymarket CLOB order placement | The Polymarket-sanctioned Python client — maintained by Polymarket itself. `[CITED: github.com/Polymarket/py-clob-client]` |
| aiohttp | 3.9.0+ | Async HTTP for Kalshi REST + dashboard | Matches project standard `[VERIFIED: requirements.txt]` |
| cryptography | 41.0.0+ (OPS-04 may bump to 46.x) | Kalshi RSA signing | Project standard `[VERIFIED: requirements.txt]` |
| structlog | (used via `arbiter/utils/logger.py`) | JSON structured logging | OPS-01 landed in Phase 2; every trading op already emits JSON `[VERIFIED: arbiter/execution/engine.py:17]` |
| pytest + asyncio + monkeypatch | 5.0+ | Scenario test harness | Project standard; Phase 4 sandbox already uses it `[VERIFIED: arbiter/sandbox/]` |

### Supporting (already wired)
| Module | Purpose | When to Use |
|--------|---------|-------------|
| `arbiter.safety.SafetySupervisor` | Kill-switch + shutdown fanout | Already the execution gate. Phase 5 invokes `trip_kill` on automated abort. |
| `arbiter.readiness.OperationalReadiness` | Live-readiness gating | Extend with a Phase-5-specific check (operator approval / first-N-trades counter). |
| `arbiter.sandbox.reconcile.assert_pnl_within_tolerance` | ±$0.01 reconciliation | Reuse as the production reconciliation primitive. |
| `arbiter.execution.store.ExecutionStore` | Postgres persistence of orders/fills/incidents | Every Phase 5 trade writes here automatically via `engine._live_execution` → `store.record_arb`. |
| `arbiter.audit.PnLReconciler` | 5-minute balance-vs-ledger reconciliation | Already wired in `main.py:374` via `run_reconciliation_loop`. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Adapter-layer env-var hard-lock (Phase-4 style) | RiskManager-layer notional check | Adapter layer is the LAST line of defense; RiskManager can be bypassed by any code path that gets an adapter reference. Adapter layer is safer. |
| Operator-approval-per-trade | Trust existing `allow_auto_trade` + kill-switch | Safer for first N trades; relaxable later. User constraint ("low risk tolerance") requires the tighter loop initially. |
| Auto-abort on reconcile breach | Log-and-alert only | Low risk tolerance + "cannot afford to lose capital to bugs" → auto-abort (trip_kill). |
| New `arbiter/live/` harness | Run live in existing `arbiter/sandbox/` with env-var flag | Separate harness keeps "production pytest" distinct from "sandbox pytest" and prevents accidental sandbox env leakage. |

**Installation:** No new packages required. Verify existing pin versions before go-live:
```bash
pip show py-clob-client aiohttp cryptography structlog
python -c "import py_clob_client; print(py_clob_client.__version__)"
```

**Version verification:** `py-clob-client` released 0.34.x line in late 2025; OPS-04 in REQUIREMENTS.md already flags the bump. If OPS-04 is still pending at Phase 5 start, the planner must decide whether to bump first (safer, but bigger surface) or lock to the tested 0.25.x and defer the bump to v2 (recommended — go live on the version Phase 4 validated against). `[ASSUMED — version pin not re-verified this session]`

## Architecture Patterns

### System Architecture Diagram

Phase 5 trade flow (reusing existing components):

```
┌─────────────────────────────────────────────────────────────────────┐
│  OPERATOR                                                            │
│  ├─ .env (DRY_RUN=false, PHASE5_MAX_ORDER_USD=10, ...)               │
│  ├─ `python -m arbiter.main --live`                                  │
│  └─ Dashboard (ARM kill-switch, approve trades, watch fills)         │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  main.py::main()                                                     │
│  ├─ `--live` sets scanner.dry_run=False                              │
│  ├─ readiness.startup_failures() — blocks if creds/mappings missing  │
│  └─ asyncio.run(run_system(config, ...))                             │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Collector → Price Store → Scanner → ExecutionEngine.execute()       │
│                                          │                            │
│                                          ▼                            │
│  chained_gate(opp):                                                  │
│    1. readiness.allow_execution(opp) — balances, profitability, etc.  │
│    2. [NEW Phase 5] operator-approval / first-N-trades counter       │
│    3. safety.allow_execution(opp) — kill-switch state                │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ approved
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  engine._live_execution(arb_id, opp):                                │
│    ├─ requoted = pre_trade_requote(opp)                              │
│    ├─ audit_opportunity(requoted)                                    │
│    └─ asyncio.gather(                                                │
│         adapter[yes].place_fok(...),  ─┐                              │
│         adapter[no].place_fok(...))    │                              │
│                                         │ each adapter enforces:      │
│                                         │  - PHASE5_MAX_ORDER_USD     │
│                                         │  - rate_limiter.acquire()   │
│                                         │  - circuit.can_execute()    │
│                                         ▼                              │
│                                    Kalshi REST / Polymarket CLOB       │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Post-trade:                                                         │
│    ├─ engine._recover_one_leg_risk (if one leg failed)               │
│    ├─ risk.record_trade (per-platform exposure)                      │
│    ├─ store.record_arb (Postgres persistence)                        │
│    ├─ [NEW Phase 5] reconcile_post_trade(execution):                 │
│    │     - fetch platform fills + fees via get_trades / get_fills    │
│    │     - compare recorded vs platform-reported                     │
│    │     - if breach > ±$0.01 → safety.trip_kill(reason="recon_fail")│
│    ├─ balance_monitor sees new balances (on its poll tick)           │
│    └─ PnLReconciler.reconcile (every 5 min) — flags drift            │
└─────────────────────────────────────────────────────────────────────┘
```

### Recommended Project Structure

```
arbiter/
├── live/                           # NEW — Phase 5 harness (mirror of sandbox/)
│   ├── __init__.py
│   ├── README.md                   # Operator runbook (mirror of sandbox/README.md)
│   ├── conftest.py                 # Guard-rail fixtures (assert PHASE5_MAX_ORDER_USD)
│   ├── evidence.py                 # evidence_dir fixture (reuse from sandbox)
│   ├── reconcile.py                # assert_pnl_within_tolerance (reuse/import)
│   ├── manifest.py                 # scenario_manifest.json writer
│   └── test_first_live_trade.py    # The actual live-fire "scenario"
├── execution/
│   └── adapters/
│       ├── kalshi.py               # ADD PHASE5_MAX_ORDER_USD check (same pattern as PHASE4)
│       └── polymarket.py           # ADD PHASE5_MAX_ORDER_USD check
├── readiness.py                    # ADD new check (e.g., _check_first_trade_approval)
└── safety/
    └── supervisor.py               # POSSIBLY extend with auto_trip_on_reconcile_breach
```

### Pattern 1: Adapter-layer notional hard-lock (copy from Phase 4)
**What:** An env-var gate at the top of `place_fok` / `place_resting_limit` that rejects any order where `qty * price > PHASE5_MAX_ORDER_USD`.
**When to use:** Always on during Phase 5. Unset in v2 when automated scaling lands.
**Example:**
```python
# Source: arbiter/execution/adapters/polymarket.py:96-115 (PHASE4 pattern, 2026-04-20)
max_order_usd_raw = os.getenv("PHASE5_MAX_ORDER_USD")
if max_order_usd_raw:
    try:
        max_order_usd = float(max_order_usd_raw)
    except (TypeError, ValueError):
        max_order_usd = 0.0  # unparseable -> safe default: reject everything
    notional_usd = float(qty) * float(price)
    if notional_usd > max_order_usd:
        log.warning("phase5_hardlock.rejected",
                    arb_id=arb_id, notional=notional_usd, max=max_order_usd)
        return self._failed_order(
            arb_id, market_id, canonical_id, side, price, qty, now,
            f"PHASE5_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
        )
```

### Pattern 2: Readiness-chained trade gate
**What:** The execution engine's `chained_gate` in `main.py:285-294` composes readiness + safety. Phase 5 inserts a new check (operator approval / first-N-trades counter) between them or into readiness itself.
**When to use:** The cleanest insertion point for a new gating rule without forking the engine.
**Example:**
```python
# arbiter/readiness.py (new check — illustrative)
def _check_first_trade_approval(self) -> ReadinessCheck:
    # The operator must explicitly approve each of the first N live trades.
    approval = getattr(self, "_pending_approval", None)
    if self._live_trade_count >= self.config.first_trade_approval_limit:
        return ReadinessCheck(key="first_trade_approval", status="pass",
                              summary="First-trade approval window exceeded; auto-approved",
                              blocking=False)
    if approval is None:
        return ReadinessCheck(key="first_trade_approval", status="fail",
                              summary="Live trade awaiting operator approval",
                              blocking=True,
                              details={"pending_count": self._live_trade_count + 1})
    return ReadinessCheck(key="first_trade_approval", status="pass",
                          summary=f"Operator approved trade #{self._live_trade_count + 1}",
                          blocking=False)
```

### Pattern 3: Post-trade reconciliation + auto-abort
**What:** After `_live_execution` returns, a new coroutine compares recorded PnL/fees against platform-reported values (via `client.get_trades(...)` for Polymarket, fills endpoint for Kalshi). On breach > ±$0.01, call `safety.trip_kill`.
**When to use:** Every live execution. Reconciliation runs synchronously before the next opportunity is considered.
**Example:**
```python
# arbiter/live/reconcile.py (new — sketched)
async def reconcile_post_trade(execution, adapters, tolerance: float = 0.01):
    # For each platform with a fill, fetch platform-reported fee + fill price.
    discrepancies = []
    for leg in (execution.leg_yes, execution.leg_no):
        if leg.status != OrderStatus.FILLED:
            continue
        platform_fee = await adapters[leg.platform].get_trade_fee(leg.order_id)
        computed_fee = compute_fee(leg.platform, leg.fill_price, leg.fill_qty)
        if abs(platform_fee - computed_fee) > tolerance:
            discrepancies.append({"leg": leg.to_dict(), "platform_fee": platform_fee,
                                   "computed_fee": computed_fee})
    return discrepancies
```

### Anti-Patterns to Avoid
- **Bypassing the readiness gate with a flag** — "just this once" becomes "every time." The gate is the contract.
- **Modifying engine._live_execution to add first-trade gating** — that's where the execution primitive lives; keep it dumb. Gate at `chained_gate`.
- **Running Phase 5 evidence in the Phase 4 sandbox tree** — contaminates the audit trail. Separate `arbiter/live/` harness.
- **Manual reconciliation only** — too easy to skip. Auto-reconcile on every trade; alert if the reconciler itself fails.
- **Loosening `PHASE4_MAX_ORDER_USD` before Phase 5 safety layer lands** — Phase 4 evidence depends on that hard-lock; removing it ahead of adding `PHASE5_MAX_ORDER_USD` creates a gap window.
- **Forgetting to document how to EXIT live mode** — Phase 5 deliverable must include "flip back to dry-run" runbook; otherwise live mode becomes sticky.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Order placement with idempotency | Custom retry-with-client-id | Existing `KalshiAdapter.place_fok` + `PolymarketAdapter._place_fok_reconciling` | Both already handle client_order_id threading (CR-02), duplicate detection (Polymarket reconcile), timeout-cancel (EXEC-05). |
| Kill-switch state machine | New "emergency stop" | `SafetySupervisor.trip_kill` | Already has cooldown, Redis optional, Postgres audit, Telegram, WS fanout. |
| Per-platform exposure tracking | New ledger | `RiskManager._platform_exposures` (`engine.py:222`) | SAFE-02 closed-loop on submitted/recovering/filled already tested. |
| Balance-vs-PnL reconciliation | New reconciler | `PnLReconciler` (`arbiter/audit/pnl_reconciler.py`) | Already runs every 5 min with ±$0.50 threshold; tune to ±$0.01 for Phase 5. |
| Structured JSON logging | Custom JSON formatter | `arbiter/utils/logger.py::setup_logging` + `structlog.contextvars.bind_contextvars` | Already emitting arb_id + canonical_id on every exec log line (Pitfall 6 of OPS-01). |
| Evidence capture | Custom directory writer | Copy Phase 4 `arbiter/sandbox/evidence.py` + `scenario_manifest.json` pattern | Aggregator already knows how to read these. |
| Reconciliation tolerance | New assertion helpers | Reuse `arbiter/sandbox/reconcile.py::assert_pnl_within_tolerance` | ±$0.01 = D-17 = project standard. |
| Telegram alerting | New bot | `BalanceMonitor.notifier` (already owned by monitor, shared with SafetySupervisor) | Single Telegram client; never instantiate another. |
| Startup gating | New "is live ready?" checker | `OperationalReadiness.startup_failures` + `.allow_execution` | Already blocks 3 classes of misconfig; extend rather than replace. |
| Dashboard kill-switch UI | New UI button | Existing ARM/RESET flow in dashboard | Wired to `/api/kill-switch` and WS events; just verify it works on mainnet. |

**Key insight:** Phase 5 has virtually no new code to write for the execution path. The risk is in what **surrounds** the execution path: pre-flight checklist, approval loop, reconciliation tolerance, abort automation, and operator runbook. Treat Phase 5 as 80% operational/procedural work and 20% thin Python layer.

## Runtime State Inventory

Phase 5 is additive, not a refactor/rename — nevertheless there is live runtime state that matters for go-live:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | Postgres: `execution_orders`, `execution_fills`, `execution_incidents`, `execution_arbs`, `safety_events`, `market_mappings`. All schemas verified in Phase 3/4 (`arbiter/sql/init.sql`). | On switch to live, point `DATABASE_URL` at the PRODUCTION database (NOT `arbiter_sandbox`). Decision needed: reuse `arbiter` (current dev DB) or create a new `arbiter_live`? Recommend: **new DB** so Phase 4 sandbox data and Phase 5 live data stay separable for audit. |
| Live service config | `MARKET_MAP` in `arbiter/config/settings.py` — curated dict of confirmed canonical_ids with `allow_auto_trade`. `iter_confirmed_market_mappings(require_auto_trade=True)` gates live start. | Operator must confirm at least ONE real mapping with `allow_auto_trade=True`, `resolution_criteria` populated, and `resolution_match_status="identical"` per SAFE-06. Consider: allowlist-of-one for the first live trade to reduce blast radius. |
| OS-registered state | None found. The system runs as a foreground Python process; no systemd/Task Scheduler/pm2 registration in the repo. | None. If deploying under a process supervisor, that is out of scope for Phase 5 (would be a v2 infra phase). |
| Secrets/env vars | `.env` at repo root holds `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `POLY_PRIVATE_KEY`, `POLY_FUNDER`, `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DRY_RUN`, `EXECUTION_TIMEOUT_S`, `PHASE4_MAX_ORDER_USD`. Phase 5 adds `PHASE5_MAX_ORDER_USD`. **Kalshi production RSA key must be different from the demo key** (`./keys/kalshi_private.pem` vs `./keys/kalshi_demo_private.pem` per `sandbox/README.md:33-36`). | 1) Create `.env.production` template mirroring `.env.sandbox.template` structure. 2) Verify prod Kalshi API key is a DIFFERENT file from demo. 3) Confirm `POLY_PRIVATE_KEY` is a wallet loaded with intended live capital (<$1K per user constraint). 4) Document that `DRY_RUN=false` in `.env.production` is the operator's deliberate, signed-off choice. |
| Build artifacts | None that embed environment identity. Python process reads env vars at import; restarts pick up new env. | None. |

**The canonical question for Phase 5:** *What's the smallest runtime-state change that takes the currently-healthy 27-hour DRY_RUN process from simulating trades to placing one real trade, and what invariants must hold before AND after that change?*

## Common Pitfalls

### Pitfall 1: Kalshi demo vs production API-key mismatch (401 on every call)
**What goes wrong:** The operator keeps `.env` pointing at demo URL but swaps in production credentials, or vice versa. Every authenticated call returns 401.
**Why it happens:** Kalshi demo and prod use separate RSA keypairs (per Phase 4 README Pitfall 7). Keys aren't portable across environments.
**How to avoid:** Verify `KALSHI_BASE_URL` and `KALSHI_PRIVATE_KEY_PATH` are BOTH set to prod values. Add a startup log line that prints `KALSHI_BASE_URL` (masking the path) so the operator can sanity-check on every launch. Add a preflight that does a read-only authenticated call (`/portfolio/balance`) before the first write.
**Warning signs:** 401 in very first Kalshi API call after `--live` flip; balance shows $0 despite known funding.

### Pitfall 2: Polymarket fee reconstruction discrepancy on small orders
**What goes wrong:** `computed_fee` (fee model) and `platform_fee` (actual charged) drift by >$0.01 because the fee rate is market-category-specific and our fallback rates (politics=0.04, crypto=0.072, etc.) don't match the specific market's live rate.
**Why it happens:** Polymarket returns per-market fee rates in `client.get_markets()`; our fallback rates are approximations. At $5-$10 notional, a 1% rate miss is $0.05-$0.10 — blows through the ±$0.01 tolerance. `[CITED: arbiter/sandbox/test_polymarket_happy_path.py — TEST-04 Pitfall 2]`
**How to avoid:** Pre-flight: fetch the actual fee rate for the target market via `client.get_market(condition_id)` and PIN it into the fee compute. If the pinned rate differs from our fallback by >0.1 percentage points, flag and halt.
**Warning signs:** Phase 4 Scenario 2/4 PnL reconcile breach on Polymarket leg; fee breakdown shows `computed_fee != platform_fee`.

### Pitfall 3: Race condition — one leg fills, other doesn't (LEG RISK)
**What goes wrong:** Classic cross-platform arb hazard. Yes leg fills on Kalshi; No leg on Polymarket rejects (insufficient liquidity, FOK miss, network drop). You're now naked long Yes.
**Why it happens:** Two independent venues; no atomic two-phase commit. `[VERIFIED: quantvps.com/blog/cross-market-arbitrage-polymarket]`
**How to avoid:** **Already mitigated.** FOK on both legs (EXEC-01). `engine._recover_one_leg_risk` detects and attempts unwind. `SAFE-03` raises one-leg incident + Telegram. This is the single most-tested safety pattern in the codebase — but it has NEVER been exercised against real money. The first Phase 5 trade might be the first true test.
**Warning signs:** `status="recovering"` in `ArbExecution`; `one_leg_exposure` WS event; Telegram alert with the unwind instructions.
**Phase-5-specific action:** Keep notional so small ($5-$10) that even a failed unwind is affordable.

### Pitfall 4: Stale-price execution at flip time
**What goes wrong:** Prices update every 1-30s (polling-based, OPT-01 not done). By the time the scanner flags an opportunity and the engine re-quotes, the book has moved; your $2.50 edge is now $0.10 or negative.
**Why it happens:** Prediction-market spreads are wide and thin; 5-10s lag between fetch and fire can eat the entire edge.
**How to avoid:** `engine._pre_trade_requote` already re-fetches before firing (`engine.py:458`). `max_quote_age_seconds=15.0` (settings.py:378) is enforced. For Phase 5, consider tightening to `max_quote_age_seconds=5.0` for the first N trades; if you miss trades that's fine — goal is clean execution, not throughput.
**Warning signs:** `status="stale"` on candidate opportunities; pre_trade_requote returning None frequently.

### Pitfall 5: Rate-limit bans during burst activity
**What goes wrong:** First live trade excites the operator who manually approves multiple opportunities in quick succession; platforms 429/403 the burst and soft-ban the account.
**Why it happens:** Kalshi 10 writes/sec, Polymarket ~5 writes/sec (SAFE-04 `arbiter/utils/retry.py::RateLimiter`). A burst of UI-driven approvals plus heartbeat + balance polls can clip the limit.
**How to avoid:** Rate limiter already wrapped at every adapter call site (Phase 3 SAFE-04). For Phase 5, enforce a **minimum 30s operator cooldown between approvals** for the first N trades — simpler than tuning rate limits.
**Warning signs:** `rate_limit_state` WS event flips to THROTTLED; adapter returns `rate_limited` error.

### Pitfall 6: Settlement delay surprises (Kalshi vs Polymarket asymmetry)
**What goes wrong:** Operator expects immediate balance update after fill; Kalshi shows it fast, Polymarket's on-chain settlement takes minutes; reconciliation loop runs during the gap and flags a false discrepancy.
**Why it happens:** Polymarket's CLOB matches off-chain then settles on-chain via Polygon (USDC transfer + outcome-token atomic swap). Match is near-instant; on-chain settlement takes ~2-30s per Polygon block times. `[CITED: docs.polymarket.com/developers/CLOB/introduction, quantvps.com/blog/polymarket-clob]`
**How to avoid:** Phase 5 reconciliation must WAIT for on-chain confirmation before running the ±$0.01 gate. Options: (a) poll `client.get_trades(market=condition_id)` every 2s until the fill appears with a tx hash, with a 60s timeout; (b) use `polygonscan` RPC directly. Recommend (a) — stays within the py-clob-client abstraction.
**Warning signs:** Immediately-post-trade reconcile shows huge discrepancy; discrepancy converges to $0 after 10-60s.

### Pitfall 7: Polymarket April 2026 stablecoin migration drift
**What goes wrong:** Polymarket migrated from USDC.e to "Polymarket USD" (new stablecoin) as collateral around April 6, 2026, with rollout through late April. If our test wallet has the old USDC.e and CTF Exchange V2 expects the new token, orders fail.
**Why it happens:** Platform-side breaking change mid-way through our own timeline. `[CITED: news.bitcoin.com/polymarkets-april-2026-upgrade, paymentexpert.com/2026/04/08]`
**How to avoid:** As of 2026-04-20, verify current migration status. Check: (a) is the funder wallet holding USDC.e or the new Polymarket USD? (b) does py-clob-client 0.25.x support the new collateral, or do we need to bump? (c) are our target markets on the old Exchange or V2? Consult Polymarket docs + the `/changelog` endpoint of the CLOB API before first live trade.
**Warning signs:** Orders accepted then immediately rejected with "unsupported collateral" or "signature version"; `client.get_balance()` shows 0 despite wallet having USDC.e; py-clob-client raises `SignatureError` on EIP-712 order signing.
**Phase 5 action:** Before Phase 5 starts, do a 30-minute reconnaissance — read the latest Polymarket developer docs + py-clob-client release notes. If mid-migration, either wait a week or verify our pinned client+collateral combination is still supported. `[ASSUMED — specific migration impact on our test wallet not verified this session]`

### Pitfall 8: Kalshi quadratic fee + integer-cent rounding at tiny notional
**What goes wrong:** For a $5 position at price $0.50, Kalshi's taker fee = `ceil(0.07 * 10 * 0.5 * 0.5 * 100) / 100 = ceil(17.5) / 100 = $0.18`. Our fee model (`kalshi_order_fee`) rounds up to cents; but our reconciler compares platform_fee == computed_fee at the penny — any off-by-one in rounding breaks ±$0.01. `[CITED: kalshi.com/docs/kalshi-fee-schedule.pdf, whirligigbear.substack.com]`
**Why it happens:** Order-level rounding in `math.ceil((raw_fee * 100.0) - 1e-9) / 100.0` (`settings.py:70`) is the right algorithm but has a `1e-9` epsilon that might disagree with Kalshi's server rounding on boundary values.
**How to avoid:** `TEST-04` in Phase 4 validates exactly this; assuming Phase 4 Scenario 1 passes, our rounding is correct. If it fails, that's Phase 4's problem, not Phase 5's. Phase 5 must NOT run until TEST-04 is green.
**Warning signs:** Phase 4 `04-VALIDATION.md` shows a FEE reconciliation breach.

### Pitfall 9: Withdrawal friction on success
**What goes wrong:** First trade clears $0.50 profit; operator wants to withdraw. Polymarket withdrawal is free on Polygon but involves a bridge to USDC.e/USD, then eventually to a CEX. Kalshi withdrawals go back to the linked ACH account and take 1-3 business days.
**Why it happens:** Prediction-market venues are designed to keep capital in-venue, not for frequent withdrawal. `[CITED: docs.polymarket.com/trading/bridge/withdraw]`
**How to avoid:** Phase 5 does NOT include a withdrawal step. Profits stay on-platform. Document this explicitly in the runbook: "profits are on-platform; withdrawal is a manual post-phase step." Polygon withdrawals cost <$0.01 in gas; bridging to Ethereum costs $5-$20. For a $0.50 profit, withdrawal ECONOMICS DO NOT WORK; this is a known v1 quirk.
**Warning signs:** Operator asks "where's my $0.50?" — explain the calculus.

### Pitfall 10: Operator desensitization / alert fatigue
**What goes wrong:** After the first successful trade, operator lowers vigilance; system hits an unexpected edge case on trade #3 that would have been caught by the same attention level as trade #1.
**Why it happens:** Human factors; "looks like it works" bias.
**How to avoid:** Enforce the first-N-trades approval gate for at least 5-10 trades (not just 1). Require each approval to come with a 30s cooldown. Log each approval as a structured event with operator comment.
**Warning signs:** Operator clicks approve <10s after receiving alert; comment field empty.

## Go-Live Preflight Checklist

_This is the Phase 5 operator-facing checklist. Every item must be verifiable before the first `DRY_RUN=false` execution._

| # | Check | How to Verify | Blocking? |
|---|-------|---------------|-----------|
| 1 | Phase 4 D-19 gate PASSED | `.planning/phases/04-sandbox-validation/04-VALIDATION.md` shows `phase_gate_status: PASS` | YES |
| 2 | Phase 4 all 9 scenarios observed | Same file, scenario table shows 0 PENDING | YES |
| 3 | Phase 4 code-review warnings resolved or accepted | 04-REVIEW.md status → `advisory`/`resolved` (WR-02 already rolled in per 04.1; WR-01/03/04/05 advisory) | YES — operator sign-off |
| 4 | Production Kalshi API key issued + loaded | `KALSHI_API_KEY_ID` set in `.env`, `./keys/kalshi_private.pem` exists, read-only `/portfolio/balance` call returns 200 | YES |
| 5 | Production Polymarket wallet funded | `POLY_PRIVATE_KEY`/`POLY_FUNDER` set; polygonscan shows ≥$50 USDC (or Polymarket USD post-migration) at funder address | YES |
| 6 | Kalshi account funded | Kalshi web UI shows ≥$100 | YES |
| 7 | `DATABASE_URL` points at live DB (not `arbiter_sandbox`, not `arbiter_dev`) | `psql $DATABASE_URL -c "SELECT current_database();"` returns `arbiter_live` (or chosen name) | YES |
| 8 | `PHASE5_MAX_ORDER_USD` set to ≤$10 | `echo $PHASE5_MAX_ORDER_USD` → e.g. `10` | YES |
| 9 | `PHASE4_MAX_ORDER_USD` UNSET (avoid overlap) | `echo $PHASE4_MAX_ORDER_USD` → empty | YES |
| 10 | Telegram alerting verified | Send manual `trip_kill` dry test; Telegram chat receives `[ARBITER] Kill switch armed` | YES |
| 11 | Dashboard kill-switch UI functional | Open dashboard, click ARM → banner shows; click RESET (after cooldown) → banner clears | YES |
| 12 | Readiness snapshot shows all greens | `curl localhost:8080/api/readiness` → `ready_for_live_trading: true` | YES |
| 13 | Polymarket April 2026 migration status verified | Check latest Polymarket blog + py-clob-client changelog; confirm pinned version + collateral combination is still supported | YES |
| 14 | At least one MARKET_MAP entry has `resolution_match_status="identical"` | Dashboard Markets page shows green resolution-criteria pill on the target mapping | YES |
| 15 | Operator has read the run-of-show runbook | Operator acknowledges: known abort triggers, reconciliation thresholds, rollback steps | YES |

## Operator Supervision Protocol

### During-trade protocol (mandatory for first 5-10 trades)

1. **Discovery:** Opportunity appears in dashboard feed with `status=tradable`.
2. **Alert:** System emits a `live_trade_approval` WebSocket event + Telegram message: "Opportunity detected: [canonical_id]; edge=X¢; notional=$Y; expires in Z s. Click APPROVE or IGNORE."
3. **Operator decision:** Operator has 60s to click APPROVE (or send Telegram `/approve <arb_id>` if we add the command) or ignore (auto-decline at 60s).
4. **Execution:** Engine proceeds only if approval received. `_live_execution` runs with all Phase 1-3 guards still active.
5. **Post-trade reconcile:** New `reconcile_post_trade` runs within 60s after both legs fill (or one leg fails + recovery). On breach, auto-`trip_kill`.
6. **Cooldown:** 30s minimum between approvals for the first 10 trades.

### Alternate simpler protocol (if the approval-button work is too heavy)

Run the bot with `DRY_RUN=false` but `PHASE5_MAX_ORDER_USD=5`, rely on existing kill-switch + small cap. Operator watches the dashboard with a finger on ARM. Accept that the operator is the human-in-the-loop without a dedicated approval button. This ships faster; first trade happens the first time an approved mapping produces a `tradable` opportunity while the operator is watching.

**Recommendation:** Go with the simpler protocol. The tight cap + kill-switch + readiness gate + small-trades count IS the supervision. Building an approval UI burns Phase 5 schedule without meaningful risk reduction given the existing guard-rail stack.

## Position Sizing for First Live Trade

### Standard industry practice (verified)
- "Start with position sizes that the book can absorb without moving the price more than 0.5%." `[CITED: polyguana.com/learn/polymarket-arbitrage]`
- "Ten small arbitrage trades are safer than one large one." `[CITED: tradealgo.com]`
- "There is no minimum, but arbitrage spreads on prediction markets are typically 1-4%. After fees, a $100 position on a 3% spread nets roughly $1-2." `[CITED: pariflow.com/blog/prediction-market-arbitrage-guide]`

### Arbiter-specific sizing (recommended)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `PHASE5_MAX_ORDER_USD` | $10 per leg | Bounded loss even if both legs go naked. Fits within $1K platform ceiling 100x over. |
| `scanner.max_position_usd` (existing) | Keep at $100 (settings.py:374 default) | Scanner already sizes to `max_position_usd / (yes_price + no_price)`. For a $0.50/$0.50 pair that gives 200 contracts — too large. Lower to $10 for Phase 5 first trades. |
| Effective first-trade size | $5-$10 notional total across both legs | Well under Polymarket's $3 deposit minimum + Kalshi's integer-contract rounding. |
| Minimum edge after fees | 2.5¢ net (existing `min_edge_cents` in settings.py:373) | Keep as is. On $10 notional a 2.5¢ edge = $0.25 expected profit — realistic and meaningful. |
| Expected profit first trade | $0.05-$0.50 | Goal is proving the system, not making money. |

**Recommendation:** Set `max_position_usd=10` in the Phase 5 config for the first 5-10 trades. Scale to $50 after clean execution, then $200. Do NOT lift the `PHASE5_MAX_ORDER_USD=10` cap until Phase 5 is SUMMARY-complete and a v2 sizing phase is planned.

## Abort Criteria

_Automated abort = auto-call `safety.trip_kill(by="system:phase5_abort", reason="<detail>")`. This halts all new execution and cancels open orders._

| # | Trigger | Threshold | Action |
|---|---------|-----------|--------|
| A1 | Fee reconciliation breach | platform_fee - computed_fee > ±$0.01 on any leg | auto-abort |
| A2 | PnL reconciliation breach | balance_delta - recorded_pnl > ±$0.01 on any platform | auto-abort |
| A3 | Slippage | fill_price - order_price > 0.5% on either leg | auto-abort (FOK should prevent this, but trust-but-verify) |
| A4 | One-leg exposure > 60s | `ArbExecution.status` stuck in `recovering` for >60s | auto-abort (SAFE-03 + Phase 5 timer) |
| A5 | Unexpected incident severity=critical | Any `ExecutionIncident` with `severity="critical"` | auto-abort |
| A6 | Rate-limit 429 repeated | >3 429s in 60s window on either platform | auto-abort (SAFE-04 already throttles; this is insurance) |
| A7 | Balance-monitor detects unexpected balance drop | Drop > 2x expected order notional, not matching an execution record | auto-abort |
| A8 | Operator kill-switch (manual) | Operator clicks ARM in dashboard or sends Telegram `/kill` | manual abort |

**Recommendation:** Implement A1 + A2 + A5 as auto-abort wired to `trip_kill` in Phase 5. A3, A4, A6, A7 are already covered by existing safety layer (Phase 3); verify they still function under live conditions. A8 is pre-existing.

## Reconciliation & Settlement

### Settlement timing per platform

| Platform | Match | Fill confirmation | Balance update | Reconciliation wait |
|----------|-------|-------------------|----------------|---------------------|
| Kalshi | Real-time REST response | Included in order response | Immediate in portfolio endpoint | 0-5 s |
| Polymarket | Real-time via `client.get_trades` | Visible via `client.get_trades(market=...)` within ~2s | USDC balance updates after Polygon on-chain settlement (~2-10s typical) | 10-60 s |

`[CITED: docs.polymarket.com/developers/CLOB/introduction — "Once a match is found, the transaction moves to the Polygon blockchain for settlement... atomic transfer"]`
`[VERIFIED: Kalshi fill endpoint + fixed-point dollar format per API changelog Jan 2026]`

### Phase 5 reconciliation loop

```
On ArbExecution completion:
  1. Sleep 2s (settle grace).
  2. Fetch platform-reported fill + fee:
     - Kalshi: GET /portfolio/fills?client_order_id=<X>
     - Polymarket: client.get_trades(market=condition_id) — filter by order_id
  3. For each leg:
     - If platform_reported_fill not found and < 60s elapsed: retry with exponential backoff
     - If > 60s elapsed and no fill visible: flag as MISSING_FILL incident, auto-abort
  4. Compare:
     - assert_fee_matches(platform, platform_fee, computed_fee, tolerance=0.01)
     - assert_pnl_within_tolerance(platform, pre_balance, post_balance, recorded_pnl, tolerance=0.01)
  5. On breach: safety.trip_kill(by="system:phase5_reconcile_fail", reason=<detail>)
  6. On success: record scenario_manifest.json + structlog line "phase5.reconcile.ok"
```

**Tolerance justification:** ±$0.01 matches Phase 4 D-17. Using a different tolerance for Phase 5 would be non-monotonic (how can we trust live if we demand less rigor than sandbox?).

### Manual confirmation on first trade

Regardless of automated reconciliation, the operator should ALSO manually:
1. Open Kalshi web UI → Portfolio → verify trade is listed with the expected contract count + price
2. Open Polymarket web UI (or polygonscan.com with the funder address) → verify position/USDC change
3. Confirm both manual checks match the dashboard's `ArbExecution` record
4. Screenshot or archive the manual confirmations into `evidence/05/first-live-trade_<ts>/manual/`

## Rollback Path

_If the first live trade goes badly, how do we unwind cleanly?_

### Scenarios and responses

| Scenario | Response |
|----------|----------|
| Both legs fill, profit is positive, reconcile passes | SUCCESS. Log the evidence. Leave positions to settle at event resolution. |
| Both legs fill, reconcile breaches | `trip_kill` fires automatically. Operator reviews: was the breach a fee-model miss (Pitfall 2) or an actual math bug? If fee-model, fix the computed rate and re-test in sandbox. If actual bug, write post-mortem + freeze Phase 5 until fixed. |
| One leg fills, other doesn't, recovery unwinds successfully | Expected SAFE-03 path. Review incident log, verify recovery was actually successful (not just marked as such), assess whether to continue Phase 5 or pause. |
| One leg fills, other doesn't, recovery fails | CRITICAL. Operator manually unwinds via platform web UI (close the naked leg at market). Accept the loss. Freeze Phase 5. Post-mortem mandatory. |
| Both legs fail (FOK rejection on both) | No-op. Operator verifies no partial fills, continues watching. |
| Positions held but one platform goes down (Kalshi/Polymarket outage) | Don't panic. Prediction markets settle at event resolution regardless of broker state. Wait it out unless the event is imminent. Consider manual unwind on the healthy platform if the outage is prolonged. |
| Capital lost exceeds 10% of starting balance | Hard stop: freeze trading (armed kill-switch), post-mortem, do NOT unfreeze until root cause identified + tested. |

### Mandatory rollback-to-dry-run procedure

```bash
# 1. Arm kill-switch to cancel any open orders
curl -X POST localhost:8080/api/kill-switch -d '{"action":"arm","reason":"rollback"}'

# 2. SIGINT the process (SAFE-05 graceful shutdown fires)
# (in the terminal running arbiter.main)  Ctrl-C

# 3. Flip DRY_RUN back to true
sed -i 's/^DRY_RUN=false/DRY_RUN=true/' .env

# 4. Restart without --live
python -m arbiter.main

# 5. Verify dry-run mode active
curl localhost:8080/api/readiness | jq '.mode'  # → "dry-run"
```

This procedure must be in the runbook and tested before first live trade.

## Evidence & Post-Trade Recording

_Mirror the Phase 4 `evidence/04/<scenario>_<ts>/` structure at `evidence/05/<scenario>_<ts>/`._

Per live trade, capture:

| Artifact | Source | Purpose |
|----------|--------|---------|
| `run.log.jsonl` | structlog JSON capture (arbiter namespace) | Chronological trace of the trade |
| `opportunity.json` | scanner output at the moment of detection | Pre-trade edge, prices, fee estimates |
| `pre_trade_requote.json` | engine re-quote result | What prices were used at fire time |
| `execution_orders.json` | `SELECT * FROM execution_orders WHERE arb_id=...` | Both legs' full lifecycle |
| `execution_fills.json` | same, `execution_fills` | Actual fill prices + qty |
| `execution_incidents.json` | same, `execution_incidents` | Any warnings/errors during execution |
| `execution_arbs.json` | same, `execution_arbs` | Complete ArbExecution record |
| `safety_events.json` | same, `safety_events` | Any kill-switch triggers during trade |
| `balances_pre.json` | BalanceMonitor snapshot before trade | Starting state |
| `balances_post.json` | BalanceMonitor snapshot after settlement (wait 60s+) | Ending state |
| `kalshi_fills.json` | Kalshi `/portfolio/fills` API response for the arb_id | Platform ground truth |
| `polymarket_trades.json` | `client.get_trades(market=condition_id)` | Platform ground truth |
| `reconciliation.json` | Output of reconcile step — breaches, confirmations | The D-19-analog gate evidence |
| `manual/kalshi_ui_screenshot.png` | Operator screenshot | Human verification |
| `manual/polymarket_ui_screenshot.png` | Operator screenshot | Human verification |
| `manual/operator_notes.md` | Operator free-text observations | Qualitative evidence |

Archive the curated subset into `.planning/phases/05-live-trading/evidence/` for `05-VALIDATION.md` citations.

## Minimum Viable Scope

_The smallest Phase 5 that gets to "first live trade executed successfully." The planner should size the plan(s) around this list._

| Task | Target file(s) | Effort | Required? |
|------|----------------|--------|-----------|
| 1. `PHASE5_MAX_ORDER_USD` adapter hard-lock | `arbiter/execution/adapters/kalshi.py`, `arbiter/execution/adapters/polymarket.py` | 1 hour — copy Phase 4 pattern | YES |
| 2. `.env.production.template` with all required Phase 5 vars | root + update .gitignore | 30 min | YES |
| 3. Preflight-checklist automation | new `arbiter/live/preflight.py` — runs all 15 checklist items, prints a pass/fail table | 2-3 hours | YES |
| 4. Post-trade reconciliation runner | new `arbiter/live/reconcile.py` — wraps `assert_pnl_within_tolerance` + calls `trip_kill` on breach | 2 hours | YES |
| 5. `arbiter/live/` test harness mirroring sandbox | `conftest.py`, `evidence.py`, `test_first_live_trade.py` | 3-4 hours | YES — provides scenario manifest output |
| 6. `scanner.max_position_usd=10` default for Phase 5 + confirm in settings | `arbiter/config/settings.py` OR env override | 30 min | YES |
| 7. Runbook: go-live procedure, abort procedure, rollback procedure | `.planning/phases/05-live-trading/RUNBOOK.md` or `arbiter/live/README.md` | 2 hours | YES |
| 8. Phase 5 validation artifact (`05-VALIDATION.md`) with gate | mirror `04-VALIDATION.md` | 1-2 hours | YES |
| 9. (Optional) Operator approval gate / button | `arbiter/readiness.py` + new API endpoint + dashboard UI | 4-6 hours | NO — can start without this |
| 10. Polymarket April 2026 migration compatibility verification | investigation only | 1-2 hours | YES (preflight check) |

**Total estimated effort:** 1-2 day's work if the simpler "tight cap + kill-switch watching" protocol is chosen. 3-4 days if building the approval UI. **Recommendation: simpler protocol; ship it.**

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Polymarket April 2026 stablecoin migration did NOT break py-clob-client 0.25.x for existing USDC.e wallets | Pitfall 7, Standard Stack | If wrong: first Polymarket leg fails with cryptic signature/collateral error. Blocking. **Preflight step #13 must verify before first trade.** |
| A2 | Kalshi demo credentials pattern (separate RSA key for demo vs prod) also applies to production key path | Pitfall 1, Runtime State Inventory | If production key works for demo too, nothing breaks — this is just "separate credentials" being the robust default. Low risk. |
| A3 | 60s is sufficient grace period for Polygon on-chain settlement during a non-congested window | Reconciliation & Settlement | If Polygon is congested at trade time, reconcile times out falsely and triggers abort. Medium risk; mitigation = extend timeout conditionally on detecting pending tx. |
| A4 | `scanner.max_position_usd=10` with FOK and `PHASE5_MAX_ORDER_USD=10` cap is small enough that even a total-loss scenario is acceptable under "low risk tolerance" | Position Sizing | Losing $20 is within any reasonable definition of acceptable; very low risk. |
| A5 | Existing `BalanceMonitor` poll interval is fast enough to see post-trade balance change within the 60s reconcile window | Reconciliation & Settlement | If balance poll is 5-10min, reconciler might see stale balance. Medium risk; mitigation = trigger explicit balance fetch immediately after `_live_execution`. |
| A6 | `py-clob-client.get_trades(market=...)` returns all recent fills for a given market filterable by order_id | Reconciliation & Settlement | If the filter is by market only (not by order_id), reconcile needs to walk the full market's recent trades — still workable, just different code. Low risk; verify in docs. |
| A7 | Running Phase 5 before OPS-04 (py-clob-client bump to 0.34.x) is safe because Phase 4 validated 0.25.x | Standard Stack | If the 0.25.x behavior diverges from 0.34.x on production endpoints (migrated or not), we could hit an unexpected production-only bug. Low-medium risk; explicit operator acknowledgment recommended. |
| A8 | The simpler "tight cap + kill-switch" supervision protocol is acceptable to the user per "ASAP" timeline constraint | Operator Supervision | If user wants the full approval-UI workflow, our plan undershoots. **Confirm in discuss-phase or early in plan-phase.** |

## Open Questions (RESOLVED)

_All 6 questions resolved during planning iteration-2 revision (2026-04-20). Each decision is LOCKED; see per-question `**Decision:**` blocks below. Decisions implemented in Plans 05-01 / 05-02 where noted._

1. **Should we create a new `arbiter_live` Postgres database, or reuse existing `arbiter` dev DB?**
   - What we know: sandbox uses `arbiter_sandbox`; dev uses `arbiter` (default per settings.py:416).
   - What's unclear: mixing dev + live data in one DB creates audit-trail ambiguity.
   - Recommendation: create `arbiter_live`. Costs almost nothing, preserves cleanliness.
   - **Decision (LOCKED):** Create a new `arbiter_live` database. `.env.production.template` sets `DATABASE_URL=postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_live`. Fixtures in `arbiter/live/fixtures/production_db.py` assert `'arbiter_live' in DATABASE_URL AND 'arbiter_sandbox' NOT in DATABASE_URL AND 'arbiter_dev' NOT in DATABASE_URL`. Operator creates the DB + applies `arbiter/sql/init.sql` during the Plan 05-02 Task 1 checkpoint. Implemented: Plan 05-01.

2. **Which market mapping do we target for the first live trade?**
   - What we know: at least one mapping must have `allow_auto_trade=True`, `resolution_match_status="identical"`, and non-zero confidence.
   - What's unclear: do we have a specific mapping in mind, or should the operator cherry-pick at trade time?
   - Recommendation: operator cherry-picks in dashboard at trade time; no need to hard-code.
   - **Decision (LOCKED):** Operator cherry-picks at trade time from the `iter_confirmed_market_mappings(require_auto_trade=True)` set. Preflight check #14 asserts at least one mapping has `resolution_match_status == "identical"`. Operator may override auto-pick via `PHASE5_TARGET_CANONICAL_ID` env var (`test_first_live_trade.py` reads it). Implemented: Plan 05-01 (preflight), Plan 05-02 (test body).

3. **Do we ship the operator approval UI (Task 9) or the simpler tight-cap protocol?**
   - What we know: CLAUDE.md says "ASAP — even with manual monitoring." That points to simpler.
   - What's unclear: does "operator supervised" in the success criteria require an explicit approval button, or is "human watching the dashboard with kill-switch ready" sufficient?
   - Recommendation: simpler protocol.
   - **Decision (LOCKED, per CLAUDE.md "ASAP" + "Safety > speed"):** Ship the **tight-cap + kill-switch protocol**; NOT an approval UI. The operator-supervision primitive is: (a) PHASE5_MAX_ORDER_USD=$10 adapter hard-lock (Plan 05-01 Task 1), (b) MAX_POSITION_USD=$10 scanner-level belt (Plan 05-01 `.env.production.template`, addresses blocker B-5), (c) operator-present-at-dashboard-with-kill-switch (Plan 05-02 Task 1 checkpoint), (d) 60-second operator-abort window in the test body immediately before `engine.execute` (Plan 05-02 Task 3b, widened from original 10s to 60s per warning W-6 + "Safety > speed"), (e) auto-abort-on-reconcile-breach (Plan 05-02 Task 2 + Task 3a). An approval-per-trade UI is DEFERRED to v2 unless the first live-fire run reveals a gap the operator window cannot close. Implemented: Plans 05-01 + 05-02.

4. **Phase 5 plan structure: single plan or multiple?**
   - What we know: Phase 5 scope is small (~1-2 days of work).
   - What's unclear: whether to break it into 3-4 plans or consolidate into one.
   - Recommendation: 2 plans.
   - **Decision (LOCKED):** 2 plans. Plan 05-01 = scaffolding (adapter hard-lock, `arbiter/live/` harness, preflight, env template, runbook). Plan 05-02 = operator preflight checkpoint + auto-abort primitive + live-fire test + VALIDATION flip. Plan 05-02 Task 3 is SPLIT into Task 3a (auto, scaffold + helpers) and Task 3b (checkpoint:human-verify, live-fire execution) per blocker B-4 resolution. Implemented: current plan structure.

5. **Is the 60s reconcile wait correct for Polymarket on-chain settlement?**
   - What we know: on-chain settlement can take 2-30s in typical conditions, longer when congested.
   - What's unclear: 99th percentile settlement time during normal market hours.
   - Recommendation: start with 60s; monitor.
   - **Decision (LOCKED):** Start at 60s. If the first live-fire run observes a pending tx on Polygonscan at reconcile time, the executor may bump to 120s for the next run AND document the observation in `operator_notes.md`. The 60s is recorded in `test_first_live_trade.py` as a module constant `POLYGON_SETTLEMENT_WAIT_SECONDS = 60.0` so it is a single-source-of-truth value, tunable without retesting. Implemented: Plan 05-02.

6. **Does the `readiness._check_profitability` gate pass in live mode if we have zero live executions?**
   - What we know: `_check_profitability` in `arbiter/readiness.py:216-254` inspects `ProfitabilityValidator.get_snapshot().verdict`. For verdicts `{"blocked", "not_profitable"}` it returns `status="fail"` + `blocking=True`. For `"validated_profitable"` it passes. For ANY OTHER verdict (including `"collecting_evidence"`) it returns `status="warning"` + `blocking=True` — which means **readiness.allow_execution denies every opportunity until enough completed executions lift the validator out of `collecting_evidence`**.
   - What's unclear (NOW RESOLVED): this creates a chicken-and-egg problem — cannot ship the FIRST live trade because readiness blocks until trades exist. Traced and confirmed: `_check_profitability` with zero live executions returns `status="warning", blocking=True`, so `startup_failures()` short-circuits to BLOCK.
   - Options: (a) relax the gate globally, (b) operator-overriding readiness for the first N trades, (c) bootstrap mode.
   - **Decision (LOCKED): Option (c) — bootstrap mode via `PHASE5_BOOTSTRAP_TRADES` env var.** Rationale: (a) globally relaxing `_check_profitability` undermines the existing safety net for later operations; (b) operator-override requires dashboard surgery out of scope. Option (c) scoped narrowly: when `PHASE5_BOOTSTRAP_TRADES` is set to a positive int (e.g., `1`) AND completed-executions count is below that int, `_check_profitability` returns `status="pass"` with `summary="Phase 5 bootstrap: <N> trades remaining"` and `blocking=False`. Once executions >= bootstrap limit, the env var has no effect and normal profitability logic resumes. This is a **temporary, opt-in, env-driven** override that leaves no silent backdoor when the env var is unset (default behavior unchanged).
   - **Implementation (LOCKED):** Plan 05-01 Task 3 (NEW) — add bootstrap-mode logic to `arbiter/readiness.py::_check_profitability`; unit-test that: (i) unset `PHASE5_BOOTSTRAP_TRADES` preserves existing collecting/blocking behavior, (ii) `PHASE5_BOOTSTRAP_TRADES=1` with 0 executions returns pass-not-blocking, (iii) `PHASE5_BOOTSTRAP_TRADES=1` with 1 execution falls through to normal logic. `.env.production.template` sets `PHASE5_BOOTSTRAP_TRADES=1` (Plan 05-01 Task 2 edit). Preflight check #16 (new) asserts `PHASE5_BOOTSTRAP_TRADES` is either unset OR an int between 1 and 5 (belt: block mis-scoped overrides like 1000). Implemented: Plan 05-01 (new Task 3 + template + preflight edit), Plan 05-02 (clears the env var in operator runbook ROLLBACK procedure after first trade completes).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12 | Arbiter runtime | ✓ (project standard) | 3.12.x | — |
| `py-clob-client` | Polymarket adapter | ✓ (installed via requirements.txt) | 0.25.x (assumed) | None — required |
| Redis 7 | Quote cache | ✓ (docker-compose) | 7-alpine | In-memory (degraded, for dev only) |
| PostgreSQL 16 | ExecutionStore + safety_events + recon | ✓ (docker-compose) | 16-alpine | None — required for live mode |
| Kalshi API (prod) | Kalshi adapter | **UNKNOWN** — depends on operator credentials | n/a | None — BLOCKS |
| Polymarket CLOB (prod) | Polymarket adapter | **UNKNOWN** — depends on operator wallet funding | n/a | None — BLOCKS |
| Telegram bot | Alerting | ✓ (if configured in Phase 3) | n/a | Warning only — non-blocking per readiness |
| Polygon RPC | Polymarket settlement | ✓ (via py-clob-client built-in) | n/a | None — required for Polymarket |

**Missing dependencies with no fallback:**
- Kalshi production credentials + funded account (operator provisions)
- Polymarket production wallet + USDC funding (operator provisions)

**Missing dependencies with fallback:**
- Telegram (warning only — system still runs, just without push alerts)

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 5.0+ + pytest-asyncio (project standard per `conftest.py` + Phase 4 harness) |
| Config file | `pytest.ini` (or pyproject.toml); Phase 4 registered `@pytest.mark.live` marker |
| Quick run command | `pytest arbiter/live/ -v` (non-live stubs / scaffolding) |
| Full suite command | `pytest -m live --live arbiter/live/ -v` (requires `.env.production` + operator presence) |
| Phase gate command | `pytest -m live --live arbiter/live/test_first_live_trade.py` + aggregator update of `05-VALIDATION.md` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TEST-05 | First live arbitrage executes successfully | integration + live | `pytest -m live --live arbiter/live/test_first_live_trade.py -v` | ❌ Wave 0 — NEW |
| TEST-05 | Reconcile helper enforces ±$0.01 on recorded vs platform fee/PnL | unit | `pytest arbiter/live/test_reconcile.py -v` | ❌ Wave 0 — NEW (reuse sandbox/reconcile.py functions) |
| TEST-05 | PHASE5_MAX_ORDER_USD hard-lock rejects oversize orders on both adapters | unit | `pytest arbiter/execution/adapters/test_phase5_hardlock.py -v` | ❌ Wave 0 — NEW (copy test_polymarket_phase4_hardlock.py pattern) |
| TEST-05 | Preflight checklist runs all 15 checks | integration | `pytest arbiter/live/test_preflight.py -v` | ❌ Wave 0 — NEW |
| TEST-05 | Auto-abort fires on reconcile breach | integration | `pytest arbiter/live/test_auto_abort.py -v` | ❌ Wave 0 — NEW (patches adapter get_trades to return mismatched fee) |
| TEST-05 (manual) | Operator manually confirms first trade on both platform web UIs | manual-only | n/a — operator screenshots | n/a |

### Sampling Rate
- **Per task commit:** `pytest arbiter/live/test_reconcile.py arbiter/live/test_preflight.py arbiter/execution/adapters/test_phase5_hardlock.py -v` (fast unit tests, <10s)
- **Per wave merge:** `pytest arbiter/ -v --tb=short` (full non-live suite)
- **Phase gate:** `pytest -m live --live arbiter/live/test_first_live_trade.py -v` + manual-confirmation artifacts in `evidence/05/`

### Wave 0 Gaps
- [ ] `arbiter/live/__init__.py`
- [ ] `arbiter/live/conftest.py` — guard-rail fixtures (assert `PHASE5_MAX_ORDER_USD` set + ≤10; assert `DATABASE_URL` includes "live")
- [ ] `arbiter/live/evidence.py` — mirror of sandbox/evidence.py
- [ ] `arbiter/live/reconcile.py` — wraps `arbiter.sandbox.reconcile` + adds `reconcile_post_trade` runner
- [ ] `arbiter/live/preflight.py` — automates the 15-item checklist
- [ ] `arbiter/live/test_reconcile.py` — unit tests for post-trade reconcile helpers
- [ ] `arbiter/live/test_preflight.py` — unit tests for preflight automation
- [ ] `arbiter/live/test_auto_abort.py` — mocked-adapter test of auto-abort on reconcile breach
- [ ] `arbiter/live/test_first_live_trade.py` — the live-fire scenario (skipped without `--live`)
- [ ] `arbiter/execution/adapters/test_phase5_hardlock.py` — mirror of `test_polymarket_phase4_hardlock.py`, covers both adapters
- [ ] `.env.production.template` — env-var scaffold for live mode
- [ ] `arbiter/live/README.md` — operator runbook (go-live + abort + rollback procedures)
- [ ] `.planning/phases/05-live-trading/05-VALIDATION.md` — phase gate artifact (`pending_live_fire` initial state mirroring Phase 4's)

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Existing Kalshi RSA signing (cryptography lib); existing Polymarket EIP-712 via py-clob-client. Phase 5 adds NOTHING — reuses phases 1-3. |
| V3 Session Management | yes | Dashboard HMAC-SHA256 session tokens (arbiter/api.py). Already operational. |
| V4 Access Control | yes | `readiness.allow_execution` + `safety.allow_execution` as the gate chain. Phase 5 extends, never loosens. |
| V5 Input Validation | yes | Existing `ArbiterConfig` validation + `_clamp_probability` in fee functions + adapter-level sanity checks. Phase 5 adds `PHASE5_MAX_ORDER_USD` numeric parsing with safe default (unparseable → 0.0 = reject all, mirroring Phase 4 pattern). |
| V6 Cryptography | yes | Never hand-roll: use `cryptography` for RSA (Kalshi), `py-clob-client` for EIP-712 (Polymarket). Phase 5 touches no crypto code. |
| V7 Error Handling and Logging | yes | structlog JSON + OPS-01 emitting arb_id + canonical_id on every line. Phase 5 adds `phase5.*` log namespaces (reconcile, abort, approval). |
| V8 Data Protection | yes | `.env` gitignored, `./keys/` gitignored, Postgres credentials in-env. Phase 5 adds `.env.production` which must ALSO be gitignored (verify). |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Accidental commit of production private keys | Information Disclosure | `.gitignore` covers `.env*` + `keys/`; `git secrets` pre-commit hook recommended but not enforced |
| Adapter gets a reference that bypasses `PHASE5_MAX_ORDER_USD` | Elevation of Privilege | Check lives at top of `place_fok` / `place_resting_limit` — every code path must go through these methods |
| Runaway execution loop (scanner producing opportunities faster than kill-switch can arm) | Denial of Service (self-inflicted) | Per-platform rate limiter (SAFE-04) throttles adapter calls; kill-switch has 5s cancel budget; `trip_kill` is idempotent under `_state_lock` |
| Telegram bot token leaked via logs | Information Disclosure | Loggers redact known secret-key patterns (check current logger config — may be a gap); rotate token quarterly |
| Operator approves a malicious opportunity (pasted arb_id in UI) | Spoofing | Approval gate must verify arb_id is a current `tradable` opportunity, not an arbitrary string |
| Replay attack on signed Polymarket order | Tampering | py-clob-client handles nonce + expiry; verify `expires_at` is set on order objects — not hand-rolled |
| Kalshi API key stolen from disk | Credential Theft | OS-level file permissions on `keys/kalshi_private.pem` (chmod 600); encrypt disk at rest (out of scope for v1) |
| Reconciliation step itself fails silently | Tampering (self) | If `reconcile_post_trade` raises an unexpected exception, default to abort (fail-closed). Log + alert. |

## Sources

### Primary (HIGH confidence)
- `arbiter/readiness.py` — `OperationalReadiness.startup_failures`, `allow_execution`, 8 blocking checks. `[VERIFIED this session]`
- `arbiter/main.py` — `--live` flag handling (line 583), chained_gate composition (line 285), graceful shutdown sequence. `[VERIFIED this session]`
- `arbiter/execution/engine.py` — `_live_execution` (line 769), `RiskManager` (line 202), dry_run branch (line 467). `[VERIFIED this session]`
- `arbiter/execution/adapters/polymarket.py` + `kalshi.py` — `PHASE4_MAX_ORDER_USD` hard-lock pattern (lines 96-115 / 291-311). `[VERIFIED this session]`
- `arbiter/safety/supervisor.py` — `trip_kill`, `reset_kill`, `prepare_shutdown`, `allow_execution`. `[VERIFIED this session]`
- `arbiter/sandbox/reconcile.py` — ±$0.01 reconciliation helpers. `[VERIFIED this session]`
- `arbiter/sandbox/README.md` — Operator runbook for Phase 4 live-fire (Phase 5 template). `[VERIFIED this session]`
- `.planning/phases/04-sandbox-validation/04-VALIDATION.md` — D-19 gate contract + expected 9 scenarios. `[VERIFIED this session]`
- `.planning/phases/04-sandbox-validation/04-REVIEW.md` — Phase 4 code-review with 5 warnings (all advisory). `[VERIFIED this session]`
- `.planning/REQUIREMENTS.md` — TEST-05 definition + v2 deferrals. `[VERIFIED this session]`
- `.planning/PROJECT.md` + `CLAUDE.md` — user constraints (<$1K, low risk, ASAP, Kalshi+Polymarket only). `[VERIFIED this session]`

### Secondary (MEDIUM confidence)
- [Kalshi Fee Schedule Feb 2026](https://kalshi.com/docs/kalshi-fee-schedule.pdf) — quadratic formula + ceil-to-cent rounding. Verified via multiple web sources.
- [Polymarket CLOB Introduction](https://docs.polymarket.com/developers/CLOB/introduction) — hybrid off-chain match + on-chain Polygon settlement.
- [Polymarket April 2026 Upgrade — Blockhead](https://www.blockhead.co/2026/04/07/polymarket-overhauls-exchange-stack-with-new-contracts-order-book-collateral-token/) — CTF Exchange V2, new USD stablecoin, EIP-1271 support.
- [Polymarket USD launch — PaymentExpert](https://paymentexpert.com/2026/04/08/polymarket-to-launch-new-stablecoin/) — migration timeline (April 6, 2-3 week rollout).
- [py-clob-client on GitHub](https://github.com/Polymarket/py-clob-client) — canonical Polymarket Python client.
- [Polymarket Withdraw Docs](https://docs.polymarket.com/trading/bridge/withdraw) — Polygon withdrawals <$0.01 gas; Ethereum bridging $5-$20.
- [Prediction Market Arbitrage Guide — polyguana.com](https://polyguana.com/learn/polymarket-arbitrage) — leg risk, position sizing, first-trade best practices.
- [Cross-Market Arbitrage — quantvps.com](https://www.quantvps.com/blog/cross-market-arbitrage-polymarket) — execute-less-liquid-leg-first pattern.
- [Unravelling the Probabilistic Forest (arXiv 2508.03474)](https://arxiv.org/abs/2508.03474) — academic evidence of Polymarket arbitrage profits.
- [Maker/Taker Math on Kalshi — whirligigbear.substack.com](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi) — taker 7%, maker 1.75%, quadratic P(1-P) per contract.

### Tertiary (LOW confidence — flagged for validation in plan-phase)
- [Trading System Kill Switch — NYIF](https://www.nyif.com/articles/trading-system-kill-switch-panacea-or-pandoras-box) — general industry framing of kill-switches, not project-specific.
- [Launch Checklist — Terms.Law](https://terms.law/Trading-Legal/guides/algo-trading-launch-checklist.html) — algo-trading launch hygiene; used only as a pattern reference.
- [FIA Automated Trading Risk Controls](https://www.fia.org/sites/default/files/2024-07/FIA_WP_AUTOMATED%20TRADING%20RISK%20CONTROLS_FINAL_0.pdf) — pre-trade controls, position limits, kill-switch design patterns. General; not prediction-market-specific.

## Metadata

**Confidence breakdown:**
- **Current codebase state (phases 1-4 delivered):** HIGH — verified via direct read of engine, readiness, supervisor, adapters, sandbox/reconcile in this session.
- **Minimum viable scope + task shape:** HIGH — Phase 4's pattern is directly portable.
- **Polymarket April 2026 migration impact:** MEDIUM — multiple credible news sources confirm the migration; actual production impact on our pinned py-clob-client version not tested in this session (Assumption A1).
- **Operator supervision protocol:** MEDIUM — industry patterns inform recommendation; exact workflow is a Phase 5 design choice.
- **Position sizing:** HIGH — multiple independent sources agree on "start small, 0.5% book-impact ceiling, 10 small trades > 1 large."
- **Reconciliation timing:** MEDIUM — Polygon block times are documented; worst-case tail during congestion is Assumption A3.
- **Abort criteria thresholds:** MEDIUM-HIGH — ±$0.01 comes from Phase 4 D-17; other thresholds (60s leg-risk, 3x429 in 60s) are defensible defaults but not independently validated.

**Research date:** 2026-04-20
**Valid until:** 2026-05-20 (30 days for stable recommendations; 7 days for the Polymarket April 2026 migration-status specific guidance — recheck at plan-phase time).
