# Validation Report — Batch 6 (Indices 500–624)

Generated: 2026-04-22T10:51:44Z
Agent: VALIDATION AGENT 6
Batch: seeds[500:625] — 125 mappings

---

## Summary

| Metric | Count |
|--------|-------|
| Total validated | 125 |
| **Confirmed (valid)** | **0** |
| Invalid / unchanged | 125 |
| Kalshi market found (HTTP 200) | 91 |
| Kalshi not found or rate-limited | 34 |
| Polymarket market found | 0 |

---

## Key Findings

### Batch Composition

All 125 mappings are **golf round-leader player markets**:

- **Indices 500–584** (`KXPGAR3LEAD-MAST26-*`): "Will [Player] lead at end of Round 3 in The Masters?"
  - Seeded resolution_date: 2026-04-20 ← **incorrect** (actual close: 2026-04-12)
  - Kalshi status: **finalized** (already resolved 2026-04-12)

- **Indices 585–624** (`KXPGAR3LEAD-RBH26-*`): "Will [Player] lead at end of Round 3 in RBC Heritage?"
  - Seeded resolution_date: 2026-04-27 ← **incorrect** (actual close: 2026-04-18)
  - Kalshi status: **finalized** (already resolved 2026-04-18)

### Root Cause: Polymarket Has No Equivalent Markets

All 125 Polymarket slugs (`tec-masters-round3leader-*`) return **no results** from the Polymarket Gamma API. A broader search confirmed that Polymarket does **not** offer per-player round-leader markets for golf — only tournament-winner markets exist (e.g. "Masters Winner 2024"). The Polymarket slugs in these seeds are fabricated and have no live counterpart.

### Kalshi Markets Exist But Are All Finalized

Of the 91 Kalshi tickers returning HTTP 200, all have `status: finalized`. Example:
- `KXPGAR3LEAD-MAST26-APOT` → "Will Aldrich Potgieter lead Round 3 of The Masters?" — finalized 2026-04-12
- `KXPGAR3LEAD-RBH26-ASCH` → "Will Adam Schenk lead Round 3 of RBC Heritage?" — finalized 2026-04-18

These markets have already resolved and cannot be traded. Even if Polymarket had equivalents, the arbitrage window has closed.

### Seeded Resolution Dates Are Wrong

Seeds used the tournament's final-round date rather than the Round 3 cutoff date:
- MAST26: seeded 2026-04-20 vs actual close 2026-04-12 (8-day error)
- RBH26: seeded 2026-04-27 vs actual close 2026-04-18 (9-day error)

---

## Recommendation

**0 mappings confirmed. Remove or archive all 125 from this batch.**

The auto-discovery pipeline needs to:
1. Validate Polymarket slug existence **before** generating candidate seeds
2. Filter out per-round sports sub-markets (no Polymarket coverage)
3. Use Kalshi `close_time` as `resolution_date`, not the tournament end date

---

## Failure Breakdown

| Reason | Count |
|--------|-------|
| `poly_not_found` — slug not on Polymarket Gamma | 125 |
| `expiry_mismatch` — Kalshi close ≠ seeded resolution | 91 |
| `kalshi_not_found` — 404 or 429 from Kalshi API | 34 |

---

## Sample Invalid Mappings
- [500] `KXPGAR3LEAD-MAST26-ANOV` / `tec-masters-round3leader-2026-04-12-andnov`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [501] `KXPGAR3LEAD-MAST26-APOT` / `tec-masters-round3leader-2026-04-12-aldpot`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [502] `KXPGAR3LEAD-MAST26-BWAT` / `tec-masters-round3leader-2026-04-12-bubwat`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [503] `KXPGAR3LEAD-MAST26-BKOE` / `tec-masters-round3leader-2026-04-12-brokoe`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [504] `KXPGAR3LEAD-MAST26-BHOL` / `tec-masters-round3leader-2026-04-12-brahol`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [505] `KXPGAR3LEAD-MAST26-BGRI` / `tec-masters-round3leader-2026-04-12-bengri`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [506] `KXPGAR3LEAD-MAST26-BDEC` / `tec-masters-round3leader-2026-04-12-brydec`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [507] `KXPGAR3LEAD-MAST26-BCAM` / `tec-masters-round3leader-2026-04-12-bricam`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [508] `KXPGAR3LEAD-MAST26-BHAR` / `tec-masters-round3leader-2026-04-12-brihar`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [509] `KXPGAR3LEAD-MAST26-CORT` / `tec-masters-round3leader-2026-04-12-carort`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [510] `KXPGAR3LEAD-MAST26-CSMI` / `tec-masters-round3leader-2026-04-12-camsmi`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [511] `KXPGAR3LEAD-MAST26-CSCH` / `tec-masters-round3leader-2026-04-12-chasch`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [512] `KXPGAR3LEAD-MAST26-CCON` / `tec-masters-round3leader-2026-04-12-corcon`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [513] `KXPGAR3LEAD-MAST26-CMOR` / `tec-masters-round3leader-2026-04-12-colmor`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [514] `KXPGAR3LEAD-MAST26-CJAR` / `tec-masters-round3leader-2026-04-12-casjar`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [515] `KXPGAR3LEAD-MAST26-CAME` / `tec-masters-round3leader-2026-04-12-camyou`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [516] `KXPGAR3LEAD-MAST26-CGOT` / `tec-masters-round3leader-2026-04-12-chrgot`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [517] `KXPGAR3LEAD-MAST26-DWIL` / `tec-masters-round3leader-2026-04-12-danwil`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [518] `KXPGAR3LEAD-MAST26-DJOH` / `tec-masters-round3leader-2026-04-12-dusjoh`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [519] `KXPGAR3LEAD-MAST26-DRIL` / `tec-masters-round3leader-2026-04-12-davril`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [520] `KXPGAR3LEAD-MAST26-DBER` / `tec-masters-round3leader-2026-04-12-danber`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [521] `KXPGAR3LEAD-MAST26-EFAN` / `tec-masters-round3leader-2026-04-12-ethfan`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [522] `KXPGAR3LEAD-MAST26-FLAO` / `tec-masters-round3leader-2026-04-12-fiflao`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [523] `KXPGAR3LEAD-MAST26-FCOU` / `tec-masters-round3leader-2026-04-12-frecou`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [524] `KXPGAR3LEAD-MAST26-GWOO` / `tec-masters-round3leader-2026-04-12-garwoo`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [525] `KXPGAR3LEAD-MAST26-HHAL` / `tec-masters-round3leader-2026-04-12-harhal`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [526] `KXPGAR3LEAD-MAST26-HLI` / `tec-masters-round3leader-2026-04-12-haoli`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [527] `KXPGAR3LEAD-MAST26-HENG` / `tec-masters-round3leader-2026-04-12-hareng`
  - poly_not_found: not_found_on_gamma
  - expiry_mismatch: kalshi=2026-04-12 vs seed=2026-04-20
- [528] `KXPGAR3LEAD-MAST26-HMAT` / `tec-masters-round3leader-2026-04-12-hidmat`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma
- [529] `KXPGAR3LEAD-MAST26-JOLA` / `tec-masters-round3leader-2026-04-12-josola`
  - kalshi_not_found: rate_limited
  - poly_not_found: not_found_on_gamma