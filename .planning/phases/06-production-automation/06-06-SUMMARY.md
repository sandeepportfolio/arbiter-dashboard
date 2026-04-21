---
phase: 06-production-automation
plan: 06
status: complete
key_files:
  created:
    - GOLIVE.md
---

# Plan 06-06 — GOLIVE.md Operator Runbook — SUMMARY

## What was built

`GOLIVE.md` at repo root — a 13-section end-to-end operator guide covering the path from a clean checkout to 24×7 automated live trading.

### Section map
| § | Topic |
|---|---|
| 0 | Safety architecture: 7 independent gate layers |
| 1 | Credentials operator must provision (Kalshi, Polymarket, Telegram, dashboard pw) |
| 2 | `.env.production` template walkthrough |
| 3 | Docker stack bring-up + health verification |
| 4 | Telegram dry-test |
| 5 | 15-item preflight |
| 6 | MARKET_MAP curation (pick a pair, Enable auto-trade) |
| 7 | First supervised live trade (Plan 05-02 live-fire) |
| 8 | Flip `AUTO_EXECUTE_ENABLED=true` and monitor |
| 9 | Lift bootstrap cap |
| 10 | Rollback procedures (kill-switch, graceful, hard stop) |
| 11 | Troubleshooting matrix (10 symptom rows) |
| 12 | Secrets hygiene |
| 13 | When to stop and investigate |

## Self-Check: PASSED
- Covers all 6 preceding Phase 6 plans + Phase 5 live-fire gate
- Every command is copy-pasteable
- 10-row troubleshooting matrix mirrors real failure modes we've seen across Phase 4 UAT
- Clearly names what I (Claude) cannot do: Section 1 ("Credentials you must provision yourself — I cannot automate these")
