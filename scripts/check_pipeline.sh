#!/bin/bash
# Pipeline diagnostic — credential-safe.
#
# Reads OPS_EMAIL / OPS_PASSWORD from the environment (or an env file if
# given). Never echo the password, never bake it into the script. If
# the env vars aren't set, point the operator at the canonical source
# instead of failing silently.
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH

API_BASE="${ARBITER_API_BASE:-http://localhost:8080}"
ENV_FILE="${ARBITER_ENV_FILE:-.env.production}"

if [ -z "$OPS_EMAIL" ] || [ -z "$OPS_PASSWORD" ]; then
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
  fi
fi

if [ -z "$OPS_EMAIL" ] || [ -z "$OPS_PASSWORD" ]; then
  echo "ERROR: OPS_EMAIL / OPS_PASSWORD not set."
  echo "  Export them in your shell or place them in $ENV_FILE."
  exit 2
fi

# Build the login body in Python so the password never appears on a
# command line and never lands in shell history / process listings.
LOGIN_BODY=$(OPS_EMAIL="$OPS_EMAIL" OPS_PASSWORD="$OPS_PASSWORD" \
  python3 -c 'import os, json; print(json.dumps({"email": os.environ["OPS_EMAIL"], "password": os.environ["OPS_PASSWORD"]}))')

RESP=$(curl -s --max-time 10 -X POST "$API_BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  --data-binary "$LOGIN_BODY")
unset LOGIN_BODY
TOKEN=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)

if [ -z "$TOKEN" ]; then
  # Print only HTTP-level fail signal — never echo the password or the
  # raw response body (it may contain server-side echoes of the request).
  echo "LOGIN FAILED (no token returned)"
  exit 1
fi

echo "=== Candidate Analysis ==="
curl -s --max-time 30 "http://localhost:8080/api/market-mappings?status=candidate&limit=10000" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if isinstance(d, dict) and 'error' in d:
    print('Error:', d['error'])
    sys.exit(1)

scores = [float(m.get('mapping_score', 0) or 0) for m in d]
has_both = sum(1 for m in d if m.get('kalshi_market_id') and m.get('polymarket_slug'))
has_kalshi = sum(1 for m in d if m.get('kalshi_market_id'))
has_poly = sum(1 for m in d if m.get('polymarket_slug'))

print(f'Total candidates: {len(d)}')
print(f'With both IDs: {has_both} / {len(d)}')
print(f'With kalshi_market_id: {has_kalshi}')
print(f'With polymarket_slug: {has_poly}')
print()

for threshold in [0.18, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
    count = sum(1 for s in scores if s >= threshold)
    print(f'Score >= {threshold:.2f}: {count}')

if scores:
    print(f'\nScore range: {min(scores):.3f} - {max(scores):.3f}')
    print(f'Avg: {sum(scores)/len(scores):.3f}')

# Show top 20 with IDs
d.sort(key=lambda x: float(x.get('mapping_score', 0) or 0), reverse=True)
print('\nTop 20 candidates:')
for m in d[:20]:
    score = float(m.get('mapping_score', 0) or 0)
    kid = m.get('kalshi_market_id', '')[:35]
    pid = m.get('polymarket_slug', '')[:35]
    reason = m.get('auto_promote_reason', '')
    print(f'  {score:.3f}  K={kid}  P={pid}  reason={reason}')
"

echo ""
echo "=== Confirmed Mappings ==="
curl -s --max-time 30 "http://localhost:8080/api/market-mappings?status=confirmed&limit=10000" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if isinstance(d, dict) and 'error' in d:
    print('Error:', d['error'])
    sys.exit(1)

print(f'Total confirmed: {len(d)}')
auto_trade = sum(1 for m in d if m.get('allow_auto_trade'))
print(f'Auto-trade enabled: {auto_trade}')
has_both = sum(1 for m in d if m.get('kalshi_market_id') and m.get('polymarket_slug'))
print(f'With both IDs: {has_both} / {len(d)}')
"
