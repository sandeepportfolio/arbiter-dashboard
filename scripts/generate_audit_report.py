#!/usr/bin/env python3
"""
Generate comprehensive JSON report with all audit findings.
"""

import requests
import json
from datetime import datetime
from collections import defaultdict

BASE_URL = "http://localhost:8080"

def fetch_and_analyze():
    """Fetch data and generate comprehensive report."""
    response = requests.get(f"{BASE_URL}/api/prices", timeout=5)
    raw_data = response.json()
    timestamp = datetime.now()

    # Group by canonical_id to find cross-platform pairs
    by_canonical = defaultdict(lambda: defaultdict(list))

    for key, price in raw_data.items():
        canonical_id = price.get('canonical_id', '')
        platform = price.get('platform', '')
        status = price.get('mapping_status', '')

        if canonical_id and platform:
            by_canonical[canonical_id][platform].append({
                'price_data': price,
                'status': status
            })

    # Extract confirmed and candidate pairs
    confirmed_pairs = []
    candidate_pairs = []

    for canonical_id, platforms in by_canonical.items():
        if 'kalshi' in platforms and 'polymarket' in platforms:
            kalshi_entries = platforms['kalshi']
            polymarket_entries = platforms['polymarket']

            kalshi_statuses = [e['status'] for e in kalshi_entries]
            polymarket_statuses = [e['status'] for e in polymarket_entries]

            is_confirmed = any(s == 'confirmed' for s in kalshi_statuses + polymarket_statuses)
            is_candidate = any(s == 'candidate' for s in kalshi_statuses + polymarket_statuses)

            k_data = kalshi_entries[0]['price_data']
            p_data = polymarket_entries[0]['price_data']

            k_yes = k_data.get('yes_price')
            k_no = k_data.get('no_price')
            p_yes = p_data.get('yes_price')
            p_no = p_data.get('no_price')

            if all(v is not None for v in [k_yes, k_no, p_yes, p_no]):
                edge = 1.0 - min(k_yes, p_yes) - min(k_no, p_no)
                k_fee = k_data.get('fee_rate', 0)
                p_fee = p_data.get('fee_rate', 0)
                net_edge = edge - (k_fee + p_fee)

                pair_dict = {
                    'canonical_id': canonical_id,
                    'kalshi_market': k_data.get('raw_market_id'),
                    'polymarket_market': p_data.get('raw_market_id'),
                    'kalshi_yes': round(k_yes, 4),
                    'kalshi_no': round(k_no, 4),
                    'kalshi_fee_rate': round(k_fee, 4),
                    'polymarket_yes': round(p_yes, 4),
                    'polymarket_no': round(p_no, 4),
                    'polymarket_fee_rate': round(p_fee, 4),
                    'gross_edge': round(edge, 4),
                    'gross_edge_percent': round(edge * 100, 2),
                    'net_edge_after_fees': round(net_edge, 4),
                    'net_edge_percent': round(net_edge * 100, 2),
                    'profitable': net_edge > 0.001,
                    'trade_direction': {
                        'buy_yes_on': 'Kalshi' if k_yes < p_yes else 'Polymarket',
                        'buy_no_on': 'Kalshi' if k_no < p_no else 'Polymarket'
                    },
                    'kalshi_status': kalshi_statuses[0],
                    'polymarket_status': polymarket_statuses[0]
                }

                if is_confirmed:
                    confirmed_pairs.append(pair_dict)
                elif is_candidate:
                    candidate_pairs.append(pair_dict)

    # Analysis
    confirmed_sorted = sorted(confirmed_pairs, key=lambda x: x['gross_edge'], reverse=True)
    candidate_sorted = sorted(candidate_pairs, key=lambda x: x['gross_edge'], reverse=True)

    profitable_confirmed = [p for p in confirmed_pairs if p['profitable']]
    unprofitable_confirmed = [p for p in confirmed_pairs if not p['profitable']]

    # Generate report
    report = {
        'audit_timestamp': timestamp.isoformat(),
        'api_url': BASE_URL,
        'summary': {
            'total_prices': len(raw_data),
            'kalshi_prices': sum(1 for p in raw_data.values() if p.get('platform') == 'kalshi'),
            'polymarket_prices': sum(1 for p in raw_data.values() if p.get('platform') == 'polymarket'),
            'confirmed_cross_platform_pairs': len(confirmed_pairs),
            'candidate_cross_platform_pairs': len(candidate_pairs),
            'profitable_confirmed_pairs': len(profitable_confirmed),
            'unprofitable_confirmed_pairs': len(unprofitable_confirmed)
        },
        'mapping_status_breakdown': {
            'confirmed': sum(1 for p in raw_data.values() if p.get('mapping_status') == 'confirmed'),
            'candidate': sum(1 for p in raw_data.values() if p.get('mapping_status') == 'candidate'),
            'expired': sum(1 for p in raw_data.values() if p.get('mapping_status') == 'expired'),
            'review': sum(1 for p in raw_data.values() if p.get('mapping_status') == 'review'),
            'rejected': sum(1 for p in raw_data.values() if p.get('mapping_status') == 'rejected')
        },
        'confirmed_pairs': {
            'all': confirmed_sorted,
            'profitable': sorted(profitable_confirmed, key=lambda x: x['net_edge_after_fees'], reverse=True),
            'unprofitable': sorted(unprofitable_confirmed, key=lambda x: x['net_edge_after_fees'], reverse=True),
            'top_10_by_gross_edge': confirmed_sorted[:10]
        },
        'candidate_pairs': {
            'all': candidate_sorted,
            'top_10_by_edge': candidate_sorted[:10]
        },
        'fee_analysis': {
            'average_kalshi_fee': round(sum(p['kalshi_fee_rate'] for p in confirmed_pairs) / len(confirmed_pairs) if confirmed_pairs else 0, 4),
            'average_polymarket_fee': round(sum(p['polymarket_fee_rate'] for p in confirmed_pairs) / len(confirmed_pairs) if confirmed_pairs else 0, 4),
            'max_gross_edge': round(max([p['gross_edge'] for p in confirmed_pairs], default=0), 4),
            'min_gross_edge': round(min([p['gross_edge'] for p in confirmed_pairs], default=0), 4),
            'avg_gross_edge': round(sum(p['gross_edge'] for p in confirmed_pairs) / len(confirmed_pairs) if confirmed_pairs else 0, 4)
        },
        'alerts': {
            'zero_profitable_confirmed_pairs': len(profitable_confirmed) == 0,
            'high_candidate_edges_not_confirmed': [
                {
                    'canonical_id': p['canonical_id'],
                    'edge_percent': p['gross_edge_percent'],
                    'kalshi_market': p['kalshi_market'],
                    'polymarket_market': p['polymarket_market'],
                    'note': 'This large edge is not yet confirmed - may indicate missing mapping or data quality issue'
                }
                for p in candidate_sorted[:5] if p['gross_edge'] > 0.05
            ]
        }
    }

    return report

if __name__ == "__main__":
    report = fetch_and_analyze()

    # Save to JSON
    with open("/tmp/arbiter_audit_report.json", "w") as f:
        json.dump(report, f, indent=2)

    # Print summary
    print(json.dumps(report, indent=2))
    print(f"\nFull report saved to: /tmp/arbiter_audit_report.json")
