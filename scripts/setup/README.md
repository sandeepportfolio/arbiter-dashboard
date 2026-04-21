# `scripts/setup/` — Pre-Go-Live Validators

Six scripts that check the production setup step by step. Use `go_live.sh`
to run them all in sequence, or invoke individually to diagnose a specific
failure.

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
    python scripts/setup/validate_env.py

Typical PASS output: `✓ .env.production shape + sanity OK` + a per-var PASS
table. Typical FAIL: one of:
- `still contains a template placeholder`
- `looks like a demo/sandbox value — Phase 5 requires production`
- `file not found at ./keys/kalshi_private.pem`
- `wrong length (expected 64 hex chars, got N)`

### `check_kalshi_auth.py` — signed round-trip vs Kalshi prod
Uses the existing `arbiter.collectors.kalshi.KalshiAuth` to sign a
`GET /portfolio/balance` request and reads the response.

    python scripts/setup/check_kalshi_auth.py

PASS prints the account balance. FAIL prints HTTP status + likely cause
(key-mismatch, clock-skew, or wrong base URL).

### `check_polymarket.py` — wallet + USDC + CLOB auth
Verifies `POLY_PRIVATE_KEY` is a valid 32-byte hex, derives the address,
queries USDC.e balance on Polygon, and attempts Polymarket CLOB
`create_or_derive_api_creds`.

    python scripts/setup/check_polymarket.py

PASS prints: derived address, USDC.e balance, POL balance, `PASS` on CLOB
auth. FAIL: invalid key, unfunded wallet, or wrong signature type.

### `check_telegram.py` — bot dry-test
Thin wrapper around `python -m arbiter.notifiers.telegram`. Sends one test
message "🧪 Arbiter Telegram dry-test" to the configured chat.

    python scripts/setup/check_telegram.py

PASS: exit 0 + message delivered to your Telegram. FAIL: exit 1 (disabled/
bad token) or 2 (exception).

### `check_mapping_ready.py` — MARKET_MAP readiness
Prints every mapping with its status + `allow_auto_trade` flag. PASS when
≥1 mapping has `status=confirmed` AND `allow_auto_trade=true` AND
`resolution_match_status=identical`.

    python scripts/setup/check_mapping_ready.py

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
6. `check_polymarket.py`
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
3. **Every script is independently runnable.** You can skip to
   `check_polymarket.py` if you're only debugging the Polymarket leg.

## See also

- [`/HANDOFF.md`](../../HANDOFF.md) — next-agent handoff doc
- [`/GOLIVE.md`](../../GOLIVE.md) — full 13-section operator runbook
- [`/deploy/README.md`](../../deploy/README.md) — docker-compose + systemd
  deployment paths
