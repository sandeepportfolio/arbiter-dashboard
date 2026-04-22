# Market Mapping Validation Report

**Date:** 2026-04-22  
**Agent:** Validation Agent 1 (indices 0–75, all 62 available)  
**Validated against:** Kalshi API (`api.elections.kalshi.com/trade-api/v2`) + Polymarket Gamma API (`gamma-api.polymarket.com`)

---

## Summary

| Status       | Count | %    |
|-------------|-------|------|
| VALID        | 0     | 0%   |
| INVALID      | 62    | 100% |
| NEEDS_REVIEW | 0     | 0%   |
| **Total**    | **62**|      |

**Result: ALL 62 mappings are INVALID. None are usable for arbitrage.**

---

## Root Cause: Structural Mismatch

Every single mapping in `data/discovered_mappings.json` pairs a **Kalshi MVE (Multi-Variate Event) parlay market** against a **single Polymarket binary market**. These are structurally incompatible for arbitrage.

### Kalshi MVE Market Types Found

| Kalshi Prefix               | Count | Description                                  |
|----------------------------|-------|----------------------------------------------|
| KXMVESPORTSMULTIGAMEEXTENDED| 45    | Multi-leg sports parlay (2–4+ legs)          |
| KXMVECROSSCATEGORY          | 17    | Cross-sport/category parlay (2–3 legs)       |

### Why This Is Not Arbitrageable

A Kalshi MVE parlay resolves YES only if **all legs win simultaneously**. For example:

**Example 1 (KXMVESPORTSMULTIGAMEEXTENDED):**
- Leg 1: Wolfsburg vs. BMG — Both Teams to Score (YES)
- Leg 2: Köln vs. Leverkusen — Leverkusen wins (YES)  
- Leg 3: Mainz 05 vs. Bayern Munich — Bayern wins (YES)
- Matched against: Polymarket "Bayer 04 Leverkusen vs. FC Bayern München: BTTS"

These are **three different games combined** vs. **one different game** — both the structure and the underlying events are mismatched.

**Example 2 (KXMVECROSSCATEGORY):**
- Leg 1: PHX vs. OKC — Jalen Williams 15+ points (YES)
- Leg 2: ORL vs. DET — Total over 204.5 (YES)
- Matched against: some single Polymarket NBA market

Two different games from different matchups combined into one Kalshi parlay, matched against one single Polymarket event.

### Why the Discovery Algorithm Produced These

The embedding-based discovery algorithm found **semantic similarity** between individual leg labels (e.g., "Both Teams to Score" in a parlay description) and standalone Polymarket questions about the same sport/category. It matched on surface text similarity without filtering for market structure type.

The `KXMVESPORTSMULTIGAMEEXTENDED` and `KXMVECROSSCATEGORY` market tickers are a **clear signal** that should be excluded from any future discovery pass.

---

## Recommended Fixes for Discovery Algorithm

1. **Filter out all `KXMVE*` ticker prefixes** from Kalshi candidates before matching.
   - `KXMVESPORTSMULTIGAMEEXTENDED-*` → skip
   - `KXMVECROSSCATEGORY-*` → skip
   - Any ticker with `MVE` in the prefix → skip

2. **Target single-market Kalshi tickers** — e.g., `KXNBAPTS-*`, `KXBUNDESLIGABTTS-*`, `KXNBATOTAL-*` — which represent exactly one outcome on one event.

3. **Re-run the embedding discovery** after applying the filter — the underlying single markets embedded within these parlays (like `KXBUNDESLIGABTTS-26APR25WOBBMG`) are the ones that should be matched against Polymarket.

---

## Files Updated

- `data/discovered_mappings.json` — all 62 mappings now have `validation_status: "INVALID"` and `validation_reason` fields
- `data/validation_report.json` — full machine-readable report with per-mapping details
- `data/validation_report.md` — this report
- `scripts/validate_mappings.py` — validation script (reusable for future mapping batches)
