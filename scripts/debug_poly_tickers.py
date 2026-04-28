#!/usr/bin/env python3
import requests
import json

response = requests.get("http://localhost:8080/api/prices")
data = response.json()

polymarket_tickers = set()

for key, price in data.items():
    platform = price.get('platform', '')
    ticker = price.get('raw_market_id', '')

    if platform == 'polymarket' and ticker:
        polymarket_tickers.add(ticker)

print(f'ALL Polymarket tickers ({len(polymarket_tickers)} total):')
for i, t in enumerate(sorted(polymarket_tickers), 1):
    print(f'  {i}. {t}')
    if i >= 30:
        print(f'  ... and {len(polymarket_tickers) - 30} more')
        break

# Look specifically for ones that might match our confirmed mappings
confirmed_slugs = [
    "aec-2026-us-house-democrats-control",
    "aec-2026-us-house-republicans-control",
    "aec-2026-us-senate-democrats-control",
    "aec-2026-us-senate-republicans-control",
    "aec-mlb-hou-bal-2026-04-27",
    "atc-bun-bar-bay-2026-04-25",
]

print(f'\nLooking for confirmed slugs in API:')
for slug in confirmed_slugs:
    found = any(slug in t for t in polymarket_tickers)
    print(f'  {slug}: {"FOUND" if found else "NOT FOUND"}')
