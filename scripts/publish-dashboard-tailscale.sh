#!/usr/bin/env bash
set -euo pipefail

TS_SOCKET="${TS_SOCKET:-$HOME/.tailscale/tailscaled.sock}"
TAILSCALE_BIN="${TAILSCALE_BIN:-$(command -v tailscale)}"
ARBITER_HOSTNAME="${ARBITER_HOSTNAME:-arbiter-dashboard}"
ARBITER_PORT="${ARBITER_PORT:-8100}"
ARBITER_TAILSCALE_MODE="${ARBITER_TAILSCALE_MODE:-funnel}"

if [[ -z "${TAILSCALE_BIN}" ]]; then
  echo "tailscale CLI not found in PATH" >&2
  exit 1
fi

if [[ ! -S "${TS_SOCKET}" ]]; then
  echo "tailscaled socket not found at ${TS_SOCKET}" >&2
  exit 1
fi

"${TAILSCALE_BIN}" --socket="${TS_SOCKET}" set --hostname="${ARBITER_HOSTNAME}"

case "${ARBITER_TAILSCALE_MODE}" in
  funnel)
    "${TAILSCALE_BIN}" --socket="${TS_SOCKET}" funnel --bg "${ARBITER_PORT}"
    ;;
  serve)
    "${TAILSCALE_BIN}" --socket="${TS_SOCKET}" serve --bg "${ARBITER_PORT}"
    ;;
  *)
    echo "Unsupported ARBITER_TAILSCALE_MODE=${ARBITER_TAILSCALE_MODE} (use funnel or serve)" >&2
    exit 1
    ;;
 esac

DNS_NAME="$(${TAILSCALE_BIN} --socket="${TS_SOCKET}" status --json | python3 -c 'import json,sys; payload=json.load(sys.stdin); print((payload.get("Self", {}) or {}).get("DNSName", "").rstrip("."))')"

if [[ -z "${DNS_NAME}" ]]; then
  echo "Dashboard published, but could not determine DNS name." >&2
  exit 1
fi

echo "Arbiter dashboard URL: https://${DNS_NAME}"
echo "Mode: ${ARBITER_TAILSCALE_MODE}"
echo "Upstream: http://127.0.0.1:${ARBITER_PORT}"
