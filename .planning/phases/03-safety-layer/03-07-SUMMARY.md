---
phase: 03-safety-layer
plan: 07
subsystem: ui
tags: [dashboard, kill-switch, websocket, playwright, vitest, xss-safety]

requires:
  - phase: 03-01
    provides: state.safety.killSwitch WS payload + POST /api/kill-switch endpoints
  - phase: 03-03
    provides: state.oneLegExposures WS event stream
  - phase: 03-04
    provides: state.safety.rateLimits WS event + /api/system.rate_limits snapshot
  - phase: 03-05
    provides: state.shutdown WS event (phase shutting_down / complete)
  - phase: 03-06
    provides: state.mappingUpdates + resolution_criteria schema
provides:
  - Safety section above command center (kill-switch ARM/RESET, cooldown countdown, rate-limit pill grid)
  - One-leg hero alert panel with pulsing border + Acknowledge flow
  - Shutdown banner that suppresses WS auto-reconnect after phase=complete
  - Side-by-side resolution-criteria comparison on mapping cards
  - buildSafetyView / buildRateLimitView / buildMappingComparison pure-function view-model helpers
  - Playwright smoke at output/verify_safety_ui.mjs
affects: [phase-04, operator-ux, safety]

tech-stack:
  added: []
  patterns:
    - "Pure view-model helpers consumed by thin render functions (pattern extended from buildDeskOverview)"
    - "XSS guard: operator-entered text rendered via textContent, never innerHTML (T-3-06-C carry-over)"
    - "Playwright smoke with --dry-run escape hatch so the script is importable without Playwright installed"

key-files:
  created:
    - arbiter/web/dashboard-view-model.js (extended with 3 new exports)
    - output/verify_safety_ui.mjs
  modified:
    - arbiter/web/dashboard.html
    - arbiter/web/dashboard.js
    - arbiter/web/dashboard-view-model.js
    - arbiter/web/dashboard-view-model.test.js
    - arbiter/web/styles.css
    - index.html
    - .planning/phases/03-safety-layer/deferred-items.md

key-decisions:
  - "Kept index.html (root static variant) and dashboard.html (aiohttp-served variant) in parity via identical safety markup blocks — avoids drift between the two entry points"
  - "Deferred a pre-existing buildMetricCards label-drift vitest failure (documented in deferred-items.md) — scope boundary, not caused by this plan"
  - "Reset button disabled-state computed from cooldown_until minus Date.now()/1000 in view-model, not server-polled — removes a WS round-trip while keeping the badge authoritative"

patterns-established:
  - "Pattern 1: Pure view-model helper → render function → click-handler wiring using existing postJson + runAction (kill-switch flow is the canonical example)"
  - "Pattern 2: Dedicated WS event types (kill_switch, one_leg_exposure, rate_limit_state, shutdown_state, mapping_state) each flip a specific state slice, not a global rerender"

requirements-completed: [SAFE-01, SAFE-02, SAFE-03, SAFE-04, SAFE-05, SAFE-06]

duration: 10min
completed: 2026-04-16
---

# Phase 3: Safety UI Consolidation Summary

**Operator-facing dashboard for the entire Phase 3 safety surface — kill-switch controls, rate-limit pills, one-leg hero alert, shutdown banner, and side-by-side resolution-criteria comparison all wired to existing WS event streams and REST endpoints.**

## Performance

- **Duration:** ~10 min (executor wall-clock)
- **Tasks:** 3 (Task 2 was the human-verify checkpoint — operator approved via `proceed`)
- **Files modified:** 7
- **New lines:** ~1,040

## Accomplishments

- `<section id="safetySection">` above command center with kill-switch ARM/RESET buttons, status badge, cooldown countdown, and rate-limit pill grid
- One-leg hero alert panel that becomes visible when an `one_leg_exposure` WS event arrives, with filled-leg details, recommended unwind, and an Acknowledge button that hides the banner
- `#shutdownBanner` top-of-page banner; WS close handler does NOT auto-reconnect after `phase=complete`
- Mapping cards gained a side-by-side Kalshi-vs-Polymarket resolution-criteria grid with a `criteria-chip` match-status indicator
- `buildSafetyView`, `buildRateLimitView`, `buildMappingComparison` pure helpers in dashboard-view-model.js; 9 new vitest cases all green
- Playwright smoke at `output/verify_safety_ui.mjs` with `--dry-run` exit path so it doesn't require Playwright to be installed just to smoke-test the script itself

## Task Commits

1. **Task 0: Wave-0 vitest red cases + Playwright smoke skeleton** — `880c0a8` (test)
2. **Task 1: view-model helpers + DOM markup + render functions + click handlers + CSS** — `e4c3411` (feat)
3. **Task 2: human-verify checkpoint** — operator responded `proceed`, approving the UI smoke sequence

**Plan metadata:** this SUMMARY (docs: complete plan)

## Files Created/Modified

- `arbiter/web/dashboard.html` — safetySection, shutdownBanner, mapping-compare-grid markup
- `arbiter/web/dashboard.js` — renderSafetyPanel / renderRateLimitBadges / renderOneLegAlert / renderShutdownBanner / renderMappingResolutionCompare + click handlers
- `arbiter/web/dashboard-view-model.js` — buildSafetyView / buildRateLimitView / buildMappingComparison
- `arbiter/web/dashboard-view-model.test.js` — 9 new vitest cases
- `arbiter/web/styles.css` — .kill-switch-controls, .rate-limit-grid, .one-leg-alert-panel, @keyframes one-leg-pulse, .shutdown-banner, .mapping-compare-grid, .criteria-chip
- `index.html` — safety markup in parity with dashboard.html
- `output/verify_safety_ui.mjs` — Playwright smoke

## Decisions Made

See key-decisions in frontmatter.

## Deviations from Plan

None — plan executed as written. The only scope trim was deferring the pre-existing `buildMetricCards` label-drift vitest failure to `deferred-items.md` (not caused by this plan).

## Issues Encountered

- User had pre-existing dashboard polish WIP (~562 insertions across 5 files) — orchestrator committed WIP first at `62220e2` so the 03-07 executor built on top of the latest intent rather than overwriting uncommitted work.

## User Setup Required

None — no external service configuration needed.

## Next Phase Readiness

- Phase 3 safety surface is fully wired and operator-visible
- Dashboard ready for Phase 4 (live trading readiness) to consume the same event channels
- `verify_safety_ui.mjs` can be re-run any time to smoke-check the safety UI

---
*Phase: 03-safety-layer*
*Completed: 2026-04-16*
