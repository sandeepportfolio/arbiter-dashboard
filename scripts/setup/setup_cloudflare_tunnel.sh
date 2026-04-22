#!/usr/bin/env bash
# Set up a permanent Cloudflare Tunnel named "arbiter" pointing at the
# local Arbiter API on 127.0.0.1:8080. The tunnel URL survives reboots
# because it is tied to a named tunnel (UUID) plus a DNS hostname you
# own in Cloudflare, not a random `trycloudflare.com` quick tunnel.
#
# Prereqs:
#   - `brew install cloudflared`
#   - You have completed `cloudflared tunnel login` once so that
#     ~/.cloudflared/cert.pem exists and is tied to a Cloudflare zone
#     (domain) you control.
#
# Usage:
#   ./scripts/setup/setup_cloudflare_tunnel.sh <hostname>
# Examples:
#   ./scripts/setup/setup_cloudflare_tunnel.sh arbiter.example.com
#   ./scripts/setup/setup_cloudflare_tunnel.sh desk.yourdomain.io

set -euo pipefail

TUNNEL_NAME="arbiter"
LOCAL_ORIGIN="http://127.0.0.1:8080"
HOSTNAME="${1:-}"
CF_DIR="${HOME}/.cloudflared"
CONFIG_PATH="${CF_DIR}/config.yml"
PLIST_LABEL="com.arbiter.cloudflared"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
CLOUDFLARED_BIN="$(command -v cloudflared || true)"

if [[ -z "${CLOUDFLARED_BIN}" ]]; then
  echo "error: cloudflared is not installed. Run: brew install cloudflared" >&2
  exit 1
fi

if [[ ! -f "${CF_DIR}/cert.pem" ]]; then
  echo "error: ${CF_DIR}/cert.pem missing." >&2
  echo "Run \`cloudflared tunnel login\` first, authorize a domain you own," >&2
  echo "then re-run this script with the hostname you want to expose." >&2
  exit 1
fi

if [[ -z "${HOSTNAME}" ]]; then
  echo "error: pass the hostname you want to expose." >&2
  echo "Usage: $0 <hostname>    (e.g. arbiter.yourdomain.com)" >&2
  exit 1
fi

# 1. Create the named tunnel if it does not already exist.
if cloudflared tunnel list 2>/dev/null | awk 'NR>1 {print $2}' | grep -Fxq "${TUNNEL_NAME}"; then
  echo "tunnel \"${TUNNEL_NAME}\" already exists, reusing it"
else
  echo "creating tunnel \"${TUNNEL_NAME}\""
  cloudflared tunnel create "${TUNNEL_NAME}"
fi

TUNNEL_UUID="$(cloudflared tunnel list 2>/dev/null | awk -v name="${TUNNEL_NAME}" '$2==name {print $1}' | head -n1)"
if [[ -z "${TUNNEL_UUID}" ]]; then
  echo "error: could not resolve UUID for tunnel ${TUNNEL_NAME}" >&2
  exit 1
fi
CREDENTIALS_FILE="${CF_DIR}/${TUNNEL_UUID}.json"

# 2. Write a minimal ingress config.
cat > "${CONFIG_PATH}" <<YAML
# Managed by scripts/setup/setup_cloudflare_tunnel.sh
tunnel: ${TUNNEL_UUID}
credentials-file: ${CREDENTIALS_FILE}

ingress:
  - hostname: ${HOSTNAME}
    service: ${LOCAL_ORIGIN}
    originRequest:
      noTLSVerify: true
      connectTimeout: 30s
  - service: http_status:404
YAML
echo "wrote ${CONFIG_PATH}"

# 3. Point DNS at the tunnel (idempotent).
echo "routing ${HOSTNAME} -> tunnel ${TUNNEL_NAME}"
cloudflared tunnel route dns "${TUNNEL_NAME}" "${HOSTNAME}" || {
  echo "note: DNS route may already exist — continuing"
}

# 4. Install a LaunchAgent so the tunnel auto-starts on login / reboot.
mkdir -p "$(dirname "${PLIST_PATH}")"
cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${CLOUDFLARED_BIN}</string>
    <string>tunnel</string>
    <string>--config</string>
    <string>${CONFIG_PATH}</string>
    <string>run</string>
    <string>${TUNNEL_NAME}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${HOME}/Library/Logs/arbiter-cloudflared.log</string>
  <key>StandardErrorPath</key>
  <string>${HOME}/Library/Logs/arbiter-cloudflared.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST
echo "wrote ${PLIST_PATH}"

# 5. (Re)load the agent so it starts right now and on every reboot.
launchctl unload "${PLIST_PATH}" 2>/dev/null || true
launchctl load  "${PLIST_PATH}"
echo "launched ${PLIST_LABEL}"

echo ""
echo "Done. Your permanent URL: https://${HOSTNAME}"
echo "Tunnel UUID: ${TUNNEL_UUID}"
echo "Logs: ~/Library/Logs/arbiter-cloudflared.log"
echo ""
echo "Sanity checks:"
echo "  launchctl list | grep ${PLIST_LABEL}"
echo "  curl -sI https://${HOSTNAME}/ | head -n1"
