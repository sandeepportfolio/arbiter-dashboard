# OPENCLAW_STATUS

Updated: 2026-04-22 00:58 PDT
Repo root: `/Users/rentamac/Documents/arbiter`

## Short version
- Branch: `main`
- Remote: `origin https://github.com/sandeepportfolio/arbiter-dashboard.git`
- HEAD before this doc refresh: `6b2634d`
- Divergence: `0 ahead / 0 behind`
- Code + docs are in git
- Live secrets are **not** in git by design

## Verification snapshot
- Python tests: `501 passed, 88 skipped`
- TypeScript typecheck: pass
- Vitest: `40 passed`
- API smoke: pass
- UI smoke: currently failing on mapping confirm guard

## Runtime snapshot
- No service responding on `127.0.0.1:8080`, `8090`, or `8100`
- `docker` unavailable on this host
- Repo `.venv` present and healthy (`Python 3.12.12`)

## Portability truth
Another machine can pull the full codebase from `main`, but it still needs private out-of-band copies of:
- `.env.production`
- `keys/kalshi_private.pem`
- any other live secret material

## Operator note
For the detailed handoff and current-state narrative, read `STATUS.md` first.
