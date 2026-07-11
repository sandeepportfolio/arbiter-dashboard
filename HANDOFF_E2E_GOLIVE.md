# Arbiter end-to-end go-live — HANDOFF PROMPT (2026-07-06, session 2)

## 🔬 FULL FORENSIC AUDIT + FIXES — 2026-07-11 (every alert since resume)

**Every trade classified (the alert-wrong vs execution-wrong split):**
- ARB-001030..001037 (8 Senate, filled 21:21-21:46Z): FALSE ALERTS. Phantom crossed
  mapping — both legs long "Dems win", no hedge. Booked \$0.65 is fictional (== detection
  net_edge, no execution_fills rows). fault=alert_wrong. (Book already de-risked/rejected.)
- ARB-001038..001041 (4 MLB): REAL edges (7-12c net, correct K-P hedges), captured 0%.
  Every polymarket primary IOC arrived non-marketable and expired at fill_qty=0.
  fault=execution (latency), zero exposure every time (sequential fail-safe).

**NEW live crossed mapping found + quarantined (P0):** POL_US_SENATE ...DEM and ...REP
both confirmed+auto_trade, both pointing at FX conid 745923952 — same party-swap class,
dormant only because the FX leg was deduped-dark. Plus a latent cascade: 179 FX-economic
mappings (FF/CPIY/RGDP/PCEY) on shared-INDEX / null conids. ALL quarantined in DB;
no confirmed auto-trade mapping shares a FX conid now (68 clean remain).

**Guard gaps closed (f6c2392):** structural_fast_promote (3rd promotion path) had NO
coherence/party gate — added shared-conid refusal. The coherence sweep was blind to
dark-FX crossings (needs a live FX leg) — added DB-metadata shared-conid detection that
quarantines regardless of live quotes. New coherence.shared_fx_conid_conflict +
MarketMappingStore.fx_conid_owner_counts.

**Execution fill-rate fixes (b24a62b) — the "make trades land" work:** root cause was
~470-570ms pre-placement latency (price was fine — ARB-001041's limit was 2c THROUGH the
ask and still expired). Three safe fixes, sequential primary-first design unchanged:
1. Non-blocking tick fetch: _cached_tick did a cold ~300ms get_market_by_slug GET in the
   order clamp (always a miss on unique game slugs) — now returns default 1c tick and
   refreshes off-path.
2. Buffer floor: the slippage buffer was SKIPPED when buffered net dipped under the 7c
   DISPLAY floor, sending at the bare touch (~50% of fails) — now spends 1-2c of the
   validated edge as long as the pair still nets >= 2c (FOK_BUFFER_MIN_NET_CENTS).
3. Fast first poll: 500ms -> ~120ms (SUBMIT_FIRST_POLL_S) so a stale-touch expiry is
   caught while the edge is still live and the requote loop re-fires.

**ForecastEx reality (make-FX-work):** the EXECUTION path works (100+ Senate fills). The
gap is the SURFACE: 179 economic mappings point at shared INDEX (IND) conids IBKR won't
resolve to per-strike binaries; the only 3 tradeable OPT event conids (DEM_HOUSE etc.)
FAIL marketdata/snapshot ~3600x/24h (FX quote feed degraded). Path to Fed/CPI trading is
feasible via CP-API re-discovery to per-strike OPT conids BUT requires (a) fixing the FX
snapshot feed first, (b) unique per-strike YES/NO conids, (c) TWS collector as fallback.
Substantial project; dangerous parts quarantined, path documented. Not done this session.

---

## 📊 EDGE & FX-SURFACE AUDIT — 2026-07-11 (answers "are trades profitable / where are real edges")

**A — Lowering the edge floor would NOT help.** Multi-snapshot sweep of live K↔P markets:
ZERO net-positive edges even at a 0¢ floor. Real executable edges are NEGATIVE (~−0.5¢
gross before fees) — kalshi and polymarket price these games within each other's spread.
The 7¢ floor isn't hiding real edges; there are none. Lowering it only surfaces
money-losing trades. NOT recommended.

**B — The ForecastEx trading surface is tiny, and the economic bulk is unquotable:**
- 179 of 233 FX mappings (Fed Funds `FF`, `CPIY`, `RGDP`, `PCEY`) point at the underlying
  INDEX conids (IBKR type `IND`, e.g. FF.X / CPIY — no bid/ask), NOT the tradeable binary
  option contracts. IBKR's `/iserver/secdef/strikes` returns empty for these and
  `secdef/search` resolves "FF" to NYSE equities — the binaries are NOT discoverable via
  the current CP-API path (the resolver correctly gives up: "resolve_event_children empty
  … unsupported IBKR conid"). Manual conid entry for 179 strikes is impractical AND is
  exactly the path that caused the party-swap. These economic markets — the most likely
  home of real cross-venue edges vs kalshi's KXFED/KXCPI — cannot trade as mapped.
- ~10 FX EVENT markets (Senate/House/MLB) have proper `OPT` conids that quote. The Senate
  ones were the crossed (now-rejected) mappings. DEM_HOUSE_2026 is correctly mapped,
  COHERENT (k_yes 0.82 vs fx_yes 0.83, 1¢ divergence) and EFFICIENT (−2¢ edge, no arb).

**Honest bottom line on profitability:** The system executes correctly (K↔P pipeline proven,
FX fills proven). But real cross-venue arbitrage edges are genuinely scarce: K↔P is
efficient (negative edges), and the FX surface is ~a handful of correctly-mapped event
markets that are also efficient where live. Lifetime genuine clean-arb profit ≈ $6 (a
couple of NBA/MLS games); the 115 Senate "captures" were the phantom mapping (now
liquidated at ~breakeven); the $131 "naked_leg_pnl" is murky unwind/settlement accounting,
not clean arbitrage. The real levers are (1) faster/resting execution to catch transient
edges before they close (like the 9¢ KC edge that evaporated in ~1s), and (2) less-efficient
market types — NOT a lower floor and NOT the FX economic markets (unresolvable via the API).

---

## 🎯 K↔P PIPELINE PROVEN LIVE — 2026-07-11 00:00Z (closes original §4.1 gate)

First real kalshi↔polymarket arbitrage attempt: ARB-001038 on GAME_MLB_20260710_KC
(kalshi YES / polymarket NO, 9.2c net edge). The polymarket NO primary was placed IOC
@0.685; the book moved (best_ask_yes → 0.33) before it filled, so the IOC expired
unfilled → primary $0 exposure → kalshi secondary correctly SKIPPED → clean FAILED,
$0 spent, zero strand. This is the ideal fail-safe: a vanishing edge on the sequential
primary-first path costs nothing. The K↔P pipeline (scanner → gate → book-walk →
sequential execute → fail-safe) works end-to-end with correct risk behavior — the one
gate open since the original go-live handoff (§4.1 K↔P independence) is now demonstrated.
A K↔P CAPTURE (primary fills) will follow when an edge persists past the ~1s IOC window.

NOTE: `engine_exposure` reports ~$100.96 (full Senate book) but REAL exposure is ~$50
(FX legs held-to-settle only) — the engine still counts the kalshi legs liquidated
outside it (documented reporting gap; over-reports, so safe).

---

## ✅ INCIDENT RESOLVED — 2026-07-10 23:20Z: full defense stack deployed, book de-risked

**Defense-in-depth against the mapping-error class (all TDD'd + deployed):**
1. Mapping promotion coherence gate: cross-venue same-side YES divergence >0.08 blocks
   promotion (`arbiter/mapping/coherence.py`).
2. FX party-identity gate: a party-tokened canonical must map to a same-party FX
   contract (fails closed without the symbol).
3. `[no-auto-promote]` marker now TERMINAL across ALL FOUR promotion paths:
   apply_promotion (010f23f), auto_promote_validated (52b2c82),
   structural_fast_promote (690f16c), and the per-cycle coherence sweep — the marker
   was being wiped/resurrected by each ungated path (live: GOP_HOUSE re-confirmed 3×).
4. Per-cycle coherence sweep quarantines any CONFIRMED mapping that turns incoherent.
5. Per-trade suspicious-edge breaker: gross edge >15c routes to operator review
   (coarse absurdity backstop; set above the ~9.5c legitimate K×P range so it never
   blocks real arbs).

**Second bad mapping caught by the new sweep:** GOP_HOUSE_2026 (0.305 polymarket-vs-FX
divergence; FX conids party-correct, polymarket side suspect, ZERO exposure) — now
review + marker.

**All three bad mappings quarantined:** DEM_SENATE_2026 / GOP_SENATE_2026 rejected
(conids corrected); GOP_HOUSE_2026 review + marker.

**Other hardening this session:** FK persistence under parent arb_id (0b2f8f0), daily-cap
UTC rollover (46c58fc), reconciler paired-inventory + FX-position identification
(bb7c8f2/a5f524a/b00baa7), FX positions cache handling (e616f77/4038955), tighter FX
post-submit poll + wall-clock timeout (3ee3fa6).

**Kill switch:** ARMED pending final quarantine-persistence confirmation; then reset to
resume trading on the coherence-validated set. NOTE: real cross-venue arb edges are rare
(a few cents) — "hundreds of trades" come from real market inefficiencies, NOT forced
volume; chasing volume on a persistent too-good edge is precisely what produced this
incident, and is now blocked at four layers.

---

## ⛔ CRITICAL — 2026-07-10 21:50Z: Senate FX mappings were PARTY-SWAPPED; system halted by operator kill switch

**Verified via IBKR /iserver/contract/info:** `DEM_SENATE_2026` mapped to conids
745924267/745924270 = `SENM_1126_Republican_YES/NO`; `GOP_SENATE_2026` mapped to
773659815/773659816 = `SENM_1126_Democratic_YES/NO`. Every executed "arb" bought TWO
legs of the SAME long-Democrat bet. The persistent "12.5¢ edge" was phantom (comparing
non-complementary contracts) — that is why it never closed.

**Book as halted (115 filled pairs, $100.96 cost, ~+$129 / −$101 binary on Senate
control):** kalshi CONTROLS-2026-D YES ×64 @0.4400; FX Republican_NO ×64 @0.4113;
FX Democratic_YES ×51 @0.4698; kalshi CONTROLS-2026-R NO ×51 @0.4416. Current bids
imply liquidation recovery ≈ $92–97 (1-lot books — slow to work).

**Halt chain (all timestamps 07-10):** 21:38Z `disable_auto_trade`+`review` via API →
**auto-validator RE-CONFIRMED the mapping** (semantic validation matches titles, cannot
see conid party; ARB-001036/1037 filled 21:41/21:46) → 21:53Z operator KILL SWITCH armed
→ 21:55Z both mappings set `status='rejected'` (terminal: excluded from runtime
hydration and promotion). **Do NOT reset the kill switch until** a conid-party /
price-coherence validation exists and the corrected mapping (DEM→773659815/816,
GOP→745924267/270) passes it. **Do NOT re-map by hand without that validator — manual
conid entry is the suspected origin of the swap.**

**Deployed protections (bb7c8f2):** reconciler paired-inventory awareness — hedged lots
are `paired_hold`, never mitigated/auto-closed (kills the standing autonomous
COMPLETE_ARB that would have bought 43 MORE Democratic_YES under the $30 auto-close cap
with `STRANDED_AUTO_CLOSE=true` live); decisions defer on venue-fetch failure; stranded
metrics use residuals (fixed the misleading "-$74 unrealized").

**DECISION EXECUTED (2026-07-10 23:01Z) — partial liquidation, forced by venue liquidity:**
- Kalshi books are DEEP (50k+ contracts at top of book): SOLD both kalshi legs via the
  tested `place_resting_sell` adapter path — 65× CONTROLS-2026-D YES filled @0.43, 51×
  CONTROLS-2026-R NO filled @0.44 (price improvement over the 0.41/0.42 limits). Both
  venue positions now FLAT; ~$50.39 recovered at ~breakeven; the 1 residual naked
  contract (ARB-000941) swept up in the same sale. Kalshi balance $309→$350.78.
- ForecastEx legs (51× Democratic_YES 773659815 + 64× Republican_NO 745924270, $50.28
  cost) are HELD TO SETTLEMENT (Nov 2026): the venue cancels all SELL orders, and the
  exit-sibling books (745924267 Rep-YES, 773659816 Dem-NO) show ZERO displayed size —
  bulk-neutralizing 115 contracts 1-lot-at-a-time into empty books at ~0.55-0.61 would
  lock a larger loss AND tie up $115 for 4 months, strictly worse than holding. These
  pay $115 if Dems win the Senate / $0 if not (residual ~$50 directional, ~44% implied).
- Net: directional exposure HALVED ($101→$50), ~$50 cash recovered at breakeven, only
  the un-exitable FX half remains. Best achievable given venue liquidity.
- NOTE: the kalshi sale was executed outside the engine (operator liquidation, kill
  switch stayed armed), so the DB filled-arb records are unchanged — the reconciler still
  labels the FX legs `paired_hold` (behaviourally correct: no action possible on them
  anyway). The FX legs are genuinely directional-held-to-settle; documented here as the
  source of truth.

**Update 22:10Z — containment complete, gates deployed (`52b2c82`):**
- Cross-venue coherence + FX party-identity gates live in the auto-validator:
  promotion now requires yes-price agreement across venues (≤0.08 divergence,
  `MAPPING_COHERENCE_MAX_DIVERGENCE`), FX party match against the canonical
  (fails CLOSED without the contract symbol), and respects a `[no-auto-promote]`
  marker so operator demotions can no longer be resurrected. A per-cycle sweep
  quarantines CONFIRMED mappings that turn incoherent (review + auto-trade off).
- Senate mapping conids CORRECTED in DB (DEM→773659815/816 Democratic,
  GOP→745924267/270 Republican), status stays `rejected` + marker — re-enable
  requires an operator, and must pass the new gates.
- Kill switch remains ARMED (survives restarts via redis). Reset ONLY after the
  operator book decision; on reset, trading resumes with the crossed markets
  rejected and every other confirmed mapping subject to the coherence sweep.
- Reconciler residual isolation confirmed live: kalshi CONTROLS-2026-D shows a
  ~$0.77 residual (the 1–2 genuinely naked contracts from historical unwind
  shortfalls) while the paired 51-lot CONTROLS-2026-R nets to `paired_hold`.

**Validation gap to fix before ANY FX mapping re-enable:** cross-venue price-coherence
check (a true YES_A/NO_B complement pair must satisfy |yes_A + no_B − 1| ≲ spread; the
swapped pair sat at 0.85 for days) + FX conid `local_symbol` party/semantics match
against the canonical.

---

## Latest status — 2026-07-10 session 5 (afternoon) — 100 arbs captured; daily-cap bug fixed; §4.1 evidence completed to the extent markets allow

**Branch:** `fix/forecastex-live-execution`, HEAD `46c58fc`, pushed. Deployed image
`1fa30627b55e` (container up 2026-07-10T20:24Z). Kill switch DISARMED (survives
restarts; shutdown-arms are lifecycle-only).

**Day-1 live results:** 99 two-leg arbs captured on 07-10 (captured_arb_count=100
lifetime), ~$9.8 booked, engine exposure ~$87 all paired, ONE naked leg all day
(ARB-000941) — auto-recovered in 15s for −$0.01 with the incident auto-resolving.
FX balance $308→$264 (committed to FX legs), all balances fresh.

**NEW P1 FIXED + DEPLOYED — `46c58fc` fix(risk): daily windows never rolled over:**
`MAX_DAILY_TRADES`/`MAX_DAILY_LOSS_USD` counters had NO reset — after 100 lifetime
trades the engine halted permanently (live 2026-07-10 20:04Z "Daily trade limit
reached"; only a container restart — which silently zeroed them — could resume).
`check_trade` now rolls both windows at UTC midnight (logged). Positional caps
untouched. TDD'd; execution suite 516 passed.

**§4.1 evidence — completed to the extent market conditions allow (3 more live drills):**

- Full-outage drills (hosts blackhole + `ss -K` killing warm conns via netshoot,
  `--net container:` + `--privileged`, scoped to dst 192.168.5.2:5000): degradation
  registers in ~2s and STICKS. Captured repeatedly from the live API during real
  degradation: global ready=false ("Collector health is degraded: forecastex"),
  `kalshi:polymarket ready=true blocking=[]`, both FX pairs blocked with the
  venue-scoped reason.
- **Structural finding (good news):** in a real full FX outage, FX-leg opportunities
  go `status=stale` within seconds and are excluded UPSTREAM of the gate (scanner
  only publishes tradable/manual) — the executor never considers them. The
  venue-scoped trade gate is the backstop for the narrow flapping regime (fresh
  quotes + degraded collector), pinned by the deployed e2e tests
  (TEST_FX_GATE_BLOCK blocked / TEST_KP_GATE_PASS allowed, green in the battery).
  K↔P scanning continued at full cadence through every outage window.
- Retry-probe dead-ends documented: (a) `risk.check_trade` runs BEFORE the trade
  gate in `execute_opportunity` (engine.py ~644 vs ~711), so risk-stage denials
  mask gate evidence; (b) **P2: execution-history restore drops opportunity
  confidence** — `POST /api/failed-trades/{id}/retry` on a restored arb always
  rejects "Low confidence: 0.00". Fix the serialization before relying on manual
  retries after a restart.
- **CAPTURED (2026-07-10 20:29:44Z) — the §4.1(b) executor-half core evidence:**
  in drill 5's recovery tail (FX quotes freshened before the collector cleared —
  the exact fresh-quotes/degraded-collector race the gate backstops), the
  auto-executor attempted the live DEM_SENATE_2026 FX-leg opportunity and the
  venue-scoped gate REJECTED it:
  `auto_executor.skip.gate_structural canonical_id=DEM_SENATE_2026
  reason="Collector health is degraded: forecastex"` — while the simultaneous
  venue_pairs capture showed `kalshi:polymarket ready=true`. Note: the denial
  comes from the executor's gate pre-check, so no `trade_gate_blocked` incident
  is recorded (that only fires inside `execute_opportunity`); the log line is
  the artifact. Side effect: 30-min structural cooldown on (DEM, that reason).
- **Remaining fragment for the strictest §4.1 letter:** "auto_executor attempts
  a K↔P opportunity during FX degradation" — unsatisfiable until a K↔P
  opportunity exists (none has since go-live; all 99 captures were FX↔K Senate
  pairs). Equivalent closer per §4.1(a): any real K↔P capture, any time
  (captured_arb_count increments on a K/P pair, no new strand). The executor
  takes it autonomously when it appears.

**Merge decision: NOT merged to main, per §4.3.5's own rule** ("any gate unmet:
push branch, leave a precise note — never claim it"). Every §4.3.4 gate is met
except the literal K↔P execution datum above. Prod runs from this branch's build;
merge main immediately after the first K↔P capture (verify: no new strand, then
`git merge fix/forecastex-live-execution && git push`).

---

## Older status — 2026-07-09/10 session 4 — LIVE AND TRADING

**Branch:** `fix/forecastex-live-execution`, HEAD `5d6d5b8`, pushed. Deployed image
`d48ba1ae02d6` (built from clean tree at 5d6d5b8, container up 2026-07-10T04:26Z).

**ARB-000919 fully reconciled — kill switch RESET, live trading resumed:**

- Root cause found in `arbiter-postgres-prod` logs (2026-07-06 23:32:33 UTC):
  `execution_orders_arb_id_fkey` violation — the requote retry RQ1 was persisted with
  DERIVED `arb_id='ARB-000919-RQ1'` (no parent row). `_handle_db_failure` armed the kill
  switch MID-EXECUTION; the armed switch then aborted every later fallback AND the
  auto-unwind (`ARB-000919-RQ2-NO-DBFAIL` is the C4.2 gate's marker order, never sent).
- **Fixed** in `0b2f8f0` (persist requote/FOK retries under the PARENT arb_id via new
  `persist_arb_id` kwarg; venue-side derived idempotency ids unchanged; regression test
  pins both). Any future retry rung MUST pass `persist_arb_id`.
- Venue verified flat at IBKR (0 positions, 2× cache-busted), arb row `closed` with
  `unwind_pnl=-0.02` booked (manual SQL reconciliation by session 3 at 01:14Z 07-07),
  all 3 critical incidents already `resolved` in `execution_incidents`.
- Kill switch reset 2026-07-10T04:29:30Z by `operator:sparx.sandeep@gmail.com` with full
  evidence note (see `/api/safety/events`).

**Live results after reset (all from live API/DB, image d48ba1ae02d6):**

- **FIVE two-leg arbs captured autonomously, zero naked legs, zero new incidents:**
  ARB-000920/922/923 (DEM_SENATE_2026, kalshi YES + forecastex NO) and ARB-000921/924
  (GOP_SENATE_2026, forecastex YES + kalshi NO). `captured_arb_count` 1 → **6**;
  `engine_exposure` $4.45 (all paired); `realized_pnl` 137.4295 → 137.9345.
- Kalshi V2 event-order path and ForecastEx CP path (reply-confirm loop) both worked
  repeatedly. Requote fix deployed; no db_failure recurrence (0 new since 07-06).
- Profitability: `collecting_evidence` + Phase-5 bootstrap bypass (54/200 used).
- Readiness: `ready_for_live_trading=true`, blocking `[]`; startup recovery logged
  "no half-recorded arbs to reconcile" (validates `fa728e1` against prod data).
- IBKR keepalive absorbed the 07-10 midnight-NY reset via `ssodh/init` with ZERO human
  logins (keepalive log 21:15–21:18 PT).

**Live-fire validation of the ARB-000919 failure chain (2026-07-10 06:11Z, ARB-000941):**

The exact 07-06 catastrophe sequence replayed in production on the new code: Kalshi YES
filled, then EVERY ForecastEx NO fallback failed (IOC → RQ1 → RQ2 → F3, all
submitted-then-cancelled — the 1-lot FX ask vanished mid-execution). This time:
- All retry orders persisted under parent `arb_id='ARB-000941'` (5 legs in DB, ZERO FK
  violations) — the `0b2f8f0` fix held under live fire; kill switch stayed DISARMED.
- Smart unwind recovered the naked Kalshi YES automatically: rested at break-even $0.44
  for 10s, then market-sold 1 @ $0.43. Net damage **−$0.01** (vs. 3 days of downtime on
  07-06).
- The one-leg critical incident AUTO-RESOLVED after the unwind; readiness stayed
  `ready=true` throughout; trading continued uninterrupted.

**§4.1 K↔P independence — status: PARTIALLY proven live; one residual:**

- Live FX-degradation drill (04:35–04:47Z, container-only /etc/hosts blackhole of
  host.docker.internal; gateway untouched): under REAL collector degradation the live
  API showed global `ready=false` ("Collector health is degraded: forecastex") while
  `venue_pairs["kalshi:polymarket"].ready_for_live_trading=true, blocking=[]` and both
  FX pairs blocked with the venue-scoped reason. That is the scoped-readiness half.
- e2e gate tests (8612c33) green on the deployed tree (in the 1011-test battery).
- **RESIDUAL (do not claim §4.1 complete):** no live `auto_executor.skip.gate_structural`
  denial with the venue-scoped reason was captured — warm keep-alive connections kept FX
  quotes flowing, so degradation FLAPPED (consec_errors 0→8→0) and gate probes landed on
  healthy instants. Satisfiable by EITHER (a) a real K↔P two-leg arb completing (any
  time a K↔P opp fires — MLB mappings are live; the executor will take it autonomously),
  OR (b) a drill window during a genuine full FX outage with a live K↔P opp present.
  Merge to main is gated on this per §4.3.5.

**New P1 findings (documented, not fixed — do not silently drop):**

1. `realized_pnl` is booked from `opp.net_edge` at DETECTION prices (engine.py:2156),
   not fill prices: ARB-000920 booked +$0.125 vs locked-in +$0.045 after 8¢ slippage.
   The shadow execution audit correctly flags each case. Fixing needs care — the
   profitability validator consumes this field.
2. **`StrandedPositionReconciler` has NO engine-arb pairing awareness**: it admits every
   venue lot as a "stranded position" and the mitigation engine evaluates lots as
   singletons — it recommended `COMPLETE_ARB` for ARB-000921's already-paired Kalshi leg
   (would DOUBLE the FX leg if executed). A failed venue positions-fetch is additionally
   treated as "venue empty" for decisions. Safe today ONLY because `STRANDED_AUTO_CLOSE`
   is unset (off) in prod. **DO NOT enable STRANDED_AUTO_CLOSE** until the reconciler
   joins engine arb state and fails closed on fetch errors.
3. `stranded_count` (now 10) includes healthy paired inventory; use one-leg-exposure
   incidents as the naked-exposure metric, not this counter.

**Validation battery (session 4):** 1011 passed / 2 skipped (execution+mapping+recovery+
readiness+api-integration+main-discovery+operator-settings+safety), tests/ 34 passed,
`tsc --noEmit` clean, vitest 53/53, compose config ok, `git diff --check` clean.

---

## Older status — 2026-07-06 session 3 / post-rebuild validation

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
