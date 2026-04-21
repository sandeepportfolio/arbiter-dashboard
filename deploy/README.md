# Arbiter Production Deployment

Two supported paths: **docker-compose** (recommended, self-contained) or **systemd** (bare-metal, integrates with distro tooling).

Both paths assume:
- `.env.production` has been populated from `.env.production.template`
- `./keys/kalshi_private.pem` exists (NOT committed; referenced by `KALSHI_PRIVATE_KEY_PATH`)
- default venue config is `POLYMARKET_VARIANT=us` with Polymarket US API credentials (`POLYMARKET_US_API_KEY_ID`, `POLYMARKET_US_API_SECRET`)
- Postgres + Redis are reachable (managed by compose, or provisioned externally for systemd)

---

## Path 1: docker-compose

Single file: `docker-compose.prod.yml`. Includes Postgres 16 (alpine), Redis 7 (alpine), and the arbiter container.

### Bring up

```bash
# 1. Provision secrets
cp .env.production.template .env.production
# Fill in all <placeholder> values, especially:
#   KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (= ./keys/kalshi_private.pem)
#   POLYMARKET_VARIANT=us
#   POLYMARKET_US_API_KEY_ID, POLYMARKET_US_API_SECRET
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#   UI_SESSION_SECRET (openssl rand -hex 32)

# 2. Launch
docker compose -f docker-compose.prod.yml --env-file .env.production up -d

# 3. Verify health
curl http://localhost:8080/api/health       # {"status":"ok"}
curl http://localhost:8080/api/readiness    # {"ready": true, "checks": {...}}

# 4. Tail logs
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod

# 5. Dry Telegram test
docker compose -f docker-compose.prod.yml exec arbiter-api-prod python -m arbiter.notifiers.telegram

# 6. Run preflight
docker compose -f docker-compose.prod.yml exec arbiter-api-prod python -m arbiter.live.preflight
```

### Tear down (graceful)

```bash
docker compose -f docker-compose.prod.yml down
# SAFE-05: SIGTERM sent → arbiter.main runs run_shutdown_sequence
# (cancels open orders, 5s trip_kill budget + 30s grace).
```

### Inspect state

```bash
# Running containers
docker compose -f docker-compose.prod.yml ps

# Exec into the arbiter container
docker compose -f docker-compose.prod.yml exec arbiter-api-prod bash

# Read Postgres directly
docker compose -f docker-compose.prod.yml exec arbiter-postgres-prod \
  psql -U arbiter -d arbiter_live -c "SELECT count(*) FROM execution_orders;"

# Flush Redis (only if you know why)
docker compose -f docker-compose.prod.yml exec arbiter-redis-prod redis-cli FLUSHDB
```

---

## Path 2: systemd (bare-metal)

Assumes Postgres + Redis are already installed as system services.

### One-time setup

```bash
# Create dedicated system user
sudo useradd --system --shell /bin/false --home /opt/arbiter arbiter

# Install to /opt/arbiter
sudo mkdir -p /opt/arbiter /etc/arbiter /var/log/arbiter
sudo chown arbiter:arbiter /opt/arbiter /var/log/arbiter

# Clone + install
sudo -u arbiter git clone https://github.com/sandeepportfolio/arbiter-dashboard /opt/arbiter
sudo -u arbiter python3.12 -m venv /opt/arbiter/.venv
sudo -u arbiter /opt/arbiter/.venv/bin/pip install -r /opt/arbiter/requirements.txt

# Copy production env to /etc (restrictive perms)
sudo install -m 0600 -o arbiter -g arbiter .env.production /etc/arbiter/arbiter.env

# Copy Kalshi RSA key
sudo install -m 0600 -o arbiter -g arbiter keys/kalshi_private.pem /opt/arbiter/keys/kalshi_private.pem

# Install DB schema
sudo -u postgres createdb arbiter_live
sudo -u postgres psql arbiter_live < /opt/arbiter/arbiter/sql/init.sql

# Install systemd unit
sudo cp /opt/arbiter/deploy/systemd/arbiter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arbiter

# Install log rotation
sudo cp /opt/arbiter/deploy/logrotate/arbiter.conf /etc/logrotate.d/arbiter
sudo logrotate -d /etc/logrotate.d/arbiter      # dry-run validation
```

### Operation

```bash
sudo systemctl status arbiter     # running? last restart?
journalctl -u arbiter -f          # tail logs
sudo systemctl stop arbiter       # graceful SIGTERM (SAFE-05)
sudo systemctl restart arbiter    # full cycle (handles crash recovery on startup)
```

---

## Upgrade workflow (both paths)

```bash
cd /opt/arbiter && sudo -u arbiter git pull origin main
sudo -u arbiter /opt/arbiter/.venv/bin/pip install -r requirements.txt

# docker: rebuild image + recreate container
docker compose -f docker-compose.prod.yml build arbiter-api-prod
docker compose -f docker-compose.prod.yml up -d arbiter-api-prod

# systemd: restart service
sudo systemctl restart arbiter
```

On startup, `arbiter.main` calls `reconcile_non_terminal_orders` to reconcile any orders left hanging by a previous crash.

---

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| Container never passes healthcheck | `docker logs arbiter-api-prod` | Missing env var or unreadable key file — verify `.env.production` + `./keys/kalshi_private.pem` |
| `/api/readiness` returns `ready: false` | Body shows which check failed | Fix the blocker listed in `blockers[]` |
| Auto-execute never fires | `/api/metrics` → `auto_executor_stats` | `AUTO_EXECUTE_ENABLED=false` or no mapping has `allow_auto_trade=true` — flip env + curate mapping in /ops |
| Kill-switch ARMED and stuck | `/api/safety/status` | Wait `cooldown_remaining` seconds, then POST `/api/kill-switch {"action":"reset"}` |
| Telegram alerts silent | `docker compose exec arbiter-api-prod python -m arbiter.notifiers.telegram` | If dry-test fails, regenerate `TELEGRAM_BOT_TOKEN` via @BotFather |
| Orders stuck SUBMITTED after crash | `arbiter.main` logs `reconcile.started` on next boot | Restart — automatic reconcile runs on startup |

---

## Secrets hygiene

- `.env.production` and `keys/*.pem` are **git-ignored** by `.gitignore`. Never commit them.
- On the server, `/etc/arbiter/arbiter.env` should be `0600` owned by `arbiter:arbiter`.
- Rotate `UI_SESSION_SECRET` on a cadence; active sessions are invalidated on restart.
- Kalshi API key: regenerate quarterly via the Kalshi account portal.
- Polymarket US API secret: treat like a production trading credential, never print it to logs/chat, rotate immediately if exposed.
- Legacy Polygon wallet secrets only matter if you intentionally run `POLYMARKET_VARIANT=legacy`.
