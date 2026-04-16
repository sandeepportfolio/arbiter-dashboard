---
phase: 01-api-integration-fixes
verified: 2026-04-16T10:30:00Z
status: human_needed
score: 14/16 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Run python -m arbiter.verify_collectors with Kalshi credentials configured (KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH in .env)"
    expected: "Kalshi collector returns PASS -- markets fetched, prices >= 0, schema matches"
    why_human: "Kalshi requires RSA-PSS signed auth for all endpoints. Credentials were not available during automated verification. SKIP is acceptable if credentials are genuinely unavailable."
  - test: "Observe logs during a 60-second run of python -m arbiter.main with Polymarket credentials configured"
    expected: "poly-heartbeat task produces heartbeat log lines every 5 seconds -- observable in output"
    why_human: "Heartbeat behavior requires a live running process with L2 auth credentials to emit observable keepalive logs. Cannot verify from static code inspection alone."
  - test: "Run python -m arbiter.verify_collectors with POLY_PRIVATE_KEY, POLY_SIGNATURE_TYPE, POLY_FUNDER configured in .env"
    expected: "Polymarket CLOB book fetches work with authenticated credentials (not just Gamma API read-only path)"
    why_human: "Authenticated CLOB endpoints require real wallet credentials. Gamma API was verified (PASS) but CLOB auth path was not exercised during automated verification."
---

# Phase 1: API Integration Fixes -- Verification Report

**Phase Goal:** All platform API calls succeed -- collectors return real data, order submission formats are correct, authentication works end-to-end
**Verified:** 2026-04-16T10:30:00Z
**Status:** human_needed
**Re-verification:** No -- initial verification

## Goal Achievement

### Observable Truths (from ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| SC1 | Kalshi order payload uses `yes_price_dollars` string format and `count_fp` for fractional markets | VERIFIED | `engine.py` line 840-845: `"count_fp": f"{float(qty):.2f}"` and `"yes_price_dollars": f"{price:.4f}"`. Legacy `price_cents` and `"yes_price"` integer fields absent. 11 tests pass. |
| SC2 | Polymarket ClobClient initializes with correct `signature_type` and `funder` -- `client.get_api_keys()` succeeds without 401 | PARTIAL | Code: `engine.py` lines 914-915 pass both params. `PolymarketConfig` has `signature_type=2` default and `funder` from env. Auth success requires live credentials (see human verification). |
| SC3 | Polymarket heartbeat manager runs as dedicated async task sending keepalive every 5s -- observable in logs | PARTIAL | Code: `polymarket_heartbeat_loop` implemented in `engine.py` with `asyncio.sleep(5)`, `post_heartbeat`, and CancelledError handling. Task launched as `name="poly-heartbeat"` in `main.py`. Observable behavior requires live credentials (see human verification). |
| SC4 | Fee calculations for all platforms match documented rates -- unit tests pass with real rate values | VERIFIED | 26 unit tests pass. `settings.py` has `POLYMARKET_DEFAULT_TAKER_FEE_RATE=0.05`, 11-category `fallback_rates` dict (crypto=0.072, politics=0.04, sports=0.03, geopolitics=0.0). Shadow calculator in `math_auditor.py` matches. Spot-check: all 5 category calculations produce correct values. |
| SC5 | All three collectors successfully fetch and parse current market data without errors | PARTIAL | PredictIt PASS (8 markets, schema OK). Polymarket PASS via Gamma API (8 markets, prices >= 0, fee_rate >= 0). Kalshi SKIP (no credentials). Kalshi schema compatibility cannot be confirmed without auth. |

**Roadmap Score:** 3/5 SCs fully verified (SC1, SC4 verified; SC2, SC3, SC5 partially verified pending human confirmation)

### Plan Must-Have Truths (from PLAN frontmatter)

| # | Plan | Truth | Status | Evidence |
|---|------|-------|--------|----------|
| 1 | 01-01 | Polymarket fee calculations use correct per-category rates matching official 2026 schedule | VERIFIED | `settings.py` fallback_rates has all 11 categories. Spot-check confirms correct values. |
| 2 | 01-01 | settings.py and math_auditor.py shadow calculator produce identical fee values for every category | VERIFIED | Both files have identical rate dicts. 26 tests pass including cross-validation test. |
| 3 | 01-01 | All existing fee tests pass with updated expected values | VERIFIED | `python -m pytest arbiter/audit/test_math_auditor.py` -- 26 passed, 0 failed. |
| 4 | 01-02 | Kalshi order payload uses yes_price_dollars string format instead of integer cents | VERIFIED | `engine.py` line 843: `order_body["yes_price_dollars"] = f"{price:.4f}"` |
| 5 | 01-02 | Kalshi order payload uses count_fp string format for quantity | VERIFIED | `engine.py` line 840: `"count_fp": f"{float(qty):.2f}"` |
| 6 | 01-02 | Kalshi response parsing reads _dollars and _fp fields correctly | VERIFIED | `engine.py` line 874: reads `fill_count_fp`; line 884: reads `yes_price_dollars` in fallback chain |
| 7 | 01-02 | No legacy yes_price or no_price integer fields remain in order construction | VERIFIED | `grep "price_cents = max"` -- not found. Legacy fields absent from `_place_kalshi_order`. |
| 8 | 01-03 | No PredictIt execution code remains in engine.py, main.py, api.py, or workflow/ | VERIFIED | `PredictItWorkflowManager` not in any file. `workflow/__init__.py` is empty package. Both `from arbiter.main import main` and `from arbiter.api import create_api_server` succeed. |
| 9 | 01-03 | PredictIt collector in arbiter/collectors/predictit.py is untouched and still works | VERIFIED | `from arbiter.collectors.predictit import PredictItCollector` succeeds. |
| 10 | 01-03 | PredictIt fee functions in scanner/arbitrage.py and settings.py are untouched | VERIFIED | `predictit_order_fee` callable, `PREDICTIT_PROFIT_FEE_RATE=0.1`, `PREDICTIT_WITHDRAWAL_FEE_RATE=0.05` intact. |
| 11 | 01-03 | No import errors when running the system after workflow removal | VERIFIED | `from arbiter.main import main` -- OK. `from arbiter.api import create_api_server` -- OK. |
| 12 | 01-04 | ClobClient initializes with signature_type and funder parameters from config | VERIFIED | `engine.py` lines 914-915: `signature_type=self.config.polymarket.signature_type`, `funder=self.config.polymarket.funder` |
| 13 | 01-04 | Polymarket heartbeat sends keepalive every 5 seconds as a dedicated async task | VERIFIED | `engine.py`: `polymarket_heartbeat_loop` with `asyncio.sleep(5)` and `post_heartbeat`. `main.py` line 189: task launched as `name="poly-heartbeat"`. |
| 14 | 01-04 | Heartbeat only starts after ClobClient has L2 auth credentials | VERIFIED | Heartbeat loop polls `_get_poly_clob_client()` in ready-wait loop before starting. |
| 15 | 01-04 | .env.template includes POLY_SIGNATURE_TYPE and POLY_FUNDER entries | VERIFIED | Lines 9-10 of `.env.template`: `POLY_SIGNATURE_TYPE=2` and `POLY_FUNDER=` |
| 16 | 01-05 | All three collectors produce valid PricePoint objects from live API responses | PARTIAL | PredictIt PASS, Polymarket PASS (Gamma API). Kalshi SKIP (no credentials). Human verification needed for Kalshi. |

**Plan Must-Have Score:** 14/16 truths fully verified (2 partial: plans 01-04 SC2/SC3 auth behavior, and 01-05 Kalshi schema)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `arbiter/config/settings.py` | Corrected fee rates, PolymarketConfig with signature_type/funder | VERIFIED | `POLYMARKET_DEFAULT_TAKER_FEE_RATE=0.05`, 11-category fallback_rates, `signature_type` and `funder` fields in PolymarketConfig |
| `arbiter/audit/math_auditor.py` | Shadow calculator with matching corrected rates | VERIFIED | Contains `"crypto": 0.072`, `"politics": 0.04`, `"default": 0.05`, all 11 categories |
| `arbiter/audit/test_math_auditor.py` | Updated test expectations, 26 tests pass | VERIFIED | Contains `test_polymarket_fee_politics` (0.0096), `test_polymarket_fee_crypto` (0.01728), `test_polymarket_fee_geopolitics` (0.0) |
| `arbiter/execution/engine.py` | Kalshi dollar string format, ClobClient auth, heartbeat | VERIFIED | `yes_price_dollars`, `count_fp`, `fill_count_fp`, `signature_type`, `funder`, `polymarket_heartbeat_loop`, `stop_heartbeat` all present |
| `arbiter/execution/test_engine.py` | Kalshi order format tests | VERIFIED | `test_kalshi_order_format_yes_side`, `test_kalshi_order_format_no_side`, `test_kalshi_response_parsing_dollar_strings` all present and passing |
| `arbiter/workflow/__init__.py` | Empty package (PredictIt removed) | VERIFIED | Only `"""ARBITER workflow package."""` -- no PredictIt imports |
| `arbiter/main.py` | No PredictIt workflow, heartbeat task added | VERIFIED | No PredictItWorkflowManager. `workflow_manager=None`. `poly-heartbeat` task. `engine.stop_heartbeat()` in shutdown. |
| `arbiter/api.py` | No PredictItWorkflowManager import | VERIFIED | `workflow_manager: Optional[object] = None` -- PredictIt types removed |
| `arbiter/collectors/polymarket.py` | Dynamic fee rate fetch, set_clob_client | VERIFIED | `_fetch_dynamic_fee_rate`, `get_fee_rate_bps`, `set_clob_client`, `_clob_client = None` in __init__, `logger.warning` fallback all present |
| `arbiter/.env.template` | POLY_SIGNATURE_TYPE and POLY_FUNDER entries | VERIFIED | Lines 9-10 present |
| `arbiter/verify_collectors.py` | Standalone collector verification script | VERIFIED | Contains `verify_predictit`, `verify_kalshi`, `verify_polymarket`. PredictIt PASS, Polymarket PASS (Gamma), Kalshi SKIP. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `arbiter/config/settings.py` | `arbiter/scanner/arbitrage.py` | import of polymarket_order_fee | VERIFIED | `polymarket_order_fee` callable from settings; fee rates flow through to scanner |
| `arbiter/audit/math_auditor.py` | `arbiter/config/settings.py` | shadow rates must match settings.py rates | VERIFIED | Both files have identical 11-category dicts. Cross-validation test confirms equality. |
| `arbiter/execution/engine.py` | Kalshi API v2 | `_place_kalshi_order` POST request with `yes_price_dollars` | VERIFIED | `engine.py` lines 840-845: correct format in POST body |
| `arbiter/main.py` | `arbiter/workflow/` | import removed | VERIFIED | No `from .workflow import PredictItWorkflowManager` in main.py |
| `arbiter/collectors/predictit.py` | `arbiter/utils/price_store.py` | still feeds price data (unchanged) | VERIFIED | PredictIt collector unchanged; `PredictItCollector` class present and imports cleanly |
| `arbiter/config/settings.py` | `arbiter/execution/engine.py` | PolymarketConfig.signature_type and .funder consumed by ClobClient init | VERIFIED | `engine.py` line 914-915: `signature_type=self.config.polymarket.signature_type`, `funder=self.config.polymarket.funder` |
| `arbiter/main.py` | `arbiter/execution/engine.py` | heartbeat task launch | VERIFIED | `main.py` line 189: `asyncio.create_task(engine.polymarket_heartbeat_loop(), name="poly-heartbeat")` |
| `arbiter/collectors/polymarket.py` | `py_clob_client.client.ClobClient` | `get_fee_rate_bps()` called during discover_markets | VERIFIED | `polymarket.py` line 108: `self._clob_client.get_fee_rate_bps(token_id)` in `_fetch_dynamic_fee_rate` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `arbiter/audit/test_math_auditor.py` | fee rate expectations | `math_auditor._polymarket_fee()` | Yes -- calls actual fee function, not mocked | FLOWING |
| `arbiter/execution/engine.py` | order_body | `_place_kalshi_order` | Yes -- price/qty params flow to formatted strings | FLOWING |
| `arbiter/collectors/polymarket.py` | `fee_rate` in discover_markets | `_fetch_dynamic_fee_rate` -> `ClobClient.get_fee_rate_bps()` OR fallback_rates | Yes -- real SDK call with fallback | FLOWING |
| `arbiter/execution/engine.py` | heartbeat_id | `post_heartbeat` response | Yes -- real API call (requires auth at runtime) | FLOWING (requires auth) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Fee calculations use correct 2026 rates | `_polymarket_fee(0.60, 'crypto')` | 0.017280 (expected 0.01728) | PASS |
| Fee calculations (politics) | `_polymarket_fee(0.60, 'politics')` | 0.009600 (expected 0.0096) | PASS |
| Fee calculations (geopolitics zero rate) | `_polymarket_fee(0.60, 'geopolitics')` | 0.000000 (expected 0.0) | PASS |
| Fee calculations (default fallback) | `_polymarket_fee(0.50, 'unknown')` | 0.012500 (expected 0.0125) | PASS |
| settings.py polymarket_order_fee matches | `polymarket_order_fee(0.60, category='politics')` | 0.009600 (expected 0.0096) | PASS |
| Kalshi order format: yes_price_dollars present | inspect `_place_kalshi_order` source | yes_price_dollars in source | PASS |
| Kalshi order format: no legacy price_cents | inspect `_place_kalshi_order` source | price_cents NOT in source | PASS |
| Heartbeat loop has 5s interval | inspect `polymarket_heartbeat_loop` source | asyncio.sleep(5) present | PASS |
| Heartbeat waits for ClobClient | inspect `polymarket_heartbeat_loop` source | `_get_poly_clob_client()` in ready-wait | PASS |
| Dynamic fee: get_fee_rate_bps called | inspect `_fetch_dynamic_fee_rate` source | `get_fee_rate_bps` in source | PASS |
| Dynamic fee: fallback warning logged | inspect `_fetch_dynamic_fee_rate` source | `logger.warning` in source | PASS |
| Fee unit tests pass | `pytest arbiter/audit/test_math_auditor.py` | 26 passed, 0 failed | PASS |
| Engine unit tests pass | `pytest arbiter/execution/test_engine.py` | 11 passed, 0 failed | PASS |
| Full test suite (excl. pre-existing) | `pytest arbiter/ --ignore=test_api_integration.py` | 85 passed, 0 failed | PASS |
| main.py imports clean | `from arbiter.main import main` | OK | PASS |
| api.py imports clean | `from arbiter.api import create_api_server` | OK | PASS |
| workflow package clean | `import arbiter.workflow; not hasattr(..., 'PredictItWorkflowManager')` | OK | PASS |
| PredictIt collector intact | `from arbiter.collectors.predictit import PredictItCollector` | OK | PASS |
| PolymarketConfig has new fields | `c = PolymarketConfig(); c.signature_type == 2` | OK | PASS |
| Heartbeat methods exist | `hasattr(ExecutionEngine, 'polymarket_heartbeat_loop')` | OK | PASS |
| Collector script functions | `from arbiter.verify_collectors import verify_predictit` | OK | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| API-01 | 01-02 | Kalshi order submission uses dollar string pricing | SATISFIED | `yes_price_dollars`, `no_price_dollars`, `count_fp` in engine.py. Legacy `price_cents` removed. 3 format tests pass. |
| API-02 | 01-04 | Polymarket ClobClient initialized with correct `signature_type` and `funder` | SATISFIED | `PolymarketConfig.signature_type`, `PolymarketConfig.funder` added. ClobClient init passes both. Auth success requires live credentials (human verification). |
| API-03 | 01-04 | Polymarket heartbeat sends keepalive every 5 seconds | SATISFIED | `polymarket_heartbeat_loop` with 5s sleep in engine.py. Task launched in main.py. CancelledError handled. Ready-wait before start. |
| API-04 | 01-01, 01-04 | Fee calculations use correct platform-specific rates | SATISFIED | 11-category Polymarket rates in settings.py and math_auditor.py. Dynamic fee fetch with fallback in polymarket.py. 26 tests pass. |
| API-05 | 01-03 | PredictIt scoped to read-only -- removed from automated execution | SATISFIED | Workflow files deleted. No PredictItWorkflowManager in main.py, api.py. Collector and fee functions preserved. Import checks pass. |
| API-06 | 01-04 | Polymarket platform decision resolved with correct SDK, endpoints, auth | SATISFIED | signature_type=2 (GNOSIS_SAFE) for US proxy wallets. funder env var added. .env.template documents both. ClobClient init corrected. |
| API-07 | 01-05 | All platform collectors verified against current API responses | NEEDS HUMAN | PredictIt PASS (8 markets, schema OK). Polymarket PASS (Gamma API, 8 markets). Kalshi SKIP (no credentials). Kalshi schema validation and Polymarket CLOB auth path need human confirmation. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `arbiter/test_api_integration.py` | 82 | Pre-existing test failure: `assert "ARBITER LIVE" in ops_html` | Info | Pre-existing failure, unrelated to Phase 1 work. Present before any Phase 1 commits. No impact on Phase 1 goal. |

No anti-patterns found in any Phase 1 modified files. No TODO/FIXME/placeholder comments, no empty implementations, no hardcoded stubs affecting dynamic data paths.

### Human Verification Required

#### 1. Kalshi Live Collector Schema Validation

**Test:** Configure Kalshi credentials in `.env` (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`) and run `python -m arbiter.verify_collectors`
**Expected:** Kalshi returns PASS -- markets fetched, prices >= 0, no KeyError or schema mismatch. If credentials are unavailable in this environment, SKIP is acceptable and API-07 partial satisfaction is documented.
**Why human:** Kalshi requires RSA-PSS signed auth for all market data endpoints. No credentials were available during automated verification. Cannot validate schema compatibility without auth.

#### 2. Polymarket Heartbeat Observable in Logs

**Test:** Configure Polymarket credentials (`POLY_PRIVATE_KEY`, `POLY_SIGNATURE_TYPE=2`, `POLY_FUNDER`) in `.env` and run `python -m arbiter.main` for 60+ seconds. Observe log output.
**Expected:** Log lines from `poly-heartbeat` task appear every 5 seconds. ClobClient initializes with L2 auth before heartbeat starts.
**Why human:** Heartbeat behavior requires a running process with valid credentials. The implementation is structurally correct (code verified) but observable log evidence requires live execution.

#### 3. Polymarket Authenticated CLOB Path

**Test:** Run `python -m arbiter.verify_collectors` with POLY_PRIVATE_KEY, POLY_SIGNATURE_TYPE, POLY_FUNDER configured.
**Expected:** Polymarket returns PASS using authenticated CLOB endpoints (not just read-only Gamma API). `client.get_api_keys()` or `create_or_derive_api_creds()` completes without 401.
**Why human:** The automated run used only the unauthenticated Gamma API path. Authenticated CLOB book fetching and auth initialization require real wallet credentials.

---

## Summary

Phase 1 goal is **substantially achieved**. All code changes are correct, complete, and wired:

- **Kalshi order format** (API-01): Fully implemented and tested. Dollar string pricing replaces legacy integer cents. 3 format tests + 8 existing engine tests pass.
- **Polymarket fee rates** (API-04): All 11 categories corrected. Primary and shadow calculators match. 26 unit tests pass. Spot-checks confirm correct values.
- **PredictIt scoping** (API-05): Clean break. Workflow deleted. Collector and fee functions preserved. All imports clean.
- **Polymarket auth** (API-02, API-06): `signature_type` and `funder` added to config and ClobClient init. `.env.template` updated.
- **Heartbeat** (API-03): Dedicated async task with 5s interval, ready-wait before start, CancelledError handling. Launched in main.py lifecycle.
- **Dynamic fee fetch** (API-04 extension): `get_fee_rate_bps()` called per token with range validation and fallback warning.
- **Collector verification** (API-07): Script created and run. PredictIt and Polymarket (Gamma path) pass with real data.

3 items require human confirmation to close API-07 fully and confirm live behavior of auth-dependent features (Kalshi schema, Polymarket CLOB auth, heartbeat in running process).

---

_Verified: 2026-04-16T10:30:00Z_
_Verifier: Claude (gsd-verifier)_
