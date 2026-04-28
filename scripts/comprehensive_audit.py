#!/usr/bin/env python3
"""
Comprehensive Price Audit for Arbiter System

Analyzes:
1. Data freshness and price validity
2. Arbitrage edges for all CONFIRMED mappings
3. Fee impact and profitability
4. Candidate pairs not yet confirmed
5. Missing cross-platform pairs
"""

import requests
import json
from datetime import datetime
from collections import defaultdict

BASE_URL = "http://localhost:8080"

class ComprehensiveAudit:
    def __init__(self):
        self.raw_data = None
        self.timestamp = datetime.now()

    def fetch_prices(self):
        """Fetch all prices from the API."""
        try:
            response = requests.get(f"{BASE_URL}/api/prices", timeout=5)
            response.raise_for_status()
            self.raw_data = response.json()
            return True
        except Exception as e:
            print(f"ERROR: Failed to fetch prices: {e}")
            return False

    def analyze_freshness(self):
        """Analyze data freshness and structure."""
        if not self.raw_data:
            return {}

        prices = list(self.raw_data.values())

        # Count by platform and status
        stats = {
            'total': len(prices),
            'by_platform': defaultdict(int),
            'by_status': defaultdict(int),
            'stale': 0,
        }

        stale_threshold = 120  # 2 minutes

        for price in prices:
            platform = price.get('platform', 'unknown')
            status = price.get('mapping_status', 'unknown')
            timestamp = price.get('timestamp', 0)

            stats['by_platform'][platform] += 1
            stats['by_status'][status] += 1

            age = (self.timestamp.timestamp() - timestamp) / 60
            if age > stale_threshold:
                stats['stale'] += 1

        return dict(stats)

    def analyze_binary_validity(self):
        """Check if yes_price + no_price ≈ 1.0."""
        if not self.raw_data:
            return {}

        invalid = []
        valid_count = 0

        for key, price in self.raw_data.items():
            yes = price.get('yes_price')
            no = price.get('no_price')

            if yes is not None and no is not None:
                total = yes + no
                # Allow some tolerance for 3-way markets and fees
                if abs(total - 1.0) > 0.05:
                    invalid.append({
                        'market_id': price.get('raw_market_id', 'unknown'),
                        'platform': price.get('platform'),
                        'yes': round(yes, 4),
                        'no': round(no, 4),
                        'sum': round(total, 4)
                    })
                else:
                    valid_count += 1

        return {
            'valid': valid_count,
            'invalid': len(invalid),
            'invalid_samples': invalid[:10]
        }

    def find_confirmed_pairs(self):
        """Find all cross-platform pairs with 'confirmed' status."""
        if not self.raw_data:
            return []

        # Group by canonical_id
        by_canonical = defaultdict(lambda: defaultdict(list))

        for key, price in self.raw_data.items():
            canonical_id = price.get('canonical_id', '')
            platform = price.get('platform', '')
            status = price.get('mapping_status', '')

            if canonical_id and platform:
                by_canonical[canonical_id][platform].append({
                    'price_data': price,
                    'status': status
                })

        # Find pairs with both platforms and confirmed status
        confirmed_pairs = []

        for canonical_id, platforms in by_canonical.items():
            # Must have both kalshi and polymarket
            if 'kalshi' in platforms and 'polymarket' in platforms:
                kalshi_entries = platforms['kalshi']
                polymarket_entries = platforms['polymarket']

                # Check if either has confirmed status
                kalshi_confirmed = any(e['status'] == 'confirmed' for e in kalshi_entries)
                polymarket_confirmed = any(e['status'] == 'confirmed' for e in polymarket_entries)

                if kalshi_confirmed or polymarket_confirmed:
                    # Use first entry of each platform
                    k_data = kalshi_entries[0]['price_data']
                    p_data = polymarket_entries[0]['price_data']

                    k_yes = k_data.get('yes_price')
                    k_no = k_data.get('no_price')
                    p_yes = p_data.get('yes_price')
                    p_no = p_data.get('no_price')

                    if all(v is not None for v in [k_yes, k_no, p_yes, p_no]):
                        # Compute edge
                        edge = 1.0 - min(k_yes, p_yes) - min(k_no, p_no)

                        confirmed_pairs.append({
                            'canonical_id': canonical_id,
                            'kalshi_market': k_data.get('raw_market_id'),
                            'polymarket_market': p_data.get('raw_market_id'),
                            'kalshi_yes': round(k_yes, 4),
                            'kalshi_no': round(k_no, 4),
                            'polymarket_yes': round(p_yes, 4),
                            'polymarket_no': round(p_no, 4),
                            'edge': round(edge, 4),
                            'edge_percent': round(edge * 100, 2),
                            'kalshi_fee': k_data.get('fee_rate', 0),
                            'polymarket_fee': p_data.get('fee_rate', 0),
                            'kalshi_status': kalshi_entries[0]['status'],
                            'polymarket_status': polymarket_entries[0]['status'],
                            'kalshi_timestamp': k_data.get('timestamp', 0),
                            'polymarket_timestamp': p_data.get('timestamp', 0)
                        })

        return confirmed_pairs

    def find_candidate_pairs(self):
        """Find candidate pairs (not yet confirmed)."""
        if not self.raw_data:
            return []

        by_canonical = defaultdict(lambda: defaultdict(list))

        for key, price in self.raw_data.items():
            canonical_id = price.get('canonical_id', '')
            platform = price.get('platform', '')
            status = price.get('mapping_status', '')

            if canonical_id and platform:
                by_canonical[canonical_id][platform].append({
                    'price_data': price,
                    'status': status
                })

        candidate_pairs = []

        for canonical_id, platforms in by_canonical.items():
            if 'kalshi' in platforms and 'polymarket' in platforms:
                kalshi_entries = platforms['kalshi']
                polymarket_entries = platforms['polymarket']

                # Check if it's candidate (not confirmed)
                kalshi_candidate = any(e['status'] == 'candidate' for e in kalshi_entries)
                polymarket_candidate = any(e['status'] == 'candidate' for e in polymarket_entries)
                kalshi_confirmed = any(e['status'] == 'confirmed' for e in kalshi_entries)
                polymarket_confirmed = any(e['status'] == 'confirmed' for e in polymarket_entries)

                # Must be candidate and not confirmed
                if (kalshi_candidate or polymarket_candidate) and not (kalshi_confirmed or polymarket_confirmed):
                    k_data = kalshi_entries[0]['price_data']
                    p_data = polymarket_entries[0]['price_data']

                    k_yes = k_data.get('yes_price')
                    k_no = k_data.get('no_price')
                    p_yes = p_data.get('yes_price')
                    p_no = p_data.get('no_price')

                    if all(v is not None for v in [k_yes, k_no, p_yes, p_no]):
                        edge = 1.0 - min(k_yes, p_yes) - min(k_no, p_no)

                        candidate_pairs.append({
                            'canonical_id': canonical_id,
                            'kalshi_market': k_data.get('raw_market_id'),
                            'polymarket_market': p_data.get('raw_market_id'),
                            'edge_percent': round(edge * 100, 2),
                            'kalshi_status': kalshi_entries[0]['status'],
                            'polymarket_status': polymarket_entries[0]['status']
                        })

        return candidate_pairs

    def analyze_profitability(self, pairs):
        """Analyze which pairs are profitable after fees."""
        profitable = []
        unprofitable = []

        for pair in pairs:
            edge = pair['edge']
            k_fee = pair['kalshi_fee']
            p_fee = pair['polymarket_fee']

            # Net edge after paying both fees
            net_edge = edge - (k_fee + p_fee)

            result = {
                'kalshi_market': pair['kalshi_market'],
                'polymarket_market': pair['polymarket_market'],
                'gross_edge': pair['edge_percent'],
                'total_fees_percent': round((k_fee + p_fee) * 100, 2),
                'net_edge_percent': round(net_edge * 100, 2),
                'profitable': net_edge > 0.001
            }

            if net_edge > 0.001:
                profitable.append(result)
            else:
                unprofitable.append(result)

        return {
            'profitable': len(profitable),
            'unprofitable': len(unprofitable),
            'profitable_list': sorted(profitable, key=lambda x: x['net_edge_percent'], reverse=True),
            'unprofitable_list': sorted(unprofitable, key=lambda x: x['net_edge_percent'], reverse=True)[:10]
        }

    def run_audit(self):
        """Execute full audit."""
        print("=" * 90)
        print("ARBITER COMPREHENSIVE PRICE AUDIT")
        print("=" * 90)
        print(f"Timestamp: {self.timestamp.isoformat()}\n")

        # Fetch
        print("[1/5] Fetching live price data...")
        if not self.fetch_prices():
            print("FATAL: Cannot connect to API")
            return

        print(f"✓ Connected to {BASE_URL}/api/prices\n")

        # Freshness
        print("[2/5] Analyzing data freshness...")
        freshness = self.analyze_freshness()
        self._print_freshness(freshness)
        print()

        # Binary validity
        print("[3/5] Validating binary price constraints...")
        validity = self.analyze_binary_validity()
        self._print_validity(validity)
        print()

        # Confirmed pairs
        print("[4/5] Computing edges for CONFIRMED cross-platform pairs...")
        confirmed = self.find_confirmed_pairs()
        self._print_confirmed(confirmed)
        print()

        # Fee analysis
        print("[5/5] Analyzing profitability after fees...")
        if confirmed:
            prof_analysis = self.analyze_profitability(confirmed)
            self._print_profitability(prof_analysis)
        else:
            print("  No confirmed pairs to analyze")
        print()

        # Candidates
        print("[BONUS] Checking CANDIDATE (not yet confirmed) pairs...")
        candidates = self.find_candidate_pairs()
        self._print_candidates(candidates)
        print()

        print("=" * 90)
        print("Audit complete")
        print("=" * 90)

    def _print_freshness(self, data):
        print(f"  Total prices in API: {data.get('total', 0)}")
        print(f"  Kalshi prices: {data.get('by_platform', {}).get('kalshi', 0)}")
        print(f"  Polymarket prices: {data.get('by_platform', {}).get('polymarket', 0)}")
        print(f"\n  By mapping status:")
        for status, count in sorted(data.get('by_status', {}).items()):
            print(f"    {status}: {count}")
        print(f"\n  Stale prices (>2 min old): {data.get('stale', 0)}")

    def _print_validity(self, data):
        print(f"  Valid prices (yes+no ≈ 1.0): {data.get('valid', 0)}")
        print(f"  Invalid prices: {data.get('invalid', 0)}")
        if data.get('invalid_samples'):
            print(f"\n  Invalid price samples:")
            for sample in data['invalid_samples'][:3]:
                print(f"    {sample['market_id']} ({sample['platform']}): yes={sample['yes']} no={sample['no']} (sum={sample['sum']})")

    def _print_confirmed(self, pairs):
        print(f"  Total confirmed cross-platform pairs: {len(pairs)}")

        if not pairs:
            print("    (No confirmed pairs found)")
            return

        sorted_pairs = sorted(pairs, key=lambda p: p['edge'], reverse=True)

        print(f"\n  Top 20 confirmed pairs by edge size:")
        print(f"  {'#':<3} {'Edge %':>8} {'Kalshi Market':<40} {'Polymarket Market':<40}")
        print("  " + "-" * 90)

        for i, pair in enumerate(sorted_pairs[:20], 1):
            k_market = pair['kalshi_market'][:37] + "..." if len(pair['kalshi_market']) > 40 else pair['kalshi_market']
            p_market = pair['polymarket_market'][:37] + "..." if len(pair['polymarket_market']) > 40 else pair['polymarket_market']
            print(f"  {i:<3} {pair['edge_percent']:>7.2f}% {k_market:<40} {p_market:<40}")

        print(f"\n  Detailed top 5:")
        for i, pair in enumerate(sorted_pairs[:5], 1):
            print(f"\n  {i}. {pair['canonical_id']}")
            print(f"     Kalshi:     YES={pair['kalshi_yes']:.4f} NO={pair['kalshi_no']:.4f} (fee={pair['kalshi_fee']:.2%})")
            print(f"     Polymarket: YES={pair['polymarket_yes']:.4f} NO={pair['polymarket_no']:.4f} (fee={pair['polymarket_fee']:.2%})")
            print(f"     EDGE: {pair['edge_percent']:.2f}%")

    def _print_profitability(self, analysis):
        print(f"  Profitable after fees: {analysis['profitable']}")
        print(f"  Unprofitable: {analysis['unprofitable']}")

        if analysis['profitable_list']:
            print(f"\n  Profitable pairs:")
            for pair in analysis['profitable_list'][:10]:
                print(f"    Gross: {pair['gross_edge']:.2f}% | Fees: {pair['total_fees_percent']:.2f}% | Net: {pair['net_edge_percent']:.2f}%")
                print(f"      {pair['kalshi_market']} ↔ {pair['polymarket_market']}")
        else:
            print(f"\n  No profitable pairs found after fees")

    def _print_candidates(self, pairs):
        print(f"  Total candidate (unconfirmed) pairs: {len(pairs)}")

        if pairs:
            sorted_pairs = sorted(pairs, key=lambda p: p['edge_percent'], reverse=True)
            print(f"\n  Top 10 candidate pairs by edge:")
            for i, pair in enumerate(sorted_pairs[:10], 1):
                print(f"    {i}. {pair['edge_percent']:.2f}% | {pair['kalshi_market']} ↔ {pair['polymarket_market']}")


if __name__ == "__main__":
    audit = ComprehensiveAudit()
    audit.run_audit()
