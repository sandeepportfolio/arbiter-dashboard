#!/bin/bash
# Complete the IBKR OAuth 1.0a (Path B, zero-login) cutover in one shot.
#
# Usage:
#   scripts/complete_oauth_cutover.sh <CONSUMER_KEY> <ACCESS_TOKEN> <ACCESS_TOKEN_SECRET>
#
# What it does, in order — stops at the first failure:
#   1. Writes the three values into .env.production (uncommenting the slots)
#   2. Runs scripts/validate_ibkr_oauth.py live against api.ibkr.com
#   3. On green: sets IBKR_OAUTH_ENABLED=true, tags the current image as
#      rollback, rebuilds, force-recreates arbiter-api-prod
#   4. Waits for /api/readiness to come back with zero blockers (5 min max);
#      on failure it flips IBKR_OAUTH_ENABLED back off and recreates again
#      (gateway transport), so a bad cutover can never strand FX.
#
# If step 2 fails because IBKR hasn't activated the consumer key yet (they
# sometimes enable it only after their nightly reset), just re-run this
# script later — steps are idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 3 ] || { echo "usage: $0 <CONSUMER_KEY> <ACCESS_TOKEN> <ACCESS_TOKEN_SECRET>"; exit 2; }
CK="$1"; AT="$2"; ATS="$3"
ENVF=".env.production"

echo "── 1/4 writing OAuth values into $ENVF"
python3 - "$ENVF" "$CK" "$AT" "$ATS" <<'PY'
import re, sys
path, ck, at, ats = sys.argv[1:5]
text = open(path).read()
def setvar(text, key, val):
    # Replace an existing (possibly commented) assignment, else append.
    pat = re.compile(rf"^#?{key}=.*$", re.M)
    line = f"{key}={val}"
    return pat.sub(line, text, count=1) if pat.search(text) else text + f"\n{line}\n"
text = setvar(text, "IBKR_OAUTH_CONSUMER_KEY", ck)
text = setvar(text, "IBKR_OAUTH_ACCESS_TOKEN", at)
text = setvar(text, "IBKR_OAUTH_ACCESS_TOKEN_SECRET", ats)
open(path, "w").write(text)
print("   values written")
PY

echo "── 2/4 live validation against api.ibkr.com"
set -a; source "$ENVF"; set +a
# Key files: env points at /app/... (container paths); remap for host run.
export IBKR_OAUTH_SIGNATURE_KEY="deploy/ib-gateway/oauth/private_signature.pem"
export IBKR_OAUTH_ENCRYPTION_KEY="deploy/ib-gateway/oauth/private_encryption.pem"
export IBKR_OAUTH_DH_PARAM="deploy/ib-gateway/oauth/dhparam.pem"
python3 scripts/validate_ibkr_oauth.py || {
    echo "✗ validation failed — NOT flipping the transport."
    echo "  If the key was just registered, IBKR may activate it at their"
    echo "  nightly reset; re-run this script later (idempotent)."
    exit 1
}

echo "── 3/4 enabling OAuth transport + redeploy"
python3 - "$ENVF" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
pat = re.compile(r"^#?IBKR_OAUTH_ENABLED=.*$", re.M)
line = "IBKR_OAUTH_ENABLED=true"
text = pat.sub(line, text, count=1) if pat.search(text) else text + f"\n{line}\n"
open(path, "w").write(text)
print("   IBKR_OAUTH_ENABLED=true")
PY
docker tag ghcr.io/sandeepportfolio/arbiter:latest \
    "ghcr.io/sandeepportfolio/arbiter:rollback-$(date +%Y%m%d-oauth)" || true
docker build -t ghcr.io/sandeepportfolio/arbiter:latest .
docker compose -f docker-compose.prod.yml --env-file .env.production \
    up -d --no-deps --no-build --force-recreate arbiter-api-prod

echo "── 4/4 waiting for readiness (5 min max)"
deadline=$(( $(date +%s) + 300 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -s http://localhost:8080/api/readiness \
        | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ready_for_live_trading') else 1)" \
        2>/dev/null; then
        echo "✓ CUTOVER COMPLETE — ForecastEx now runs on OAuth (api.ibkr.com)."
        echo "  No local gateway, no browser login, ever. The keepalive"
        echo "  supervisor and localhost:5000 gateway are now optional."
        exit 0
    fi
    sleep 10
done

echo "✗ readiness did not recover in 5 min — ROLLING BACK to gateway transport"
python3 - "$ENVF" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
text = re.compile(r"^IBKR_OAUTH_ENABLED=.*$", re.M).sub("#IBKR_OAUTH_ENABLED=true", text, count=1)
open(path, "w").write(text)
print("   IBKR_OAUTH_ENABLED commented back out")
PY
docker compose -f docker-compose.prod.yml --env-file .env.production \
    up -d --no-deps --no-build --force-recreate arbiter-api-prod
echo "   gateway transport restored — investigate before retrying."
exit 1
