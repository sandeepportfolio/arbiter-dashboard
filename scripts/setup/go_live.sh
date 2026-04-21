#!/usr/bin/env bash
# go_live.sh — orchestrates the complete pre-flight check sequence.
#
# Runs (in order):
#   1. validate_env.py       Shape + sanity check on .env.production
#   2. docker compose up -d  Bring up Postgres + Redis + arbiter-api
#   3. check_kalshi_auth.py  Signed round-trip against Kalshi prod
#   4. check_polymarket.py   Wallet + USDC + CLOB auth round-trip
#   5. check_telegram.py     Send test message via bot
#   6. check_mapping_ready   ≥1 MARKET_MAP entry confirmed + allow_auto_trade
#   7. arbiter.live.preflight 15-item preflight runner
#
# ANY failure stops the script. Does NOT run the first live trade — that's
# always operator-supervised.
#
# Usage (from repo root):
#   ./scripts/setup/go_live.sh
#
# Assumes:
#   - .env.production exists and is populated
#   - ./keys/kalshi_private.pem exists and is readable
#   - Docker + docker-compose v2 installed
#   - Repo dependencies installed (pip install -r requirements.txt)

set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

PYTHON_BIN="${ARBITER_PYTHON:-$ROOT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    else
        echo "No Python interpreter found. Run ./scripts/setup/bootstrap_python.sh first." >&2
        exit 1
    fi
fi

# ─── Colors ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; BOLD=""; NC=""
fi

step() { echo; echo "${BOLD}━━━ $1 ━━━${NC}"; }
pass() { echo "${GREEN}✓${NC} $1"; }
fail() { echo "${RED}✗${NC} $1" >&2; exit 1; }

# ─── 0. Preconditions ─────────────────────────────────────────────────
step "0. Preconditions"

if [ ! -f .env.production ]; then
    fail ".env.production not found. Copy .env.production.template and fill in the placeholders first."
fi
pass ".env.production exists"

if [ ! -f keys/kalshi_private.pem ]; then
    fail "keys/kalshi_private.pem not found. Download the RSA key from kalshi.com and save it there."
fi
pass "keys/kalshi_private.pem exists"

if ! command -v docker >/dev/null 2>&1; then
    fail "docker not installed or not in PATH"
fi
pass "docker available"

if ! "$PYTHON_BIN" --version >/dev/null 2>&1; then
    fail "selected Python is not runnable: $PYTHON_BIN"
fi
pass "python available via $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# Source env so subsequent python calls see the values
set -a
# shellcheck disable=SC1091
source .env.production
set +a

# ─── 1. validate_env.py ───────────────────────────────────────────────
step "1. Validate .env.production shape + sanity"
if ! "$PYTHON_BIN" scripts/setup/validate_env.py; then
    fail "validate_env.py reported FAIL rows. Fix them and re-run."
fi

# ─── 2. docker compose up ─────────────────────────────────────────────
step "2. Bring up production docker stack"
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
echo "Waiting 20s for healthchecks to settle..."
sleep 20

# Poll /api/health
for i in 1 2 3 4 5; do
    if curl -sf http://localhost:8080/api/health >/dev/null; then
        pass "arbiter-api-prod /api/health OK"
        break
    fi
    if [ "$i" -eq 5 ]; then
        echo "${YELLOW}(stack may still be coming up — check docker compose logs)${NC}"
        fail "arbiter-api-prod /api/health did not respond after 5 attempts"
    fi
    sleep 5
done

# ─── 3. Kalshi auth round-trip ────────────────────────────────────────
step "3. Kalshi prod authentication + balance check"
if ! "$PYTHON_BIN" scripts/setup/check_kalshi_auth.py; then
    fail "Kalshi auth check failed — see output above"
fi

# ─── 4. Polymarket wallet ─────────────────────────────────────────────
step "4. Polymarket wallet / credentials check"
# Task 17: route to check_polymarket_us.py for US DCM variant, legacy otherwise.
_POLY_VARIANT="${POLYMARKET_VARIANT:-legacy}"
if [ "$_POLY_VARIANT" = "us" ]; then
    if ! "$PYTHON_BIN" scripts/setup/check_polymarket_us.py; then
        fail "Polymarket US check failed — see output above"
    fi
else
    if ! "$PYTHON_BIN" scripts/setup/check_polymarket.py; then
        fail "Polymarket check failed — see output above"
    fi
fi

# ─── 5. Telegram dry-test ─────────────────────────────────────────────
step "5. Telegram dry-test (a message should land in your chat)"
if ! "$PYTHON_BIN" scripts/setup/check_telegram.py; then
    fail "Telegram dry-test failed — see output above. Fix BOT_TOKEN/CHAT_ID, or message the bot first."
fi

# ─── 6. MARKET_MAP readiness ──────────────────────────────────────────
step "6. MARKET_MAP: at least one auto-trade-ready pair"
if ! "$PYTHON_BIN" scripts/setup/check_mapping_ready.py; then
    echo "${YELLOW}Open http://localhost:8080/ops, log in, navigate to Mappings panel.${NC}"
    echo "${YELLOW}Pick one pair with identical resolution criteria, click Confirm, then Enable auto-trade.${NC}"
    echo "${YELLOW}Re-run this script.${NC}"
    fail "No mapping ready for auto-trade"
fi

# ─── 7. 15-item preflight (+ Task 16 5b live balance) ────────────────
step "7. Arbiter preflight (16 checks)"
# Preflight in-process (not via docker exec) so we use the same env we just validated.
# PREFLIGHT_ALLOW_LIVE=1 enables the 5b live balance check (safe here — we already
# verified credentials in step 4, and go_live.sh always runs with full env).
if ! PREFLIGHT_ALLOW_LIVE=1 "$PYTHON_BIN" -m arbiter.live.preflight; then
    fail "Preflight reported blockers — fix them and re-run"
fi

# ─── Done ─────────────────────────────────────────────────────────────
step "ALL CHECKS PASSED"
cat <<EOF

${GREEN}${BOLD}System is ready for the first supervised live trade.${NC}

${BOLD}Next (manual, operator-required):${NC}

  1. Open the dashboard in a browser — you need the kill-switch ARM button in reach.
     ${BOLD}open http://localhost:8080/ops${NC}

  2. Run the first-live-trade scenario. It will:
     - write pre_trade_requote.json to evidence/05/
     - sleep 60 seconds (your abort window — hit ARM if anything looks wrong)
     - place ONE real trade on the confirmed mapping
     - reconcile within ±\$0.01 OR trip the kill-switch via auto_abort

     ${BOLD}docker compose -f docker-compose.prod.yml exec arbiter-api-prod \\
         pytest -m live --live arbiter/live/test_first_live_trade.py -v -s${NC}

  3. If step 2 passes cleanly, flip AUTO_EXECUTE_ENABLED=true in .env.production
     and restart arbiter-api-prod. The system will then auto-trade within
     MAX_POSITION_USD and PHASE5_BOOTSTRAP_TRADES caps.

Documents: GOLIVE.md (full runbook), HANDOFF.md (for a second operator).
EOF
