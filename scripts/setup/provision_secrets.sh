#!/usr/bin/env bash
#
# provision_secrets.sh — one-stop guided onboarding for a fresh clone.
#
# Walks the operator through every credential and key the live trading stack
# needs, prefers the encrypted portability bundle when present, and validates
# each live credential before handing off to go_live.sh.
#
# Usage:
#   ./scripts/setup/provision_secrets.sh              # interactive walk-through
#   ./scripts/setup/provision_secrets.sh --no-input   # non-interactive (CI / scripted)
#
# Exit codes:
#   0 — secrets in place and every live check passed
#   1 — operator action required (placeholders, missing file, failed check)
#   2 — runtime/dependency error
#
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env.production"
ENV_TEMPLATE="$REPO_ROOT/.env.production.template"
KALSHI_KEY="$REPO_ROOT/keys/kalshi_private.pem"
PORTABLE_BUNDLE="$REPO_ROOT/portable-secrets/arbiter-portable-secrets.tgz.enc"

PYTHON_BIN="${ARBITER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "[provision] no Python interpreter found. Run ./scripts/setup/bootstrap_python.sh first." >&2
    exit 2
  fi
fi

INTERACTIVE=1
if [[ "${1:-}" == "--no-input" ]]; then
  INTERACTIVE=0
fi

section() {
  printf '\n\033[1m== %s ==\033[0m\n' "$1"
}

ask() {
  local prompt="$1"
  local default="${2:-n}"
  if [[ "$INTERACTIVE" -eq 0 ]]; then
    echo "[provision] non-interactive: assuming '$default' for: $prompt"
    [[ "$default" == "y" ]]
    return
  fi
  local reply
  read -r -p "$prompt [y/N] " reply || reply=""
  [[ "$reply" =~ ^[Yy]$ ]]
}

section "Step 1 — .env.production"
if [[ -f "$ENV_FILE" ]]; then
  echo "[provision] .env.production present ($(wc -c <"$ENV_FILE") bytes)"
else
  if [[ -f "$PORTABLE_BUNDLE" ]] && ask "Restore .env.production from encrypted portable bundle?" "y"; then
    if [[ -z "${PORTABLE_SECRETS_PASSPHRASE:-}" ]]; then
      if [[ "$INTERACTIVE" -eq 0 ]]; then
        echo "[provision] PORTABLE_SECRETS_PASSPHRASE must be set in non-interactive mode." >&2
        exit 1
      fi
      read -r -s -p "Portable bundle passphrase: " pass
      echo
      export PORTABLE_SECRETS_PASSPHRASE="$pass"
      unset pass
    fi
    ./scripts/setup/import_portable_secrets.sh
  else
    if [[ ! -f "$ENV_TEMPLATE" ]]; then
      echo "[provision] .env.production.template is missing from the repo." >&2
      exit 1
    fi
    cp "$ENV_TEMPLATE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "[provision] copied template to .env.production (chmod 600)."
    echo "[provision] open it in your editor and replace every <placeholder>. See GOLIVE.md §2 for each credential."
    exit 1
  fi
fi

chmod 600 "$ENV_FILE" || true

section "Step 2 — placeholder sweep"
if grep -n '<' "$ENV_FILE" >/tmp/arbiter-placeholders.txt && [[ -s /tmp/arbiter-placeholders.txt ]]; then
  echo "[provision] .env.production still contains template placeholders:" >&2
  cat /tmp/arbiter-placeholders.txt >&2
  rm -f /tmp/arbiter-placeholders.txt
  exit 1
fi
rm -f /tmp/arbiter-placeholders.txt
echo "[provision] no placeholder tokens remaining."

section "Step 3 — Kalshi private key"
if [[ ! -f "$KALSHI_KEY" ]]; then
  echo "[provision] keys/kalshi_private.pem is missing." >&2
  echo "[provision] See GOLIVE.md §2A: download the RSA key from Kalshi's API page and save to $KALSHI_KEY." >&2
  exit 1
fi
chmod 600 "$KALSHI_KEY" || true
echo "[provision] keys/kalshi_private.pem present."

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

section "Step 4 — shape + sanity"
"$PYTHON_BIN" scripts/setup/validate_env.py

section "Step 5 — Kalshi signed round-trip"
"$PYTHON_BIN" scripts/setup/check_kalshi_auth.py

section "Step 6 — Polymarket round-trip"
case "${POLYMARKET_VARIANT:-us}" in
  us)
    "$PYTHON_BIN" scripts/setup/check_polymarket_us.py
    ;;
  legacy)
    "$PYTHON_BIN" scripts/setup/check_polymarket.py
    ;;
  disabled)
    echo "[provision] POLYMARKET_VARIANT=disabled — skipping Polymarket auth check."
    ;;
  *)
    echo "[provision] Unknown POLYMARKET_VARIANT=${POLYMARKET_VARIANT:-unset}" >&2
    exit 1
    ;;
esac

section "Step 7 — Telegram dry-test"
"$PYTHON_BIN" scripts/setup/check_telegram.py

section "All credentials validated"
echo "[provision] every live credential passed its dry-test."
echo "[provision] next step: ./scripts/setup/go_live.sh (requires Docker Desktop running)."
