# Arbiter Dashboard — Current Status

Updated: 2026-04-22 00:58 PDT
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
- Current HEAD before this status-doc commit: `6b2634d` (`feat(arbiter): ship mapping expansion and ops dashboard updates`)
- Divergence vs `origin/main`: `0 ahead / 0 behind`
- Working tree before this status-doc update: clean

## What is safely checked into git

Checked in on `main`:
- Arbiter Python runtime
- Dashboard frontend and operator desk UI
- Mapping expansion and auto-discovery code
- Runtime settings persistence, SQL helpers, startup scripts, and docs
- Templates such as `.env.production.template`
- Live-state / handoff / go-live documentation

Intentionally **not** checked into git:
- `.env`
- `.env.production`
- `keys/kalshi_private.pem`
- Any live API keys, bot tokens, session secrets, or other auth material

That split is deliberate. The repo is portable; secrets stay private.

## Current verification snapshot

Fresh checks run on this machine tonight:
- Python repo tests: `501 passed, 88 skipped`
- TypeScript typecheck: pass
- Vitest: `5 files, 40 tests` passed
- API smoke: pass

UI verification state:
- Public desk render: pass
- Mobile public layout smoke: pass
- Operator desk auth/render smoke: pass
- Log filter smoke: pass
- Mapping action smoke: **currently red**
  - `./scripts/ui-smoke.sh` reports: `mapping confirm guard did not disable an unsafe confirm action`
  - Because of that, `make verify-full` is not green right now
  - `./scripts/static-smoke.sh` was not re-run after this failure because the full chain stopped at the UI smoke failure

## Current runtime / live state on this host

Current local machine state at the time of this update:
- No listener responding on `127.0.0.1:8080`
- No listener responding on `127.0.0.1:8090`
- No listener responding on `127.0.0.1:8100`
- `docker` is currently unavailable on this host (`docker: unavailable`)
- Repo `.venv` is present and healthy on Python `3.12.12`

Practical meaning:
- The codebase is present and testable locally
- The live dashboard/runtime is **not currently running** on this machine
- A live bring-up from this host is still blocked until Docker is available again and private credentials are supplied locally

## Recent meaningful commits already on `main`

- `6b2634d` `feat(arbiter): ship mapping expansion and ops dashboard updates`
- `1454bc9` `fix(live): normalize polymarket us readiness checks`
- `1f04522` `feat(ops): persist runtime settings and restore local verification`

## What another machine needs

A second machine can fully reproduce the repo state from GitHub, but it still needs private credential material copied over out-of-band.

### Needed from git
```bash
git clone https://github.com/sandeepportfolio/arbiter-dashboard.git
cd arbiter-dashboard
```

### Needed privately, not from git
Copy these from a trusted machine using a secure channel:
- `.env.production`
- `keys/kalshi_private.pem`
- any other local-only operator secret files you actually rely on

Do **not** commit those files.

## New-machine bring-up checklist

```bash
cd arbiter-dashboard
./scripts/setup/bootstrap_python.sh
npm install
cp .env.production.template .env.production   # then replace placeholders from your secure copy if needed
```

Then restore the real secret files privately and verify:

```bash
python scripts/setup/validate_env.py
python scripts/setup/check_kalshi_auth.py
python scripts/setup/check_polymarket_us.py
python scripts/setup/check_telegram.py
```

If Docker is available on the destination machine:

```bash
./scripts/setup/go_live.sh
```

## Current blocker list

1. Secrets are intentionally not in git, so another machine still needs a private secret handoff
2. Docker is unavailable on this host, so live stack bring-up cannot happen here right now
3. UI smoke currently catches a mapping-confirm guard issue, so `make verify-full` is not fully green

## Recommendation

Treat `main` as the correct code-and-docs source of truth.
Use a secure private transfer for the live secret files.
Before trading from any machine, fix the mapping-confirm guard regression and re-run the full verification chain.
