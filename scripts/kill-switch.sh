#!/bin/bash
# One-command kill-switch wrapper around POST /api/kill-switch.
#
# Usage:
#   scripts/kill-switch.sh arm "reason text"     # halt all trading immediately
#   scripts/kill-switch.sh reset "operator note" # clear the kill (respects cooldown)
#   scripts/kill-switch.sh status                # GET /api/safety/status (no auth)
#
# Auth: arm/reset require a session token in $ARBITER_AUTH_TOKEN
# (login via POST /api/auth/login first; status needs no auth).
set -euo pipefail
API="${ARBITER_API:-http://localhost:8080}"
ACTION="${1:-status}"
REASON="${2:-}"

case "$ACTION" in
  arm|reset)
    if [[ -z "${ARBITER_AUTH_TOKEN:-}" ]]; then
      echo "ERROR: set ARBITER_AUTH_TOKEN (POST /api/auth/login first)" >&2
      exit 2
    fi
    if [[ -z "$REASON" ]]; then
      echo "ERROR: $ACTION requires a reason as the 2nd argument" >&2
      exit 2
    fi
    BODY=$(python3 -c "import json,sys; print(json.dumps({'action': '$ACTION', 'reason': sys.argv[1], 'note': sys.argv[1]}))" "$REASON")
    curl -sS -X POST "$API/api/kill-switch" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $ARBITER_AUTH_TOKEN" \
      -d "$BODY" | python3 -m json.tool
    ;;
  status)
    curl -sS "$API/api/safety/status" | python3 -m json.tool
    ;;
  *)
    echo "Usage: $0 {arm|reset|status} [reason]" >&2
    exit 2
    ;;
esac
