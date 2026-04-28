#!/bin/bash
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH

# Login
RESP=$(curl -s --max-time 10 -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email": "sparx.sandeep@gmail.com", "password": "saibaba1"}')
TOKEN=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

if [ -z "$TOKEN" ]; then
  echo "LOGIN FAILED: $RESP" > /tmp/batch-discover-result.json
  exit 1
fi

echo "$(date): Discovery started" > /tmp/batch-discover-status.txt

# Batch discover with auto-promote
curl -s --max-time 900 -X POST http://localhost:8080/api/batch-discover \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"min_score": 0.18, "max_candidates": 5000, "auto_promote": true}' \
  > /tmp/batch-discover-result.json 2>&1

echo "$(date): Discovery done" >> /tmp/batch-discover-status.txt
