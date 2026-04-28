#!/usr/bin/env python3
"""
Inspect the API structure to understand how prices are organized and mapped.
"""
import requests
import json
from collections import Counter

response = requests.get("http://localhost:8080/api/prices")
data = response.json()

# Analyze structure
mapping_statuses = Counter()
platforms = Counter()
price_count = 0

sample_kalshi = None
sample_polymarket = None

for key, price in data.items():
    price_count += 1
    mapping_status = price.get('mapping_status', 'MISSING')
    platform = price.get('platform', 'MISSING')

    mapping_statuses[mapping_status] += 1
    platforms[platform] += 1

    if platform == 'kalshi' and not sample_kalshi:
        sample_kalshi = price
    if platform == 'polymarket' and not sample_polymarket:
        sample_polymarket = price

print(f"Total prices in API: {price_count}")
print(f"\nPlatforms:")
for platform, count in sorted(platforms.items()):
    print(f"  {platform}: {count}")

print(f"\nMapping statuses:")
for status, count in sorted(mapping_statuses.items()):
    print(f"  {status}: {count}")

print(f"\n\nSample Kalshi price entry:")
if sample_kalshi:
    print(json.dumps(sample_kalshi, indent=2)[:500])

print(f"\n\nSample Polymarket price entry:")
if sample_polymarket:
    print(json.dumps(sample_polymarket, indent=2)[:500])

# Check if there's a way prices reference each other
print(f"\n\nSearching for cross-references in Kalshi prices...")
kalshi_prices = [p for p in data.values() if p.get('platform') == 'kalshi'][:5]
for i, kp in enumerate(kalshi_prices):
    print(f"\nKalshi entry {i+1} keys: {list(kp.keys())}")
    # Look for any reference fields
    for k, v in kp.items():
        if isinstance(v, str) and ('poly' in v.lower() or 'market' in v.lower()):
            print(f"  Found reference: {k} = {v}")
