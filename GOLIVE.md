# Arbiter, Go-Live Operator Runbook

**Current source of truth for go-live.** This runbook reflects the completed Polymarket US pivot. It supersedes older CLOB wallet instructions found in historical planning docs.

**Also trust:** `HANDOFF.md` for full context, `STATUS.md` for the latest verification snapshot.

**Host caveat:** `CLAUDE.md` and some planning docs still mention `/gsd-*` entrypoints. Those commands are not available on this host. Use the concrete shell commands in this file instead.

---

## 1. What you need before starting

- Docker + Docker Compose v2
- A funded Kalshi production account with API access
- A Polymarket US account with KYC completed and access to `polymarket.us/developer`
- Telegram for operator alerts
- About 1 to 2 hours for credential setup and the first supervised live trade

This repo now targets **Polymarket US** by default:
- API base: `api.polymarket.us/v1`
- Auth: Ed25519 API key ID + base64 secret
- Legacy `clob.polymarket.com` support remains only behind `POLYMARKET_VARIANT=legacy`

---

## 2. Human-only provisioning

### 2A. Kalshi production

1. Log into `https://kalshi.com`.
2. Complete KYC if needed.
3. Fund the account, at least ~$100 recommended for first supervised testing.
4. Create a production API key.
5. Save the private key to `./keys/kalshi_private.pem`.
6. Lock permissions:

```bash
chmod 600 ./keys/kalshi_private.pem
```

### 2B. Polymarket US production

1. Complete Polymarket US KYC.
2. Open `https://polymarket.us/developer`.
3. Generate a developer API key.
4. Capture:
   - `POLYMARKET_US_API_KEY_ID`
   - `POLYMARKET_US_API_SECRET` (base64 Ed25519 secret)
5. Fund the Polymarket US account, at least ~$20 recommended.

Optional helper:

```bash
python scripts/setup/onboard_polymarket_us.py
```

It opens a browser and can capture the key material after you authenticate. The script is designed not to print the secret.

### 2C. Telegram

1. Use `@BotFather` to create a bot.
2. Use `@userinfobot` to get your numeric chat ID.
3. Message the bot once so it can DM you later.

### 2D. Dashboard session secret

```bash
openssl rand -hex 32
```

Use that value for `UI_SESSION_SECRET`.

---

## 2E. Optional cross-machine portability bundle

If you want a second machine to restore the same local live secrets quickly,
create an encrypted bundle on the source machine:

```bash
export PORTABLE_SECRETS_PASSPHRASE='choose-a-strong-passphrase'
./scripts/setup/export_portable_secrets.sh
```

That produces:

```text
portable-secrets/arbiter-portable-secrets.tgz.enc
```

After cloning on another machine, restore it with:

```bash
export PORTABLE_SECRETS_PASSPHRASE='the-same-passphrase'
./scripts/setup/import_portable_secrets.sh
```

This restores local secret files like `.env.production` and `keys/kalshi_private.pem`
without committing them in raw form.

## 3. Fill `.env.production`

```bash
cp .env.production.template .env.production
chmod 600 .env.production
```

Fill every placeholder.

Important current values:

```bash
DRY_RUN=false
POLYMARKET_VARIANT=us

KALSHI_API_KEY_ID=<from Kalshi>
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_private.pem

POLYMARKET_US_API_KEY_ID=<from polymarket.us/developer>
POLYMARKET_US_API_SECRET=<base64 secret>

TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHAT_ID=<your numeric chat id>
UI_SESSION_SECRET=<openssl rand -hex 32>
```

Notes:
- The legacy env vars (`POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLYMARKET_CLOB_URL`) are only for `POLYMARKET_VARIANT=legacy`.
- Leave `AUTO_EXECUTE_ENABLED=false` for the first supervised trade.
- After editing, verify no placeholder markers remain:

```bash
grep -n '<' .env.production
```

Expected: no output.

---

## 4. One-shot bring-up and preflight

Run the orchestrator:

```bash
./scripts/setup/go_live.sh
```

It stops on first failure and runs, in order:
1. `validate_env.py`
2. `docker compose -f docker-compose.prod.yml up -d`
3. `check_kalshi_auth.py`
4. `check_polymarket_us.py` for `POLYMARKET_VARIANT=us` or `check_polymarket.py` for `legacy`
5. `check_telegram.py`
6. `check_mapping_ready.py`
7. `python -m arbiter.live.preflight` with `PREFLIGHT_ALLOW_LIVE=1`

Expected result: all checks pass and the script ends with the success banner.

Common fixes:
- `401` from `check_polymarket_us.py`: key ID and base64 secret do not match
- Telegram check fails: bot token/chat ID wrong, or you never messaged the bot
- Mapping check fails: confirm and enable at least one pair in `/ops`

---

## 5. Curate at least one live mapping

Open:

- `http://localhost:8080/ops`

In **Settings**:
- Optionally tune non-secret runtime knobs like scanner edge floor, alert cooldowns, and auto-executor caps.
- Secrets, exchange credentials, and the final live-mode flip still stay outside the UI on purpose.

In **Mappings**:
1. Review a candidate pair.
2. Confirm the resolution criteria are genuinely identical.
3. Click **Confirm**.
4. Click **Enable auto-trade**.

Default behavior:
- `AUTO_PROMOTE_ENABLED=false` means new candidates stay operator-reviewed.
- If you later enable auto-promote, the 8-condition gate in `arbiter/mapping/auto_promote.py` still applies.

---

## 6. First supervised live trade

Keep `/ops` open with the kill switch visible, then run:

```bash
docker compose -f docker-compose.prod.yml exec arbiter-api-prod \
  pytest -m live --live arbiter/live/test_first_live_trade.py -v -s
```

Sequence:
1. Preflight runs.
2. A pre-trade requote artifact is written.
3. You get a 60-second abort window.
4. If you do not abort, both legs fire with the configured micro-cap.
5. Reconciliation runs after settlement.
6. Reconcile breach should auto-trip the kill switch.

Pass condition:
- either the trade reconciles within tolerance, or
- the auto-abort path fires correctly on breach

If neither happens cleanly, stop and investigate before enabling auto mode.

---

## 7. Flip to auto-mode

After the supervised trade is clean:

```bash
# edit .env.production
AUTO_EXECUTE_ENABLED=true

docker compose -f docker-compose.prod.yml restart arbiter-api-prod
docker compose -f docker-compose.prod.yml logs -f arbiter-api-prod | grep auto_executor
```

Watch for the first hour:
- `/ops` stays green
- `curl http://localhost:8080/api/metrics | grep auto_executor`
- Telegram heartbeat arrives every 15 minutes while auto-exec is enabled
- Kalshi and Polymarket US balances match the dashboard view

Bootstrap remains capped by `PHASE5_BOOTSTRAP_TRADES=5` until you remove it.

---

## 8. Optional scale-up

Only after several clean hours:

```bash
# .env.production
PHASE5_BOOTSTRAP_TRADES=
AUTO_PROMOTE_ENABLED=true
AUTO_PROMOTE_DAILY_CAP=20
AUTO_PROMOTE_ADVISORY_SCANS=30
```

Then restart:

```bash
docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

---

## 9. Rollback

Fast config-only rollback:

```bash
# .env.production
POLYMARKET_VARIANT=legacy
# or
POLYMARKET_VARIANT=disabled

docker compose -f docker-compose.prod.yml restart arbiter-api-prod
```

- `legacy` switches back to `clob.polymarket.com`
- `disabled` runs Kalshi-only

No code revert is required.

---

## 10. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `/api/readiness` is blocked | Inspect `blocking_reasons[]` | Fix each blocker, then rerun preflight |
| Polymarket US auth fails | `check_polymarket_us.py` output | Regenerate or re-copy key ID + base64 secret |
| Telegram stays silent | `python -m arbiter.notifiers.telegram` in container | Fix bot token/chat ID or message the bot first |
| No mapping ready | `/ops` → Mappings | Confirm one pair and enable auto-trade |
| AutoExecutor never fires | `/api/metrics` skipped reasons | Usually `disabled`, `not_allowed`, or `over_cap` |
| Kill-switch remains armed | `/api/safety/status` | Wait cooldown, then reset |
| Crash leaves orders hanging | restart logs for `reconcile.started` | Startup reconcile should cancel/recover leftovers |

---

## 11. Secrets hygiene

- Never commit `.env.production` or `keys/*.pem`
- Keep `chmod 600 .env.production keys/*.pem`
- Do not print or paste `POLYMARKET_US_API_SECRET` into logs or chat
- If a trading secret leaks, arm the kill switch, revoke the credential, and rotate it before resuming
- The legacy Polygon wallet keys are not needed for the default US path
