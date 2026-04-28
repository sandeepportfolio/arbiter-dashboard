#!/usr/bin/env python3
import requests
import json

response = requests.get("http://localhost:8080/api/prices")
data = response.json()

kalshi = {}
polymarket = {}

for key, price in data.items():
    platform = price.get('platform', '')
    ticker = price.get('raw_market_id', '')

    if platform == 'kalshi' and ticker:
        if 'GAME' in ticker or ticker.startswith('HOUSE') or ticker.startswith('SENATE'):
            if ticker not in kalshi:
                kalshi[ticker] = price
    elif platform == 'polymarket' and ticker:
        if 'game' in ticker.lower() or 'house' in ticker.lower() or 'senate' in ticker.lower():
            if ticker not in polymarket:
                polymarket[ticker] = price

print('Sample KALSHI game/political tickers (first 20):')
for i, t in enumerate(sorted(kalshi.keys())[:20], 1):
    print(f'  {i}. {t}')

print(f'\nSample POLYMARKET game/political tickers (first 20):')
for i, t in enumerate(sorted(polymarket.keys())[:20], 1):
    print(f'  {i}. {t}')

print(f'\nTotal KALSHI game/political: {len(kalshi)}')
print(f'Total POLYMARKET game/political: {len(polymarket)}')
