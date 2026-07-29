#!/bin/bash
# One-command kill-switch wrapper around POST /api/kill-switch.
#
# Usage:
#   scripts/kill-switch.sh arm "reason text"     # halt all trading immediately
#   scripts/kill-switch.sh reset "operator note" # clear the kill (respects cooldown)
#   scripts/kill-switch.sh status                # GET /api/safety/status
#
# Auth: ALL three subcommands require a session token in $ARBITER_AUTH_TOKEN
# (login via POST /api/auth/login first). Every /api/* route is gated by
# _auth_middleware (arbiter/api.py) — including status.
set -euo pipefail
API="${ARBITER_API:-http://localhost:8080}"
ACTION="${1:-status}"
REASON="${2:-}"

require_token() {
  if [[ -z "${ARBITER_AUTH_TOKEN:-}" ]]; then
    echo "ERROR: set ARBITER_AUTH_TOKEN (POST /api/auth/login first)" >&2
    exit 2
  fi
}

# Print the response body, then FAIL LOUDLY on any non-2xx.
#
# `curl -sS` exits 0 on an HTTP error, so the previous form piped
# {"error":"Authentication required"} straight into `python3 -m json.tool`,
# which parsed it happily — the script exited 0 and an operator checking the
# kill switch mid-incident read an error object as safety state. Checking the
# status code is what makes a 401/403/500 a visible failure rather than a
# silent false reading.
request() {
  local raw rc http body
  set +e
  raw=$(curl -sS -w $'\n%{http_code}' "$@" 2>&1)
  rc=$?
  set -e
  if (( rc != 0 )); then
    echo "ERROR: cannot reach $API (curl exit $rc)" >&2
    printf '%s\n' "$raw" >&2
    exit 1
  fi
  http=${raw##*$'\n'}
  body=${raw%$'\n'*}
  if [[ -n "$body" ]]; then
    printf '%s\n' "$body" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$body"
  fi
  if [[ "$http" != 2* ]]; then
    echo "ERROR: HTTP $http from $API — response above is NOT kill-switch state" >&2
    exit 1
  fi
}

case "$ACTION" in
  arm|reset)
    require_token
    if [[ -z "$REASON" ]]; then
      echo "ERROR: $ACTION requires a reason as the 2nd argument" >&2
      exit 2
    fi
    BODY=$(python3 -c "import json,sys; print(json.dumps({'action': '$ACTION', 'reason': sys.argv[1], 'note': sys.argv[1]}))" "$REASON")
    request -X POST "$API/api/kill-switch" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $ARBITER_AUTH_TOKEN" \
      -d "$BODY"
    ;;
  status)
    # /api/safety/status is auth-gated like every other /api/* route.
    require_token
    request "$API/api/safety/status" \
      -H "Authorization: Bearer $ARBITER_AUTH_TOKEN"
    ;;
  *)
    echo "Usage: $0 {arm|reset|status} [reason]" >&2
    exit 2
    ;;
esac
