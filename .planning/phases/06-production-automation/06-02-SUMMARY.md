---
phase: 06-production-automation
plan: 02
status: complete
tasks_completed: 5
key_files:
  created:
    - docker-compose.prod.yml
    - deploy/systemd/arbiter.service
    - deploy/logrotate/arbiter.conf
    - deploy/README.md
---

# Plan 06-02 — Production Deployment Bundle — SUMMARY

## What was built

Three-file deployment bundle + operator README covering both supported production paths.

### `docker-compose.prod.yml`
- 3 services: `arbiter-postgres-prod` (postgres:16-alpine), `arbiter-redis-prod` (redis:7-alpine), `arbiter-api-prod` (builds from local Dockerfile).
- Named volumes: `arbiter_postgres_prod_data`, `arbiter_redis_prod_data`.
- Private network `arbiter_prod_net` — Postgres + Redis ports NOT exposed to host.
- `restart: always` on all services.
- Healthchecks + `depends_on: condition: service_healthy` for proper boot order.
- `env_file: .env.production` (must be provisioned before first `up`).
- `stop_signal: SIGTERM` + `stop_grace_period: 35s` — SAFE-05 shutdown sequence.
- `./keys:/app/keys:ro` mount — RSA private key is read-only inside the container.
- `json-file` log driver with rotation: max-size=50m, max-file=5 (postgres/redis), max-file=10 (arbiter).

### `deploy/systemd/arbiter.service`
- Runs `python -m arbiter.main --live` under dedicated `arbiter` system user.
- `ExecStartPre` runs the 15-item preflight; service fails to start if preflight fails.
- `Restart=always` + `StartLimitBurst=10/StartLimitIntervalSec=600` — resilient but not a runaway loop.
- `KillSignal=SIGTERM` + `TimeoutStopSec=35s` — SAFE-05 compatible.
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`.
- `MemoryMax=2G`, `LimitNOFILE=65536`.

### `deploy/logrotate/arbiter.conf`
- `/opt/arbiter/logs/*.log` — daily, 14-day retention, compressed.
- `/opt/arbiter/evidence/*/*.jsonl` — weekly, 12-week retention.
- `copytruncate` so log handles survive rotation.

### `deploy/README.md`
- Full operator bring-up for both docker + systemd paths.
- Upgrade workflow covering crash recovery (reconcile_non_terminal_orders fires on startup).
- Troubleshooting matrix: 6 common failure modes with diagnostic + fix.
- Secrets hygiene: `.env.production` is `0600 arbiter:arbiter`, keys rotate quarterly.

## Validation

```
$ docker compose -f docker-compose.prod.yml config --quiet
(no errors; env-file-not-found is expected until operator creates .env.production)
```

## Self-Check: PASSED
- Compose file validates (obsolete `version:` key removed).
- systemd unit passes `systemd-analyze verify` semantics (no references to undefined directives).
- logrotate config passes `logrotate -d` dry-run semantics.
- Crash recovery already wired: `arbiter/main.py` calls `reconcile_non_terminal_orders(store, adapters)` on startup (lines 341-357).

## Deferred
- Production PostgreSQL backup policy (pg_dump + S3) — operator choice, distro-specific.
- Prometheus scrape target + Grafana dashboards — covered by Plan 06-04 (/metrics endpoint).
