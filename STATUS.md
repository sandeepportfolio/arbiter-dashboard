# Arbiter Dashboard — Current Status

Updated: 2026-04-22 01:50 PDT
Repo root: `/Users/rentamac/Documents/arbiter`

## Source of truth

Use these in order:
1. `STATUS.md` (this file)
2. `HANDOFF.md`
3. `GOLIVE.md`
4. `LIVE_MAPPING_AUDIT.md`

Older planning docs are useful background, but not the current operator truth unless revalidated.

## Git truth

- Branch: `main`
- Remote: `origin https://github.com/sandeepportfolio/arbiter-dashboard.git`
- Active work branch: `claude/gallant-meitner-243961` (worktree) — to be merged to `main` this cycle
- GitHub Pages: **live** at `https://sandeepportfolio.github.io/arbiter-dashboard/` (source: `main` branch root, HTTPS enforced)

## What is safely checked into git

Checked in on `main`:
- Arbiter Python runtime
- Dashboard frontend and operator desk UI (also served statically via GitHub Pages)
- Mapping expansion and auto-discovery code
- Runtime settings persistence, SQL helpers, startup scripts, and docs
- Templates such as `.env.production.template`
- Portable secrets export/import helpers
- `scripts/setup/provision_secrets.sh` — guided onboarding wrapper
- Live-state / handoff / go-live documentation

Intentionally **not** checked into git:
- `.env`
- `.env.production`
- `keys/kalshi_private.pem`
- Any live API keys, bot tokens, session secrets, or other auth material

That split is deliberate. The repo is portable; secrets stay private.

## Current verification snapshot

Fresh checks run on this worktree (2026-04-22):
- Python repo tests: `501 passed, 88 skipped`
- TypeScript typecheck: pass
- Vitest: `5 files, 40 tests` passed
- API smoke: pass
- `./scripts/ui-smoke.sh`: **pass**
- `./scripts/static-smoke.sh`: **pass**
- `make verify-full`: **GREEN** (quick-check → ui-smoke → static-smoke chain clean)

UI verification state:
- Public desk render: pass
- Mobile public layout smoke: pass
- Operator desk auth/render smoke: pass
- Log filter smoke: pass
- Mapping action smoke: pass (confirm-guard coverage split across `DEM_HOUSE_2026` pending-review fixture and `GOP_HOUSE_2026` enable-flow fixture)
- Operator settings save smoke: pass
- Cross-origin static shell smoke: pass

## Live trading defaults (production template)

`.env.production.template` now ships with execution-ready defaults so a fresh machine gets the intended scale out of the box. Tighten downward if a given operator wants a slower start.

| Variable | Template default | Meaning |
|----------|-----------------|---------|
| `AUTO_EXECUTE_ENABLED` | `true` | AutoExecutor is on by default (still gated by 7 policy gates) |
| `PHASE5_BOOTSTRAP_TRADES` | `1000` | AutoExecutor trade cap; readiness gate falls through normally for values > 5 |
| `AUTO_PROMOTE_ENABLED` | `true` | 8-condition auto-promote is on |
| `AUTO_PROMOTE_DAILY_CAP` | `500` | Up from 250 |
| `AUTO_PROMOTE_ADVISORY_SCANS` | `30` | Advisory window before promotion |
| `AUTO_PROMOTE_MAX_DAYS` | `400` | Promotion lookback window |
| `AUTO_DISCOVERY_INTERVAL_S` | `300` | Discovery loop period |
| `AUTO_DISCOVERY_BUDGET_RPS` | `2.0` | Discovery rate budget |
| `AUTO_DISCOVERY_MIN_SCORE` | `0.18` | Min match score for candidates |
| `AUTO_DISCOVERY_MAX_CANDIDATES` | `2500` | Up from 1500 |
| `PHASE5_MAX_ORDER_USD` | `10` | Adapter-layer hard-lock (defense in depth) |
| `MAX_POSITION_USD` | `10` | AutoExecutor position cap (must be <= PHASE5) |

## Current runtime / live state on this host

Current local machine state at the time of this update:
- No listener responding on `127.0.0.1:8080`
- No listener responding on `127.0.0.1:8090`
- No listener responding on `127.0.0.1:8100`
- `docker` is currently unavailable on this host
- Repo `.venv` is present and healthy on Python `3.12.12`

Practical meaning:
- The codebase is present and testable locally; every gate is green
- The live dashboard/runtime is **not currently running** on this machine
- A live bring-up from this host is still blocked until Docker is available again and private credentials are supplied locally

## Recent meaningful commits on `main`

- `6b2634d` `feat(arbiter): ship mapping expansion and ops dashboard updates`
- `1454bc9` `fix(live): normalize polymarket us readiness checks`
- `1f04522` `feat(ops): persist runtime settings and restore local verification`
- `77fcf44` `feat(setup): add portable secrets bundle workflow`
- `c8a800a` `docs(status): refresh current live state`

## What another machine needs

A second machine can fully reproduce the repo state from GitHub, but it still needs private credential material copied over out-of-band.

### Needed from git
```bash
git clone https://github.com/sandeepportfolio/arbiter-dashboard.git
cd arbiter-dashboard
```

### Needed privately, not from git
Copy these from a trusted machine using a secure channel (or use the portable bundle flow below):
- `.env.production`
- `keys/kalshi_private.pem`
- any other local-only operator secret files you actually rely on

Do **not** commit those files.

## New-machine bring-up checklist

```bash
cd arbiter-dashboard
./scripts/setup/bootstrap_python.sh
npm install
```

### Option A — guided provisioning (recommended)

```bash
./scripts/setup/provision_secrets.sh
```

This walks through: restore/copy `.env.production` → placeholder sweep → Kalshi PEM check →
`validate_env.py` → `check_kalshi_auth.py` → Polymarket variant check → `check_telegram.py`.
Pass `--no-input` for CI; set `PORTABLE_SECRETS_PASSPHRASE` in the environment if you want
the script to restore from the encrypted portable bundle non-interactively.

### Option B — manual

```bash
cp .env.production.template .env.production
# edit placeholders, then:
python scripts/setup/validate_env.py
python scripts/setup/check_kalshi_auth.py
python scripts/setup/check_polymarket_us.py
python scripts/setup/check_telegram.py
```

### Portable bundle flow (across machines)

```bash
# source machine
export PORTABLE_SECRETS_PASSPHRASE='choose-a-strong-passphrase'
./scripts/setup/export_portable_secrets.sh

# destination machine after git clone
export PORTABLE_SECRETS_PASSPHRASE='the-same-passphrase'
./scripts/setup/import_portable_secrets.sh
```

If Docker is available on the destination machine:

```bash
./scripts/setup/go_live.sh
```

## Current blocker list

1. Secrets are intentionally not in git, so another machine still needs a private secret handoff (portable bundle flow exists)
2. Docker is unavailable on this host, so live stack bring-up cannot happen here right now

## Recommendation

Treat `main` as the correct code-and-docs source of truth.
The verification chain is green — `make verify-full` passes end-to-end, live trading defaults are shipped in the template, GitHub Pages dashboard is live, and provisioning is guided.
Use the portable bundle (or a secure private transfer) for live secret files, then run `provision_secrets.sh` on the destination before `go_live.sh`.
