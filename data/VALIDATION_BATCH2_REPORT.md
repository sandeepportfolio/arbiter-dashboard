# Validation Agent 2 — Batch 2 Report

**Date:** 2026-04-22  
**Agent:** Validation Agent 2  
**Batch:** indices 75–150 of `scripts/output/market_candidates_v3.json`  
**Candidates evaluated:** 76  

---

## Summary

| Status | Count |
|--------|-------|
| VALID (confirmed) | **0** |
| INVALID | **76** |
| NEEDS_REVIEW | **0** |
| Confirmed for trading | **0** |

---

## Root-Cause Analysis

### Finding 1: Polymarket slugs were pattern-generated, not API-verified (62/76)

The largest failure mode (`polymarket_not_found`, 62 markets) is that the Polymarket slugs in
`market_candidates_v3.json` follow a `tec-<sport>-<event>-<date>-<team>` naming convention that was
**fabricated** by the v3 discovery script based on assumed slug patterns.  These slugs do not exist
on the live Polymarket Gamma API (`gamma-api.polymarket.com`).

Real Polymarket slugs for comparable markets follow completely different naming patterns:
- `will-the-detroit-tigers-win-the-2026-american-league-championship-series` (not `tec-mlb-alchamp-2026-09-27-det`)
- `nhl-2025-26-hart-memorial-trophy-jason-robertson` (not `tec-nhl-hart-2026-06-30-jasrob`)
- `will-nikola-jokic-win-the-2026-nba-mvp-award` (not `tec-nba-mvp-2026-06-10-nikjok`)

**Action required:** The v3 discovery output must be regenerated with real Polymarket slugs fetched
from the live API before any of these candidates can be confirmed.

### Finding 2: Fuzzy-matched MLB markets resolve on different events (12/76)

For MLB AL/NL Champion markets, the fuzzy fallback found actual Polymarket World Series winner
markets (e.g. `will-the-houston-astros-win-the-2026-world-series`) as the nearest text match.
However:

- **Kalshi:** "Will [Team] win the 2026 American League Championship?" (resolves October 2026, close_time=2028-10-31)
- **Polymarket (matched):** "Will [Team] win the 2026 World Series?" (resolves ~October 2026)

These are **fundamentally different markets** (League Championship ≠ World Series).  Even if both
platforms had these markets, they would not be arbitrageable as a pair.

Date delta: **853 days** (Kalshi close_time 2028-10-31 vs Polymarket 2026-10-04 for WS) — this also
strongly signals mismatched markets.

### Finding 3: Two Masters markets already finalized (2/76)

`KXPGAR1LEAD-MAST26-TFIN` and `KXPGAR1LEAD-MAST26-TWOO` have Kalshi status `finalized` — the 2026
Masters has already concluded (Apr 22, 2026).  These are stale candidates.

---

## By Market Type

| Category | Candidates | VALID | INVALID | Root cause |
|----------|-----------|-------|---------|------------|
| MLB AL Champion | 11 | 0 | 11 | Slug missing + date/market type mismatch |
| MLB NL Champion | 11 | 0 | 11 | Slug missing + date/market type mismatch |
| NHL Hart Trophy | 29 | 0 | 29 | Polymarket slugs not found |
| NBA MVP | 22 | 0 | 22 | Polymarket slugs not found |
| Science (Mars) | 1 | 0 | 1 | Polymarket slug not found |
| PGA Golf | 2 | 0 | 2 | Kalshi already finalized |

---

## Actionable Recommendations

1. **Discard all 76 batch-2 candidates** — none are valid cross-platform pairs as currently defined.

2. **Fix the v3 discovery slug generation:**
   - Fetch real Polymarket slugs from `gamma-api.polymarket.com/markets` with pagination
   - Match against Kalshi markets by question similarity, not by slug-pattern guessing
   - For NHL Hart: known real slugs include `nhl-2025-26-hart-memorial-trophy-*`
   - For NBA MVP: search with `q=nba mvp 2026` or equivalent

3. **Separate market types for arbitage:**
   - "League Champion" markets on Kalshi ≠ "World Series winner" on Polymarket — these are not the same event
   - A valid mapping requires both platforms to ask the **same binary question** with the same resolution criteria

4. **Remove finalized Kalshi markets from the candidate pool:**
   - Filter `status != "finalized"` before including in any candidate set

5. **The 500-market Polymarket page used for fuzzy matching is insufficient** for niche sports award
   markets (specific player Hart/MVP candidates). A full catalog pull (20k+ markets) is needed to
   find these if they exist.

---

## Data Files

- Full per-mapping results: `data/validation_report_batch2.json`
- Confirmed mappings: `data/validated_mappings_batch2.json` (empty — 0 confirmed)
- Validation script: `scripts/validate_mappings_batch2.py`
