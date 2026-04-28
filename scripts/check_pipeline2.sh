#!/bin/bash
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH

RESP=$(curl -s --max-time 10 -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email": "sparx.sandeep@gmail.com", "password": "saibaba1"}')
TOKEN=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

echo "=== Candidate Analysis (correct field names) ==="
curl -s --max-time 30 "http://localhost:8080/api/market-mappings?status=candidate&limit=10000" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if isinstance(d, dict) and 'error' in d:
    print('Error:', d['error'])
    sys.exit(1)

scores = [float(m.get('mapping_score', 0) or 0) for m in d]
has_both = sum(1 for m in d if m.get('kalshi') and m.get('polymarket'))
has_kalshi = sum(1 for m in d if m.get('kalshi'))
has_poly = sum(1 for m in d if m.get('polymarket'))

print(f'Total candidates: {len(d)}')
print(f'With both IDs: {has_both} / {len(d)}')
print(f'With kalshi: {has_kalshi}')
print(f'With polymarket: {has_poly}')

for threshold in [0.18, 0.25, 0.30, 0.40, 0.50, 0.60]:
    count = sum(1 for s in scores if s >= threshold)
    print(f'Score >= {threshold:.2f}: {count}')

if scores:
    print(f'Score range: {min(scores):.3f} - {max(scores):.3f}')

d.sort(key=lambda x: float(x.get('mapping_score', 0) or 0), reverse=True)
print('\nTop 10 candidates:')
for m in d[:10]:
    score = float(m.get('mapping_score', 0) or 0)
    kid = (m.get('kalshi') or '')[:40]
    pid = (m.get('polymarket') or '')[:40]
    reason = m.get('auto_promote_reason', '')
    desc = (m.get('description') or '')[:50]
    print(f'  {score:.3f}  K={kid}  P={pid}')
    print(f'          desc={desc}  reason={reason}')
"

echo ""
echo "=== Confirmed ==="
curl -s --max-time 30 "http://localhost:8080/api/market-mappings?status=confirmed&limit=100" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import json, sys
d = json.load(sys.stdin)
has_both = sum(1 for m in d if m.get('kalshi') and m.get('polymarket'))
print(f'Total confirmed: {len(d)}, with both IDs: {has_both}')
"
