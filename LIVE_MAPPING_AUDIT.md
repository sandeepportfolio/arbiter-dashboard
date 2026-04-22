# LIVE_MAPPING_AUDIT

Audit date: 2026-04-21 23:56:30 UTC

Question: do we currently have at least one identical tradable Kalshi <-> Polymarket US market pair, or are the checked-in seeds stale with no live overlap?

Verdict:
- The checked-in seeds are stale.
- We do have current live overlap today.
- The strongest live pairs are 2026 U.S. House control and 2026 U.S. Senate control.
- I would refresh seeds to those live pairs, then keep `allow_auto_trade=false` until a human signs off on final SAFE-06 review.

## Evidence

### Stale checked-in seeds

Current repo seeds in `arbiter/config/settings.py` are stale against live venue metadata:

- `DEM_HOUSE_2026` points Kalshi to `KXPRESPARTY-2028`.
  - Live check: `GET https://api.elections.kalshi.com/trade-api/v2/markets/KXPRESPARTY-2028`
  - Result: `{"error":{"code":"not_found",...}}`
- Polymarket seed slugs are not the live open slugs anymore:
  - `which-party-will-win-the-house-in-2026`
  - `which-party-will-win-the-senate-in-2026`
  - `republican-presidential-nominee-2028`
  - `democratic-presidential-nominee-2028`
  - `georgia-senate-election-winner`
  - `michigan-senate-election-winner`
  - None of the above appeared in the live open Polymarket US market list from:
    `GET https://gateway.polymarket.us/v1/markets?limit=500&offset=0&closed=false&active=true&archived=false`

### Live overlap that exists now

#### House 2026

Kalshi:
- Event: `CONTROLH-2026`
- Market: `CONTROLH-2026-D`
  - Title: `Will Democrats win the House in 2026?`
  - Close: `2027-02-01T15:00:00Z`
  - Rule: `If the Democratic Party has won control of the House in 2026, then the market resolves to Yes.`
  - Tie-break: `Victory will be determined by the party identification of the Speaker of the House on February 1, 2027.`
- Market: `CONTROLH-2026-R`
  - Title: `Will Republicans win the House in 2026?`
  - Close: `2027-02-01T15:00:00Z`

Polymarket US:
- Slug: `paccc-usho-midterms-2026-11-03-dem`
  - Question: `U.S House Midterm Winner`
  - Description: `Will the Democratic Party win the House in the 2026 Midterms?`
  - End: `2027-02-01T23:59:00Z`
  - `active=true`, `closed=false`
- Slug: `paccc-usho-midterms-2026-11-03-rep`
  - Question: `U.S House Midterm Winner`
  - Description: `Will the Republican Party win the House in the 2026 Midterms?`
  - End: `2027-02-01T23:59:00Z`
  - `active=true`, `closed=false`

Assessment:
- High-confidence identical pair.
- Same control question, same party side split, same practical settlement day.
- Polymarket's public endpoint does not expose an explicit tie-break/source field, so final operator confirmation is still prudent before auto-trade.

#### Senate 2026

Kalshi:
- Event: `CONTROLS-2026`
- Market: `CONTROLS-2026-D`
  - Title: `Will Democrats win the U.S. Senate in 2026?`
  - Close: `2027-02-01T15:00:00Z`
  - Rule: `If the Democratic Party has won control of the U.S. Senate in 2026, then the market resolves to Yes.`
  - Tie-break: `Victory will be determined by the party identification of the President pro tempore of the Senate on February 1, 2027.`
- Market: `CONTROLS-2026-R`
  - Title: `Will Republicans win the U.S. Senate in 2026?`
  - Close: `2027-02-01T15:00:00Z`

Polymarket US:
- Slug: `paccc-usse-midterms-2026-11-03-dem`
  - Question: `U.S Senate Midterm Winner`
  - Description: `Will the Democratic Party win the Senate in the 2026 Midterms?`
  - End: `2027-02-01T23:59:00Z`
  - `active=true`, `closed=false`
- Slug: `paccc-usse-midterms-2026-11-03-rep`
  - Question: `U.S Senate Midterm Winner`
  - Description: `Will the Republican Party win the Senate in the 2026 Midterms?`
  - End: `2027-02-01T23:59:00Z`
  - `active=true`, `closed=false`

Assessment:
- High-confidence identical pair for the same reasons as House 2026.

## Non-matches checked

- Polymarket US has open NBA MVP markets such as `tec-nba-mvp-2026-06-10-shagil`, but Kalshi's open MVP event found during this audit was `KXNFLMVP-27` (NFL MVP, 2026-27 season), not NBA MVP.
- Polymarket US has open April 2026 Fed decision markets such as `rdc-usfed-fomc-2026-04-29-maintains`, but the open Kalshi Fed-decision events sampled during this audit were 2027+ dated, so I did not treat Fed as a current overlap candidate.
- The old Polymarket 2028 nomination slugs in `MARKET_SEEDS` currently return `market not found` on the Polymarket US public retail API.

## Recommended refresh candidates

If the goal is "at least one live identical tradable pair for go-live", the best current refresh set is:

- `HOUSE_DEM_2026`: `CONTROLH-2026-D` <-> `paccc-usho-midterms-2026-11-03-dem`
- `HOUSE_REP_2026`: `CONTROLH-2026-R` <-> `paccc-usho-midterms-2026-11-03-rep`
- `SENATE_DEM_2026`: `CONTROLS-2026-D` <-> `paccc-usse-midterms-2026-11-03-dem`
- `SENATE_REP_2026`: `CONTROLS-2026-R` <-> `paccc-usse-midterms-2026-11-03-rep`

Operational recommendation:
- Replace the stale hand seeds with one or more of the four pairs above.
- Keep them `status=review` / `allow_auto_trade=false` until operator confirmation is recorded, because Polymarket US public metadata does not expose full resolution-source/tie-break text.
