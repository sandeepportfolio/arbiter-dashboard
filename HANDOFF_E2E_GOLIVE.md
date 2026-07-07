# Arbiter end-to-end go-live — HANDOFF PROMPT (2026-07-06, session 2)

## Latest status — 2026-07-06 session 3 / post-rebuild validation

**Branch:** `fix/forecastex-live-execution`

**New commits added after the older session-2 handoff:**

- `8612c33 fix(readiness): scope live gates by venue pair`
- `da9a79e feat(discovery): expose continuous loop status`
- `41330f9 fix(kalshi): use v2 event order endpoints`
- `454f2d4 chore(gitignore): ignore production env backups`

**What changed:**

- Readiness/trade gating is now venue-scoped for collector degradation: ForecastEx collector degradation blocks ForecastEx-legged opportunities but does not by itself block Kalshi↔Polymarket. Kalshi or Polymarket degradation still blocks K↔P. Legacy `/api/readiness.ready_for_live_trading` remains conservative/backward-compatible.
- `/api/discovery/status` now exposes the continuous loop: `continuous.enabled`, `continuous.last_written`, `continuous.candidates_pending`, `continuous.last_pass_at`.
- Runtime operator settings persist through `./.arbiter-runtime:/root/.arbiter`; `.env.production` has explicit `AUTO_DISCOVERY_ENABLED=true` but remains gitignored.
- Kalshi execution adapter now uses V2 event-order endpoints:
  - create/sign path: `/trade-api/v2/portfolio/events/orders`
  - cancel/sign path: `/trade-api/v2/portfolio/events/orders/{order_id}`
  - request body uses `side=bid|ask`, `count`, `price`, explicit `time_in_force`, `self_trade_prevention_type=taker_at_cross`; no legacy `action`, `count_fp`, `yes_price_dollars`, or `no_price_dollars` request fields.
  - Critical economics: V2 quotes a single YES book, so NO-side wire prices are inverted (`NO @ p` → YES-book price `1-p`) and V2 `average_fill_price` is inverted back for Arbiter NO-side fills.

**Validation already run after the Kalshi V2 fix:**

- `python3 -m pytest arbiter/execution/adapters/test_kalshi_adapter.py arbiter/execution/adapters/test_kalshi_place_resting_limit.py arbiter/execution/adapters/test_kalshi_list_open_orders_signing.py -q` → `80 passed in 3.22s`
- `python3 -m pytest arbiter/execution arbiter/mapping arbiter/recovery arbiter/test_readiness.py arbiter/test_api_integration.py arbiter/test_main_discovery_loop.py arbiter/test_operator_settings.py tests/test_mapping_validation.py tests/test_safety_guards.py -q` → `1007 passed, 2 skipped in 87.82s`
- `git diff --check` → exit `0`
- `npm run typecheck` → exit `0`
- `npm test` → `6 files / 53 tests passed`
- Compose config check → `compose_config_ok` with expected warnings that dormant `IBKR_USERNAME` / `IBKR_PASSWORD` are unset.

**Deployment/validation already run:**

- Rebuilt/recreated `arbiter-api-prod` from committed branch tree; image observed as `db3ef8fcb13b`.
- `/api/balances`: Kalshi `$354.08`, Polymarket `$347.30`, ForecastEx `$301.23`; all non-low, non-stale.
- `/api/discovery/status`: `continuous.enabled=true`, `last_written=42`, `candidates_pending=208`, `last_pass_at=1783382693.290891` after warmup.
- `/api/opportunities`: 2 opportunities appeared after warmup, both ForecastEx↔Kalshi Senate mappings. No K↔P opportunity materialized during this validation window.
- `/api/portfolio/summary`: `captured_arb_count=1`, `stranded_count=9`, `engine_exposure=0.47`, `open_positions=9`, `realized_pnl=137.4295`.
- `/api/safety/status`: `armed=true`, `armed_by=system:redis_restore`, reason says the kill switch was restored from a prior `ExecutionStore.upsert_order write failed` (`execution_engine:db_failure`).
- Logs: `auto_executor.skip.armed` for the observed GOP/DEM Senate opportunities; `auto_executor` considered 2, executed 0, skipped_armed 2.

**Current blockers — do not claim go-live complete:**

1. `/api/readiness.ready_for_live_trading=false` because:
   - `Profitability verdict is blocked`
   - `3 critical incidents remain unresolved`
2. Kill switch is armed from Redis and should **not** be reset without explicit operator authorization.
3. No real two-leg arb completed in this session; `captured_arb_count` remained `1`.
4. K↔P independence is proven by tests, but not by live execution/API evidence because no K↔P live opportunity appeared and the kill switch prevented execution attempts.

**Next safe step:** reconcile/decide on `ARB-000919` and the persisted kill-switch state. Only after an explicit operator decision to reset the kill switch should live execution resume. Then rerun readiness, opportunities, portfolio summary, and either prove a real K↔P execution or run a controlled FX-degradation drill.

---

You are continuing the Arbiter cross-venue arbitrage go-live (Kalshi + Polymarket +
ForecastEx/IBKR) to FULLY WORKING, end to end, autonomously. A prior session completed
Phases 0/1/3 of `.arbiter-scheduled-state/E2E_GOLIVE_PROMPT.md` (read it for the original
contract). This document is the authoritative state + remaining work. The human's single
IBKR login has ALREADY been done — do not ask them for anything unless the IBKR SSO
hard-expires (see §2.3).

---

## 1. Working location and branch

- **Directory:** `/Users/rentamac/Documents/arbiter`
- **Branch:** `fix/forecastex-live-execution`, HEAD `5db0ffe`, working tree clean,
  **9 commits ahead of `main` (`7a3af51`)**. The 4 commits added on 2026-07-06:
  - `16cba5f` fix(api): case-insensitive ops login + guaranteed operator credential
    (this was the previously-uncommitted api.py diff — deliberately committed)
  - `a78f49b` feat(ibkr): CP session keepalive supervisor + trade-API research notes
  - `0087fcd` fix(forecastex): parse cum_fill/average_price + cross-side exit via
    sibling BUY (P0 x2)
  - `5db0ffe` feat(engine): secondary requote-then-retry + verify-before-unwind (P0/P1)
- Push state unverified — check `git log origin/fix/forecastex-live-execution..HEAD`;
  push the branch when convenient (pushing does NOT deploy).
- **Production is LOCAL Docker on this machine** — `arbiter-api-prod` serves
  `http://localhost:8080` and is still running the **8-day-old image: NONE of the 4 new
  commits are deployed yet.** Deploy procedure (env is injected at container CREATION,
  `docker restart` does NOT re-read it):
  ```bash
  cd /Users/rentamac/Documents/arbiter
  docker compose -f docker-compose.prod.yml --env-file .env.production build arbiter-api-prod
  docker compose -f docker-compose.prod.yml --env-file .env.production up -d arbiter-api-prod
  ```
  `Dockerfile` does `COPY . .` — the working tree ships as-is; keep it clean before building.
- Untracked junk in the tree (`.env.production.bak.*`, `ops-*.png`, `arbiter.db`,
  `premium-stays.html`, `signal-filters-live.png`) — leave it; do not commit it.

## 2. Infrastructure state (verified live 2026-07-06 ~14:05 PT)

### 2.1 What is UP right now
- `arbiter-api-prod` (8080, healthy, OLD code), `arbiter-postgres`, `arbiter-postgres-prod`,
  `arbiter-redis-prod` — all healthy.
- LLM mapping-verifier sidecar `http://localhost:8079/health` → ok (must stay up).
- **IBKR Client Portal gateway UP + AUTHENTICATED**: java process from
  `~/ibkr-gateway/start.sh`, serving **https**://localhost:5000 (self-signed; from Docker:
  `https://host.docker.internal:5000`). Brokerage session established for live account
  U25953084 (`allowEventContract:true`).
- **Session keepalive running**: LaunchAgent `com.arbiter.ibkr-keepalive` →
  `~/.arbiter/bin/ibkr_session_keepalive.sh` (repo copy: `scripts/ibkr_session_keepalive.sh`).
  Tickles every 60s, auto-recovers soft expiry via
  `POST /iserver/auth/ssodh/init {"publish":true,"compete":true}`, restarts the gateway if
  :5000 dies, macOS-notifies the human ONLY on hard SSO expiry. Log:
  `~/.arbiter/ibkr-keepalive.log`, state: `~/.arbiter/ibkr-keepalive.state`.
- Live API: `readiness.ready_for_live_trading: true`, blocking `[]`. Balances all fresh:
  kalshi $354.08, polymarket $347.30, forecastex $305.55. FX quotes flowing.

### 2.2 Critical operational warnings
- **DO NOT restart the IBKR gateway process** unless it is actually dead — a restart
  destroys the SSO session and forces a human browser login. The keepalive owns recovery.
- **IBKR sessions hard-reset daily around midnight America/New_York.** Expect the keepalive
  to flag `HARD_LOGIN_REQUIRED` once/day; the human then logs in at https://localhost:5000.
  If after login `/iserver/auth/status` shows `fail: "Invalid_username_or_password Error
  code =1"` with `connected:true`, that is the stale-DH lockup — fix WITHOUT re-login:
  `curl -sk -X POST https://localhost:5000/v1/api/iserver/auth/ssodh/init -H 'Content-Type: application/json' -d '{"publish":true,"compete":true}'`
  (All gateway POSTs need a body or Akamai returns HTML 411.)
- **Do NOT force-close the 9 held stranded positions** (~$30 exposure) — mitigation engine
  holds them correctly (HOLD_TO_SETTLE / MANUAL_REVIEW). Read `mitigation_decision` on
  `/api/portfolio/positions` before touching anything.
- Long-term zero-login path is `deploy/ib-gateway/RUNBOOK.md` Path A (dedicated bot
  username + IBC docker service, dormant in docker-compose.prod.yml) — requires HUMAN
  account admin (create bot username, enable "Event Contracts – ForecastEx" ON THE BOT
  USERNAME, IB Key). Out of scope unless the human volunteers.

### 2.3 Key references
- `docs/ibkr-trade-api-notes.md` — verified CP Web API auth lifecycle, order placement,
  reply-loop, pacing, ForecastEx event-contract specifics. READ THIS before touching IBKR.
- Memory: `~/.claude/projects/-Users-rentamac-Documents-arbiter/memory/` — esp.
  `ibkr-gateway-auth-recovery.md`, `arbiter-ops-runtime-model.md`,
  `forecastex-live-execution-effort.md`.
- Original mission contract: `.arbiter-scheduled-state/E2E_GOLIVE_PROMPT.md`.

## 3. What was done (with evidence) — do not redo

### Phase 1 — IBKR live (COMPLETE except daily-login caveat)
- Gateway found at `~/ibkr-gateway/`, started, human logged in once, stale-DH lockup
  diagnosed from `~/ibkr-gateway/logs/gw.*.log` and fixed via `ssodh/init compete:true`.
- `forecastex-rest` circuit self-closed (~120s recovery); FX balance + quotes + readiness
  all went green (cited live in session).
- **$0.20 live probe through the engine's adapter path** (orders 249901299/302/303/304):
  - BUY IOC 1 × GOP_HOUSE_2026 YES @ 0.20 → **FILLED** (venue-confirmed).
  - Found P0: adapter `get_order` parsed none of `/iserver/account/order/status/{id}`'s
    real fill fields (`cum_fill`, `average_price`) → FILLED-with-fill_qty=0 → engine
    FILL-02 would read a filled FX leg as "no exposure" (silent naked leg). **FIXED** in
    `0087fcd` with regression tests on the verbatim live payload.
  - Found P0: **ForecastEx venue-cancels every SELL order** (GTC @0.25 and IOC @0.01 vs a
    0.19 bid both accepted-then-cancelled, zero fill). The entire smart-unwind sell path
    was dead on FX. **FIXED** in `0087fcd`: exits now BUY the opposing conid at
    `1 − sell_price` (sell YES at p ≡ buy NO at 1−p); IBKR nets positions (proven live:
    1 YES + 1 NO netted to flat, probe cost ≈ $0.04). Sibling conids resolve via
    contract-info → event-children, cached per adapter; unresolvable → fail closed
    (FAILED order, never a doomed SELL). Fill prices translate back to sell terms in the
    ack AND in every `get_order` re-read (`_inverted_exits` set — in-memory; a process
    restart mid-unwind leaves the re-read untranslated; recovery reconciler owns that window).

### Phase 3 — Execution hardening (COMPLETE, committed `5db0ffe`)
- **Requote-then-retry** in `ExecutionEngine._execute_with_fallbacks`
  (arbiter/execution/engine.py): after a CLEAN secondary failure, re-walk the book up to
  `SECONDARY_REQUOTE_MAX_ATTEMPTS` (default 2) × `SECONDARY_REQUOTE_DELAY_MS` (default
  250ms) and retry an IOC while walked ≤ max_affordable. Final attempt may complete at
  break-even (≥0¢ net beats paying spread+fees to unwind). The old attempt-2 was dead code
  (initial == max_affordable in the live path — prod error strings proved it).
- **EDGE-LOST branch** now enters the same loop (`skip_initial_attempt=True`) instead of
  instantly stranding the primary (~35 naked arbs took the old path). ABORTED leg is
  constructed only after the retry budget exhausts with nothing submitted.
- **Verify-before-unwind** in `_recover_one_leg_risk`: an ambiguous/may-be-live secondary
  (C3 flag, "cancel failed", "MAY be live") is re-read at the venue before selling the
  primary. Proven dead → unwind; late-filled → pair up, unwind only the diff; unprovable →
  unwind BLOCKED + critical `unwind_blocked_ambiguous_secondary` incident, exposure stays
  booked. Closes the reverse-short hole.
- `idempotency_ambiguous` still suppresses ALL retries unconditionally; kill-switch,
  sequential guards, phantom-fill fix (`916e711`), FILL-02 semantics preserved.
- Tests: `arbiter/execution/test_secondary_requote.py` (7),
  `arbiter/execution/test_verify_before_unwind.py` (7, incl. the previously-uncovered
  partial-fill diff-unwind pin), plus E2E
  `test_engine.py::test_live_leg2_clean_reject_triggers_full_unwind_e2e` (reject → real
  unwind → pnl booked → reservation released → incident auto-resolved).
- **Suite state: 842 passed, 2 skipped** (arbiter/execution + arbiter/mapping +
  tests/test_mapping_validation.py + tests/test_safety_guards.py). Baseline was 334+524.

### Phase 2 — premise CORRECTED (partially done, nothing committed)
- **Discovery is ALREADY RUNNING.** `AUTO_DISCOVERY_ENABLED` defaults to TRUE when absent
  (`arbiter/operator_settings.py:54`); prod logs show "Market discovery pass complete:
  wrote=43 pending_candidates=213" every ~5–17 min. **`/api/discovery/status` "idle" is
  misleading** — it only tracks one-shot `POST /api/batch-discover` runs
  (`arbiter/api.py:3181`), not the continuous loop (`arbiter/main.py:886`
  `run_market_discovery_loop`, gate at :904). The loop publishes
  `auto_discovery_last_written` / `auto_discovery_candidates_pending` into `pm_us_metrics`
  (main.py:946-947) which the API can read via `self._pm_us_metrics` (api.py:4093).
- Promotion gates verified safe: fuzzy NEVER auto-confirms
  (`arbiter/mapping/auto_promote.py:157-192` cages semantic matches to status=review,
  allow_auto_trade=False); LLM verifier :8079 fails closed; resolution DIVERGENT/PENDING
  fail closed. Do not touch these.
- **Precedence trap:** once `/root/.arbiter/operator-settings.json` exists (inside the
  container, NOT volume-mounted, lost on recreation), its value OVERRIDES the env var
  (`operator_settings.py:78-80`). Currently no file exists.

## 4. REMAINING WORK (in order)

### 4.1 NEW REQUIREMENT — Kalshi↔Polymarket must trade end-to-end while FX is being fixed
**User directive (2026-07-06): K↔P trades must execute/mitigate end to end even while
ForecastEx is degraded or under repair.** Today that is NOT guaranteed:

- When the IBKR gateway was down, `/api/readiness` returned
  `ready_for_live_trading: false` with blocking reason "Collector health is degraded:
  forecastex" — and the readiness→safety chained trade gate (wired
  `arbiter/main.py:~1163-1173`, enforced `arbiter/execution/engine.py:~688-699`; re-verify
  exact lines) halts **ALL** trading, including K↔P pairs whose venues are perfectly
  healthy. The nightly IBKR midnight-NY reset means FX will degrade briefly EVERY DAY —
  K↔P must not go dark each time.
- **Task:** scope venue-health gating to the pair being traded. A degraded forecastex
  collector must block only opportunities with a forecastex leg; K↔P opportunities
  (both legs on healthy venues) must pass the gate. NEVER loosen the inverse: if kalshi or
  polymarket is degraded, K↔P stays blocked; kill-switch and all other gates unchanged.
  Readiness output should reflect this (e.g. per-venue/per-pair readiness rather than one
  global boolean blocking everything) — but keep `/api/readiness` backward compatible for
  the dashboard.
- **TDD:** failing tests first — (a) unit: trade gate allows a K↔P opp while the
  forecastex collector reports degraded, blocks any FX-leg opp; (b) blocks K↔P when
  kalshi (or polymarket) itself is degraded; (c) e2e through `execute_opportunity`.
- **Mitigation on K↔P already works and is now better-covered**: 156/156 historical
  auto-unwinds filled (+$118 aggregate); requote + verify-before-unwind tests all use
  kalshi/polymarket adapters. Nothing extra needed beyond keeping those suites green.
- **Live proof required:** with the new code deployed, a real K↔P two-leg arb completes —
  `captured_arb_count` increments and `stranded_count` does not grow — OR, if no K↔P
  opportunity materializes during the session, a live FX-degradation drill: stop nothing,
  wait for (or simulate via readiness inputs) forecastex degradation, and show from the
  live API that K↔P opportunities still pass the trade gate (auto_executor attempts them)
  while FX-leg opportunities are rejected with the venue-scoped reason.

### 4.2 Finish Phase 2 (discovery visibility + tests)
1. Add `AUTO_DISCOVERY_ENABLED=true` explicitly to `.env.production` (documents intent;
   absence already means true).
2. Extend `/api/discovery/status` to include the continuous loop:
   `continuous: {enabled, last_written, candidates_pending, last_pass_at}` sourced from
   `pm_us_metrics` + operator settings (add a last-pass timestamp to the metrics dict in
   `run_market_discovery_loop`). Keep the existing batch-run fields untouched. TDD in
   `arbiter/test_api_integration.py` (has GET/POST /api/settings roundtrip patterns).
3. New regression tests (research agent found NO coverage):
   - `arbiter/test_main_discovery_loop.py`: loop skips work + sleeps when
     `auto_discovery_enabled` false; runs a pass and updates metrics when true (follow
     `arbiter/test_main_auto_resolver_loop.py` conventions).
   - `arbiter/test_operator_settings.py`: `AUTO_DISCOVERY_ENABLED` env default-true
     parsing; JSON-file-overrides-env precedence (`operator_settings.py:19-23, 52-108`).
4. Optional (recommended): volume-mount the operator-settings dir in
   docker-compose.prod.yml (e.g. `./.arbiter-runtime:/root/.arbiter`) so dashboard toggles
   survive container recreation; document the file-over-env precedence next to the mount.

### 4.3 Phase 4 — validate, deploy, merge (unchanged from original prompt, plus new gate)
1. Full test run: mapping + safety + execution + recovery + api-integration suites; report
   exact counts (baseline this session: 842 passed, 2 skipped on execution+mapping+safety).
2. Commit remaining work in logical commits on `fix/forecastex-live-execution`; push.
3. Deploy (build + up -d, §1). Then re-run every Phase-0 check from the original prompt.
4. **Live definition of done — show from the live API, never assert:**
   - `/api/readiness` → ready (or venue-scoped equivalent) with no blocking reasons
   - `/api/balances` → all three venues non-null and fresh
   - `/api/discovery/status` → continuous section shows passes + candidate counts growing
   - `/api/opportunities` → opportunities appear as discovery replenishes
   - **A real two-leg arb completes** (`captured_arb_count` > 1 lifetime, no new strand);
     K↔P counts — it does NOT have to be an FX pair
   - **K↔P independence proven** per §4.1
   - Zero human logins beyond the daily IBKR midnight reset (keepalive log shows automated
     soft-expiry recoveries)
5. Only after ALL gates: merge `fix/forecastex-live-execution` → `main`, push. Any gate
   unmet: push branch, leave a precise handoff note in `.arbiter-scheduled-state/` naming
   the unmet gate and what will prove it — never claim it.

### 4.4 Deferred (documented, do not silently drop)
- **Status terminalization:** a fully-unwound arb stays `status="recovering"` forever
  (constructed engine.py:~2122-2131, never updated post-recovery). Effects: auto_executor
  cooldowns treat it as failure (auto_executor.py:552-566 — intended), stuck_trade_recovery
  reconciles later. Fixing requires care vs StrandedReconciler ownership
  (stranded_reconciler.py:755-784) — see session research before attempting.
- Cross-restart `arb_id` collisions (engine.py counter resets per process) corrupt
  DB joins for early ARB-000001..9 rows — key any new persistence off client_order_id.
- `_inverted_exits` restart window (§3 Phase 1 notes).
- IBC bot-username migration (§2.2) — needs human account admin.

## 5. Hard rules (unchanged)
- Safety > speed. Existing engine caps stay; any new live probe ≤ $5 notional.
- Never loosen a safety gate; never auto-confirm fuzzy mappings; fail closed on
  NO/MAYBE/missing data. The K↔P work scopes gating — it must not weaken per-venue checks.
- Never fabricate validation: every "done" cites live API output or test output.
- Update `.arbiter-scheduled-state/mapping_learning_loop.json` with what was learned.
- TDD for all engine/adapter changes (superpowers:test-driven-development).

## 6. Quick verification crib sheet
```bash
cd /Users/rentamac/Documents/arbiter
git status && git log --oneline origin/main..HEAD
docker ps --format '{{.Names}}\t{{.Status}}'
curl -s localhost:8080/api/readiness | python3 -m json.tool | head -20
curl -s localhost:8080/api/balances | python3 -m json.tool | head -30
curl -s localhost:8080/api/discovery/status
curl -s localhost:8080/api/portfolio/summary | python3 -m json.tool
curl -s localhost:8080/api/opportunities | head -c 400
curl -s localhost:8079/health
curl -sk -m 5 -X POST -d '' https://localhost:5000/v1/api/iserver/auth/status   # IBKR
tail -5 ~/.arbiter/ibkr-keepalive.log
python3 -m pytest arbiter/execution arbiter/mapping tests/test_mapping_validation.py tests/test_safety_guards.py -q | tail -2
# expected: 842 passed, 2 skipped (pre-4.1/4.2 work)
docker logs arbiter-api-prod --since 30m 2>&1 | grep "Market discovery pass complete" | tail -3
```
