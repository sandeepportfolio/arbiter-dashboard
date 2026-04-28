#!/usr/bin/env python3
"""
Analyze what mappings the live API actually has and compute edges from current data.
"""
import requests
import json
from collections import defaultdict

response = requests.get("http://localhost:8080/api/prices")
data = response.json()

# Get all prices and group by platform
prices_by_slug = defaultdict(dict)

for key, price in data.items():
    platform = price.get('platform', '')
    raw_market_id = price.get('raw_market_id', '')
    mapping_status = price.get('mapping_status', '')

    if raw_market_id and platform:
        prices_by_slug[raw_market_id][platform] = {
            'price': price,
            'mapping_status': mapping_status
        }

# Find pairs that have BOTH platforms
cross_platform_pairs = []

for slug, platforms_dict in prices_by_slug.items():
    if len(platforms_dict) >= 2:  # Has at least 2 platforms
        # Extract platform info
        platform_keys = list(platforms_dict.keys())
        if len(platform_keys) >= 2:
            # Get first two platforms
            p1, p2 = platform_keys[0], platform_keys[1]
            p1_data = platforms_dict[p1]['price']
            p2_data = platforms_dict[p2]['price']

            # Get prices
            p1_yes = p1_data.get('yes_price')
            p1_no = p1_data.get('no_price')
            p2_yes = p2_data.get('yes_price')
            p2_no = p2_data.get('no_price')

            if all(v is not None for v in [p1_yes, p1_no, p2_yes, p2_no]):
                # Compute edge
                edge = 1.0 - min(p1_yes, p2_yes) - min(p1_no, p2_no)

                cross_platform_pairs.append({
                    'slug': slug,
                    'platform1': p1,
                    'platform2': p2,
                    f'{p1}_yes': round(p1_yes, 4),
                    f'{p1}_no': round(p1_no, 4),
                    f'{p2}_yes': round(p2_yes, 4),
                    f'{p2}_no': round(p2_no, 4),
                    'edge': round(edge, 4),
                    'edge_percent': round(edge * 100, 2),
                    'mapping_status_p1': platforms_dict[p1]['mapping_status'],
                    'mapping_status_p2': platforms_dict[p2]['mapping_status']
                })

print(f"Total cross-platform price pairs: {len(cross_platform_pairs)}")
print(f"Kalshi prices: {sum(1 for p in data.values() if p.get('platform') == 'kalshi')}")
print(f"Polymarket prices: {sum(1 for p in data.values() if p.get('platform') == 'polymarket')}")

# Sort by edge size
sorted_pairs = sorted(cross_platform_pairs, key=lambda x: x['edge'], reverse=True)

print(f"\nTop 30 edges (all platforms with both sides):")
print(f"{'#':<3} {'Edge %':>8} {'Status':<20} {'Slug':<50}")
print("=" * 80)

for i, pair in enumerate(sorted_pairs[:30], 1):
    status = f"{pair['mapping_status_p1'][:10]}/{pair['mapping_status_p2'][:10]}"
    edge_pct = pair['edge_percent']
    slug = pair['slug']
    print(f"{i:<3} {edge_pct:>7.2f}% {status:<20} {slug:<50}")

# Find confirmed-only pairs
confirmed_pairs = [p for p in sorted_pairs if 'confirmed' in p['mapping_status_p1'] or 'confirmed' in p['mapping_status_p2']]
print(f"\n\nPairs with AT LEAST ONE 'confirmed' mapping: {len(confirmed_pairs)}")

if confirmed_pairs:
    print(f"\nTop 10 confirmed edges:")
    for i, pair in enumerate(confirmed_pairs[:10], 1):
        print(f"{i}. {pair['slug']}")
        print(f"   Edge: {pair['edge_percent']:.2f}%")
        p1 = pair['platform1']
        p2 = pair['platform2']
        p1_yes_key = f'{p1}_yes'
        p1_no_key = f'{p1}_no'
        p2_yes_key = f'{p2}_yes'
        p2_no_key = f'{p2}_no'
        print(f"   {p1}: YES={pair[p1_yes_key]:.4f} NO={pair[p1_no_key]:.4f}")
        print(f"   {p2}: YES={pair[p2_yes_key]:.4f} NO={pair[p2_no_key]:.4f}")
