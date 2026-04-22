# `scripts/setup/` — Pre-Go-Live Validators

Six scripts that check the production setup step by step. Use `go_live.sh`
to run them all in sequence, or invoke individually to diagnose a specific
failure.

This setup path now targets **Polymarket US** by default. Legacy
`check_polymarket.py` remains only for `POLYMARKET_VARIANT=legacy`.

## Local Python bootstrap for verification

For a clean local test environment, create the repo virtualenv first:

    ./scripts/setup/bootstrap_python.sh

That bootstraps a local `.venv` with the supported Python 3.12 runtime (via
`uv` when available, otherwise `python3.12`) plus the repo's Python test
requirements. After that, `make test` and `make verify-quick` will prefer the
repo `.venv` automatically.

## Portable secrets for a second machine

To move the exact live secret setup to another machine without committing raw secrets,
use the encrypted portability bundle helpers:

    export PORTABLE_SECRETS_PASSPHRASE='choose-a-strong-passphrase'
    ./scripts/setup/export_portable_secrets.sh

That produces `portable-secrets/arbiter-portable-secrets.tgz.enc`.
After cloning on the destination machine:

    export PORTABLE_SECRETS_PASSPHRASE='the-same-passphrase'
    ./scripts/setup/import_portable_secrets.sh

See `portable-secrets/README.md` for the full workflow.

## Design rules

- **Never print secrets.** Every script masks private keys + tokens. What they
  print: presence, length, address, balance, public metadata.
- **Idempotent.** Safe to re-run. Nothing writes state outside `evidence/` or
  `logs/`.
- **Exit codes are contract.** `0 = PASS`, `1 = FAIL`, `2 = EXCEPTION` (bot
  error, missing dependency). Use these in CI or in chained shell pipelines.

## Script reference

### `validate_env.py` — shape + sanity of `.env.production`
Runs before any network I/O. Catches: template leftovers (`<placeholder>`),
demo URLs in production fields, wrong-length hex, missing required vars.

    set -a; source .env.production; set +a
    ./.venv/bin/python scripts/setup/validate_env.py

Typical PASS output: `✓ .env.production shape + sanity OK` + a per-var PASS
table. Typical FAIL: one of:
- `still contains a template placeholder`
- `looks like a demo/sandbox value — Phase 5 requires production`
- `file not found at ./keys/kalshi_private.pem`
- `wrong length (expected 64 hex chars, got N)`

### `check_kalshi_auth.py` — signed round-trip vs Kalshi prod
Uses the existing `arbiter.collectors.kalshi.KalshiAuth` to sign a
`GET /portfolio/balance` request and reads the response.

    ./.venv/bin/python scripts/setup/check_kalshi_auth.py

PASS prints the account balance. FAIL prints HTTP status + likely cause
(key-mismatch, clock-skew, or wrong base URL).

### `check_polymarket_us.py` — Polymarket US API credential check
Verifies the Polymarket US API key ID + base64 Ed25519 secret against the
current `api.polymarket.us/v1` flow.

    ./.venv/bin/python scripts/setup/check_polymarket_us.py

PASS prints a safe round-trip result and account balance metadata without
ever printing the secret. FAIL usually means key/secret mismatch, missing
funding, or a bad API URL.

### `check_polymarket.py` — legacy CLOB wallet auth
This is only for `POLYMARKET_VARIANT=legacy`.

    ./.venv/bin/python scripts/setup/check_polymarket.py

### `check_telegram.py` — bot dry-test
Thin wrapper around `python -m arbiter.notifiers.telegram`. Sends one test
message "🧪 Arbiter Telegram dry-test" to the configured chat.

    ./.venv/bin/python scripts/setup/check_telegram.py

PASS: exit 0 + message delivered to your Telegram. FAIL: exit 1 (disabled/
bad token) or 2 (exception).

### `check_mapping_ready.py` — MARKET_MAP readiness
Prints every mapping with its status + `allow_auto_trade` flag. PASS when
≥1 mapping has `status=confirmed` AND `allow_auto_trade=true` AND
`resolution_match_status=identical`.

    ./.venv/bin/python scripts/setup/check_mapping_ready.py

FAIL when no mapping meets all three criteria. Operator must open
http://localhost:8080/ops, Mappings panel, and curate one pair.

### `go_live.sh` — orchestrator (runs everything)

    ./scripts/setup/go_live.sh

Runs in order, stopping on first failure:

1. Precondition check (`.env.production`, `keys/kalshi_private.pem`, docker)
2. `validate_env.py`
3. `docker compose -f docker-compose.prod.yml up -d`
4. Wait 20s + poll `/api/health`
5. `check_kalshi_auth.py`
6. `check_polymarket_us.py` for `POLYMARKET_VARIANT=us`, otherwise `check_polymarket.py`
7. `check_telegram.py`
8. `check_mapping_ready.py`
9. `python -m arbiter.live.preflight`

Prints the exact command for the first supervised live trade at the end.

## Safety invariants these scripts preserve

1. **No secret ever leaves the machine.** All network calls are to the
   destined platforms (Kalshi, Polymarket, Telegram, Polygon RPC). No
   telemetry, no crash-reporting to third parties.
2. **No side effects on-platform.** These are read-only: balance queries,
   CLOB auth derivation (does not place orders), Telegram sendMessage
   (a harmless dry-test).
3. **Every script is independently runnable.** You can skip straight to
   `check_polymarket_us.py` for the default US path, or `check_polymarket.py`
   only when debugging the legacy variant.

## See also

- [`/HANDOFF.md`](../../HANDOFF.md) — next-agent handoff doc
- [`/GOLIVE.md`](../../GOLIVE.md) — full 13-section operator runbook
- [`/deploy/README.md`](../../deploy/README.md) — docker-compose + systemd
  deployment paths
