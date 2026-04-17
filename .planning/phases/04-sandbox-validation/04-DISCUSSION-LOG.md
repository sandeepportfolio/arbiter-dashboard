# Phase 4: Sandbox Validation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in `04-CONTEXT.md` — this log preserves the alternatives considered.

**Date:** 2026-04-17
**Phase:** 04-sandbox-validation
**Areas discussed:** Environment & credential isolation, Scenario coverage, Test harness & artifacts, Reconciliation methodology

---

## Environment & credential isolation

### Kalshi demo vs production runtime selection

| Option | Description | Selected |
|--------|-------------|----------|
| Env var override (`KALSHI_BASE_URL`) | Add env var; default stays prod; `.env.sandbox` sets demo URL. Minimal change to `arbiter/config/settings.py:365`. | ✓ |
| `ARBITER_ENV=sandbox` flag | Single env var drives both Kalshi URL and separate demo key/RSA path. More cohesive, more surface area. | |
| Separate sandbox config profile | `.env.sandbox.template` + config profile loader. Heavier scaffolding. | |

**Choice:** Env var override.
**Notes:** User preferred the minimal-surface approach.

### Polymarket real-$1–5 containment (no sandbox exists)

| Option | Description | Selected |
|--------|-------------|----------|
| Dedicated test wallet, hard-capped balance | Separate `POLY_PRIVATE_KEY`, ~$10 USDC cap, blast radius = $10. | ✓ |
| Config hard-lock max notional ≤ $5/order | `PHASE4_MAX_ORDER_USD=5` guard at adapter layer. Belt-and-suspenders with SAFE-02. | ✓ |
| Manual confirmation prompt per submit | Operator types Y/N before each live POST. Slow; zero accidental spend. | |
| Dry-run shadow-log Polymarket | Skip real orders entirely. Fails TEST-02 acceptance. | |

**Choice:** Dedicated test wallet + config hard-lock (both).
**Notes:** Defense in depth; neither alone is sufficient for "safety > speed."

### Sandbox state location

| Option | Description | Selected |
|--------|-------------|----------|
| Separate Postgres database | `arbiter_sandbox` DB; prod `execution_*` tables never polluted. | ✓ |
| Same DB with `environment` column tag | Lighter infra; risks mixed prod/sandbox P&L math. | |
| Separate schema inside same DB | `arbiter.sandbox.*` vs `arbiter.*`. Middle ground. | |

**Choice:** Separate database.
**Notes:** Cleanest lineage; cheap to add in `docker-compose.yml`.

### Sandbox credentials bootstrap

| Option | Description | Selected |
|--------|-------------|----------|
| `.env.sandbox.template` + README | Operator fills template, reads docs, runs. Zero magic. | ✓ |
| Interactive `scripts/setup_sandbox.py` | Walks operator through credentials, verifies connectivity. More code. | |
| Manual, no tooling | Plan doc lists vars; operator edits `.env` directly. No guardrails. | |

**Choice:** `.env.sandbox.template` + README.
**Notes:** Interactive setup deferred; can revisit if credential setup becomes recurring friction.

---

## Scenario coverage

### Minimum scenario set (happy path + invariants)

| Option | Description | Selected |
|--------|-------------|----------|
| Kalshi demo happy path (submit → fill → record) | TEST-01 core. | ✓ |
| Polymarket real-$1 happy path | TEST-02 core. Unavoidable ~$1–5 per run. | ✓ |
| FOK rejection on thin-liquidity market (both platforms) | Proves EXEC-01 no-partial-fills against real API. | ✓ |
| Execution timeout + cancel (Kalshi demo) | EXEC-05 / CR-01 invariant live. | (added on recheck) |

**Choice:** First three selected initially; timeout + cancel added after recheck.
**Notes:** The CR-01 live check was the one unverified safety invariant; user agreed it belongs in Phase 4.

### Safety-layer live-fire scenarios

| Option | Description | Selected |
|--------|-------------|----------|
| Kill-switch trip cancels live open orders (SAFE-01) | Fires `/api/kill-switch` during open demo order. | ✓ |
| Graceful shutdown cancels open orders (SAFE-05) | SIGINT/SIGTERM during active order. | ✓ |
| Per-adapter rate-limit backoff under burst (SAFE-04) | Fire N calls, observe tenacity retry-after + WS event. | ✓ |
| One-leg exposure alert path (SAFE-03) | Fake second-leg failure; verify WS event + Telegram. | ✓ |

**Choice:** All four.
**Notes:** Broad coverage matches "cannot afford to lose capital to bugs" stance.

### Cross-platform arb scope in Phase 4

| Option | Description | Selected |
|--------|-------------|----------|
| Single-platform lifecycles only | Matches ROADMAP text; Phase 5 is the explicit go-live gate. | ✓ |
| Kalshi-demo + Polymarket-$1 paper arb | Tests concurrent-legs code path live; bleeds into Phase 5 scope. | |
| Simulated cross-platform on Kalshi demo alone | No real cross-venue proof. | |

**Choice:** Single-platform only.
**Notes:** Deliberate scope discipline; keeps Phase 5 as a clean gate.

### Failure-scenario trigger approach

| Option | Description | Selected |
|--------|-------------|----------|
| Mix: real where possible, injected where impractical | FOK/timeout/kill-switch/shutdown real; one-leg/rate-limit injected. | ✓ |
| Only real conditions | Smaller scope; defers one-leg + rate-limit live to later. | |
| Full fault injection harness | Most rigorous; significant new Phase 4 surface. | |

**Choice:** Mix.
**Notes:** Each scenario tagged `real` or `injected` in `04-VALIDATION.md`.

---

## Test harness & artifacts

### Primary harness form

| Option | Description | Selected |
|--------|-------------|----------|
| pytest suite with `@pytest.mark.live` | Opt-in via `pytest -m live`. Matches existing project test layout. | ✓ |
| `scripts/sandbox_validate.py` runbook + prompts | Interactive; operator-friendly; worse regression story. | |
| Hybrid pytest + runbook | Split between automatable and human-observed. | |

**Choice:** pytest-live.
**Notes:** Primary harness is pytest; human-observation scenarios reuse the Phase 3 UAT-style checklist only as supplement.

### Acceptance artifact format

| Option | Description | Selected |
|--------|-------------|----------|
| `04-VALIDATION.md` per-scenario pass/fail + evidence links | Structured markdown matching Phase 3's `03-VERIFICATION.md`. | ✓ |
| pytest-html report + raw log archive | Machine-readable; less narrative. | |
| Both (narrative + pytest artifacts subdir) | Heaviest; most auditable. | |

**Choice:** `04-VALIDATION.md`.
**Notes:** Single source of truth for phase completion.

### Raw API response capture strategy

| Option | Description | Selected |
|--------|-------------|----------|
| structlog run + DB rows + balance snapshot JSON | Reuses existing Phase 2 infra; stored under `evidence/04/<scenario>/`. | ✓ |
| Dedicated response recorder middleware | More granular; touches adapter code. | |
| VCR-style cassette replay | Incompatible with live-only phase. | |

**Choice:** structlog + DB rows + balances.
**Notes:** No adapter-level HTTP recorder.

### Harness code location

| Option | Description | Selected |
|--------|-------------|----------|
| `arbiter/sandbox/` module | Alongside `arbiter/safety/`, `arbiter/audit/`. | ✓ |
| `scripts/sandbox/` outside package | Cleaner separation; loses pytest discovery. | |
| `tests/integration/live/` | Standard Python convention; breaks existing co-located pattern. | |

**Choice:** `arbiter/sandbox/`.
**Notes:** Consistency with existing layout wins.

---

## Reconciliation methodology (TEST-03, TEST-04)

### Balance snapshot strategy for PnL reconciliation

| Option | Description | Selected |
|--------|-------------|----------|
| Pre+post via existing `balance.py` + in-memory diff | Reuses `BalanceMonitor.fetch_balance()`; minimum new surface. | ✓ |
| Polling timeline (every Ns) | Full time-series; overkill for discrete order lifecycles. | |
| Platform settlement webhook/event listener | Most accurate; requires per-platform event discovery. | |

**Choice:** Pre+post via balance.py.
**Notes:** `evidence/04/<scenario>/balances.json` persists both snapshots.

### PnL tolerance threshold

| Option | Description | Selected |
|--------|-------------|----------|
| ±1¢ absolute | Sub-cent is pure rounding; any ≥2¢ is a real bug. | ✓ |
| ±0.5% of notional | Scales with order size; inconsistent at $1 Polymarket. | |
| Hybrid `max(1c, 0.1%)` | More correct at scale; more code to explain. | |

**Choice:** ±1¢ absolute.
**Notes:** Simple, defensible, fails loudly.

### Fee verification methodology

| Option | Description | Selected |
|--------|-------------|----------|
| Parse platform response fee + compare to `arbiter.config.settings` | Kalshi `realized_fee`, Polymarket CLOB `fee` field; compared against fee functions. | ✓ |
| Balance-delta inference | Derives fee from balance diff; contaminates PnL signal. | |
| Both (response-field primary, balance-delta cross-check) | Strongest; more harness code. | |

**Choice:** Response-field parsing + comparison to existing fee functions.
**Notes:** Any discrepancy logged as structured `ExecutionIncident`.

### Tolerance breach gating

| Option | Description | Selected |
|--------|-------------|----------|
| Hard gate | Phase 4 fails if any scenario exceeds tolerance. Blocks Phase 5. | ✓ |
| Soft flag | Logged + operator ack allowed. Risks proceeding with unexplained gap. | |
| Tiered (pass / explained soft / unexplained hard) | Most nuanced; hardest to enforce consistently. | |

**Choice:** Hard gate.
**Notes:** Aligns with PROJECT.md "cannot afford to lose capital to bugs."

---

## Claude's Discretion

Captured as `D-**` entries in `04-CONTEXT.md` §Claude's Discretion. Summary:
- pytest fixture internals for sandbox DB bootstrap/teardown
- Thin-liquidity market selection for FOK rejection
- `ExecutionIncident` payload shape for fee-discrepancy logs
- `evidence/04/<scenario>/` directory schema
- Fault injection implementation mechanics (asyncio mock + `unittest.mock.patch`)
- Aggressive-limit price strategy for timeout-cancel scenario
- docker-compose layout for sandbox Postgres database

## Deferred Ideas

Captured as §Deferred in `04-CONTEXT.md`. Summary:
- Cross-platform arb execution → Phase 5
- Simulated cross-platform arb on Kalshi demo → not adopted
- Adapter-level HTTP response recorder → not adopted
- VCR cassette replay → incompatible with live phase
- pytest-html CI gating → v2
- Interactive sandbox setup script → revisit if friction emerges
- Polling-every-Ns balance timeline → overkill for discrete lifecycles
- Tiered reconciliation gating → rejected in favor of hard gate
