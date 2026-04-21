# OPENCLAW_ARCHITECTURE_MAP

## Runtime shape
- `arbiter/` — Python backend, live trading, API, safety, execution, collectors, scanner, mapping, readiness
- `src/` — TypeScript CLI and lightweight execution/matching path
- `arbiter/web/` + `index.html` — dashboard view-model and browser assets
- `scripts/setup/` — bootstrap, live checks, onboarding, go-live orchestration
- `deploy/` — docker-compose prod, systemd, logrotate, deployment docs
- `.planning/` — historical plan/research artifacts, partially stale

## Core live path
1. Collectors ingest Kalshi + Polymarket market data
2. `arbiter/scanner/` produces matched pairs / arbitrage opportunities
3. `arbiter/execution/` adapters and engine place/cancel/fill orders
4. `arbiter/safety/` and `arbiter/live/` enforce kill-switch, preflight, auto-abort, and supervised live-trade flow
5. `arbiter/api.py` and dashboard routes expose `/ops`, health, readiness, metrics, mappings, safety state
6. `arbiter/notifiers/` sends Telegram alerts / heartbeat

## Current canonical docs
- Current state: `HANDOFF.md`, `STATUS.md`
- Operator path: `HANDOFF.md` first, then `scripts/setup/*` + `.env.production.template`
- Historical design context: `.planning/`, `docs/superpowers/specs/*`, `docs/superpowers/plans/*`

## Verification entrypoints
- JS typecheck: `npm run typecheck`
- JS tests: `npm test`
- Python tests: `python3 -m pytest -q`
- Go-live orchestrator: `./scripts/setup/go_live.sh`
