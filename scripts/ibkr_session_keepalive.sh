#!/bin/bash
# IBKR Client Portal session keepalive supervisor.
#
# Keeps the CP gateway (https://localhost:5000) session alive unattended:
#   - tickles every KEEPALIVE_INTERVAL_S (default 60s; brokerage session dies ~5min idle)
#   - on soft expiry (connected && !authenticated) re-inits the brokerage session via
#     POST /iserver/auth/ssodh/init {"publish":true,"compete":true}  (the officially
#     recommended recovery; /iserver/reauthenticate is deprecated and often no-ops)
#   - restarts the gateway process if nothing is listening on :5000
#   - on hard expiry (SSO gone — only a human browser login can fix it) sends a macOS
#     notification once per state change and logs a HARD_LOGIN_REQUIRED marker
#
# Designed to run under launchd (com.arbiter.ibkr-keepalive). Install:
#   mkdir -p ~/.arbiter/bin && cp scripts/ibkr_session_keepalive.sh ~/.arbiter/bin/
#   (launchd + ~/Documents = TCC denial; run the ~/.arbiter/bin copy, not the repo copy)

set -u

GW="https://localhost:5000/v1/api"
GATEWAY_START="${IBKR_GATEWAY_START:-$HOME/ibkr-gateway/start.sh}"
LOGFILE="${IBKR_KEEPALIVE_LOG:-$HOME/.arbiter/ibkr-keepalive.log}"
STATEFILE="${IBKR_KEEPALIVE_STATE:-$HOME/.arbiter/ibkr-keepalive.state}"
INTERVAL="${KEEPALIVE_INTERVAL_S:-60}"
HEARTBEAT_EVERY=10   # log an "ok" line every N cycles (~10 min at 60s)

mkdir -p "$(dirname "$LOGFILE")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE"; }

notify() {
    # Best-effort macOS notification; never fail the loop over it.
    /usr/bin/osascript -e "display notification \"$1\" with title \"Arbiter IBKR\" sound name \"Basso\"" 2>/dev/null || true
}

state_get() { cat "$STATEFILE" 2>/dev/null || echo "unknown"; }
state_set() { echo "$1" > "$STATEFILE"; }

gw_post() { curl -sk -m 10 -X POST -d "${2:-}" ${3:+-H "Content-Type: application/json"} "$GW$1" 2>/dev/null; }

authenticated() { echo "$1" | grep -q '"authenticated":true'; }
connected()     { echo "$1" | grep -q '"connected":true'; }

cycle=0
log "keepalive started (interval=${INTERVAL}s, gateway=$GATEWAY_START)"

while true; do
    cycle=$((cycle + 1))

    # 1. Gateway process up?
    if ! curl -sk -m 5 -o /dev/null "$GW/one/user"; then
        log "gateway unreachable on :5000 — restarting via $GATEWAY_START"
        nohup "$GATEWAY_START" >/dev/null 2>&1 &
        sleep 20
        if curl -sk -m 5 -o /dev/null "$GW/one/user"; then
            log "gateway restarted; fresh SSO login will be required"
            notify "IBKR gateway restarted — sign in at https://localhost:5000"
            state_set "hard_login_required"
        else
            log "gateway restart FAILED — will retry next cycle"
            state_set "gateway_down"
        fi
        sleep "$INTERVAL"
        continue
    fi

    # 2. Tickle (keeps SSO + brokerage session warm; embeds auth status)
    tickle_resp=$(gw_post /tickle)
    status_resp=$(gw_post /iserver/auth/status)

    if authenticated "$status_resp"; then
        [ "$(state_get)" != "ok" ] && log "session healthy: $status_resp" && notify "IBKR session healthy again"
        state_set "ok"
        [ $((cycle % HEARTBEAT_EVERY)) -eq 0 ] && log "ok (ssoExpires=$(echo "$tickle_resp" | sed -n 's/.*"ssoExpires":\([0-9]*\).*/\1/p')ms)"
    elif connected "$status_resp"; then
        # Soft expiry: brokerage session dropped or competing session took the slot.
        log "soft expiry detected ($status_resp) — re-initing brokerage session"
        init_resp=$(gw_post /iserver/auth/ssodh/init '{"publish":true,"compete":true}' json)
        sleep 3
        recheck=$(gw_post /iserver/auth/status)
        if authenticated "$recheck"; then
            log "brokerage session recovered via ssodh/init"
            state_set "ok"
        else
            log "ssodh/init did not recover (init=$init_resp recheck=$recheck)"
            if [ "$(state_get)" != "hard_login_required" ]; then
                log "HARD_LOGIN_REQUIRED — human browser login needed at https://localhost:5000"
                notify "IBKR needs a human login at https://localhost:5000"
                state_set "hard_login_required"
            fi
        fi
    else
        # Not even connected: SSO likely dead (daily midnight-NY reset does this).
        if [ "$(state_get)" != "hard_login_required" ]; then
            log "HARD_LOGIN_REQUIRED (not connected: $status_resp) — human browser login needed"
            notify "IBKR needs a human login at https://localhost:5000"
            state_set "hard_login_required"
        fi
    fi

    sleep "$INTERVAL"
done
