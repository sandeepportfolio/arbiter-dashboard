#!/bin/bash
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH

# Login
RESP=$(curl -s --max-time 10 -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email": "sparx.sandeep@gmail.com", "password": "saibaba1"}')
TOKEN=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

if [ -z "$TOKEN" ]; then
  echo "LOGIN FAILED: $RESP"
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
