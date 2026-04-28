#!/usr/bin/env bash
# Run this AFTER completing cloudflared tunnel login in the browser.
# Usage: ./scripts/setup/finish_tunnel_setup.sh <your-hostname>
# Example: ./scripts/setup/finish_tunnel_setup.sh arbiter.yourdomain.com

set -euo pipefail

HOSTNAME="${1:-}"
if [[ -z "$HOSTNAME" ]]; then
  echo "Usage: $0 <hostname>  (e.g. arbiter.yourdomain.com)" >&2
  exit 1
fi

if [[ ! -f "$HOME/.cloudflared/cert.pem" ]]; then
  echo "cert.pem not found — complete browser login first:" >&2
  echo "  cloudflared tunnel login" >&2
  exit 1
fi

# Kill any running quick tunnel
pkill -f "cloudflared tunnel --url" 2>/dev/null || true

# Run the full setup
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bash "$SCRIPT_DIR/setup_cloudflare_tunnel.sh" "$HOSTNAME"
