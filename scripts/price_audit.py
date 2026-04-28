#!/usr/bin/env python3
"""
Comprehensive Price Audit for Arbiter Prediction Market System
Fetches live prices from localhost:8080 and analyzes:
1. Data freshness and completeness
2. Price validity (yes + no ≈ 1.0)
3. Arbitrage edges for all 23 confirmed mappings
4. Fee impact analysis
5. Missed opportunities
"""

import requests
import json
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import time

# 23 confirmed mappings (4 political + 19 sports)
# Format: (kalshi_ticker, polymarket_slug, polarity_flip)
CONFIRMED_MAPPINGS = [
    # Political (4)
    ("HOUSE.DEM-2026-11", "aec-2026-us-house-democrats-control", False),
    ("HOUSE.GOP-2026-11", "aec-2026-us-house-republicans-control", False),
    ("SENATE.DEM-2026-11", "aec-2026-us-senate-democrats-control", False),
    ("SENATE.GOP-2026-11", "aec-2026-us-senate-republicans-control", False),

    # Sports (19)
    # MLB (1)
    ("KXMLBGAME-HOU-BAL-2026-04-27", "aec-mlb-hou-bal-2026-04-27", False),

    # NHL (1)
    ("KXNHLGAME-PHI-MTL-2026-04-26", "aec-nhl-phi-mtl-2026-04-26", False),

    # Bundesliga (6)
    ("KXBUNDESLIGAGAME-FCB-BAY-2026-04-25", "atc-bun-bar-bay-2026-04-25", False),
    ("KXBUNDESLIGAGAME-BVB-MGL-2026-04-25", "atc-bun-bvb-mgl-2026-04-25", False),
    ("KXBUNDESLIGAGAME-VFB-HAN-2026-04-26", "atc-bun-vfb-han-2026-04-26", False),
    ("KXBUNDESLIGAGAME-LEV-HOF-2026-04-26", "atc-bun-lev-hof-2026-04-26", False),
    ("KXBUNDESLIGAGAME-SGE-KOL-2026-04-26", "atc-bun-sge-kol-2026-04-26", False),
    ("KXBUNDESLIGAGAME-MAI-BSC-2026-04-26", "atc-bun-mai-bsc-2026-04-26", False),

    # La Liga (3)
    ("KXLALIGAGAME-RMA-ALC-2026-04-26", "atc-lal-rma-alc-2026-04-26", False),
    ("KXLALIGAGAME-FCB-VLL-2026-04-26", "atc-lal-fcb-vll-2026-04-26", False),
    ("KXLALIGAGAME-ATM-BET-2026-04-26", "atc-lal-atm-bet-2026-04-26", False),

    # Serie A (5)
    ("KXSERIEAGAME-JUV-ROM-2026-04-26", "atc-sea-juv-rom-2026-04-26", False),
    ("KXSERIEAGAME-INT-LAZ-2026-04-27", "atc-sea-int-laz-2026-04-27", False),
    ("KXSERIEAGAME-ACM-NAP-2026-04-27", "atc-sea-acm-nap-2026-04-27", False),
    ("KXSERIEAGAME-ATA-FIO-2026-04-27", "atc-sea-ata-fio-2026-04-27", False),
    ("KXSERIEAGAME-SAM-PAR-2026-04-27", "atc-sea-sam-par-2026-04-27", False),

    # MLS (3)
    ("KXMLSGAME-LAG-SEA-2026-04-25", "atc-mls-lag-sea-2026-04-25", False),
    ("KXMLSGAME-COL-SJ-2026-04-26", "atc-mls-col-sj-2026-04-26", False),
    ("KXMLSGAME-NYC-ATX-2026-04-27", "atc-mls-nyc-atx-2026-04-27", False),
]


class PriceAuditor:
    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url
        self.raw_prices = None
        self.timestamp = None

    def fetch_prices(self) -> bool:
        """Fetch all prices from the API."""
        try:
            response = requests.get(f"{self.base_url}/api/prices", timeout=5)
            response.raise_for_status()
            self.raw_prices = response.json()
            self.timestamp = datetime.now()
            return True
        except Exception as e:
            print(f"ERROR: Failed to fetch prices: {e}")
            return False

    def _normalize_prices(self) -> Dict[str, Dict]:
        """Convert API dict format to normalized format by slug/ticker."""
        normalized = {}

        for key, price_data in self.raw_prices.items():
            # Extract platform and identifier
            platform = price_data.get("platform", "")

            # Try multiple possible identifiers
            slug = price_data.get("raw_market_id") or price_data.get("metadata", {}).get("slug") or ""

            if not slug:
                continue

            if slug not in normalized:
                normalized[slug] = {}

            normalized[slug][platform] = price_data

        return normalized

    def analyze_data_freshness(self) -> Dict:
        """Check how many prices are present and their age."""
        if not self.raw_prices:
            return {"error": "No price data"}

        prices = list(self.raw_prices.values())

        kalshi_count = 0
        polymarket_count = 0
        stale_prices = []

        for price in prices:
            platform = price.get("platform", "")
            timestamp = price.get("timestamp", 0)

            if platform == "kalshi":
                kalshi_count += 1
            elif platform == "polymarket":
                polymarket_count += 1

            # Check staleness (older than 2 minutes)
            price_age = (self.timestamp.timestamp() - timestamp) / 60
            if price_age > 2:
                stale_prices.append({
                    "identifier": price.get("raw_market_id", "unknown"),
                    "platform": platform,
                    "age_minutes": round(price_age, 1)
                })

        return {
            "total_prices": len(prices),
            "kalshi_count": kalshi_count,
            "polymarket_count": polymarket_count,
            "stale_prices": stale_prices[:10],  # First 10
            "stale_count": len(stale_prices),
            "fetch_time": self.timestamp.isoformat()
        }

    def validate_binary_prices(self) -> Dict:
        """Check if yes_price + no_price ≈ 1.0 for each market."""
        if not self.raw_prices:
            return {"error": "No price data"}

        validation_results = []
        invalid_count = 0

        for price in self.raw_prices.values():
            yes_price = price.get("yes_price")
            no_price = price.get("no_price")

            if yes_price is not None and no_price is not None:
                total = yes_price + no_price
                # Allow 5% tolerance for 3-way markets and fee impacts
                if abs(total - 1.0) > 0.05:
                    validation_results.append({
                        "identifier": price.get("raw_market_id", "unknown"),
                        "platform": price.get("platform", ""),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "sum": total
                    })
                    invalid_count += 1

        return {
            "total_prices": len([p for p in self.raw_prices.values()
                                if p.get("yes_price") is not None and p.get("no_price") is not None]),
            "invalid_count": invalid_count,
            "invalid_markets": validation_results[:10]
        }

    def compute_edges(self) -> List[Dict]:
        """
        Compute arbitrage edges for all 23 confirmed mappings.
        """
        if not self.raw_prices:
            return [{"error": "No price data"}]

        # Build a map of slugs to price data
        slug_map = {}
        for price_data in self.raw_prices.values():
            slug = price_data.get("raw_market_id") or price_data.get("metadata", {}).get("slug") or ""
            if slug:
                platform = price_data.get("platform", "")
                if slug not in slug_map:
                    slug_map[slug] = {}
                slug_map[slug][platform] = price_data

        edges = []

        for kalshi_ticker, polymarket_slug, is_flipped in CONFIRMED_MAPPINGS:
            # Try to find matching prices
            # Look for Kalshi price (by ticker)
            kalshi_price = None
            for slug, platforms in slug_map.items():
                if kalshi_ticker in slug and "kalshi" in platforms:
                    kalshi_price = platforms["kalshi"]
                    break

            # Look for Polymarket price (by slug)
            polymarket_price = None
            if polymarket_slug in slug_map and "polymarket" in slug_map[polymarket_slug]:
                polymarket_price = slug_map[polymarket_slug]["polymarket"]

            result = {
                "kalshi_ticker": kalshi_ticker,
                "polymarket_slug": polymarket_slug,
                "polarity_flipped": is_flipped,
                "has_kalshi_price": kalshi_price is not None,
                "has_polymarket_price": polymarket_price is not None
            }

            if kalshi_price and polymarket_price:
                k_yes = kalshi_price.get("yes_price")
                k_no = kalshi_price.get("no_price")
                p_yes = polymarket_price.get("yes_price")
                p_no = polymarket_price.get("no_price")

                if all(v is not None for v in [k_yes, k_no, p_yes, p_no]):
                    if not is_flipped:
                        # Standard calculation: same polarity
                        edge = 1.0 - min(k_yes, p_yes) - min(k_no, p_no)
                        trade_direction = {
                            "buy_yes_on": "Kalshi" if k_yes < p_yes else "Polymarket",
                            "buy_no_on": "Kalshi" if k_no < p_no else "Polymarket"
                        }
                    else:
                        # Flipped: Kalshi YES = Polymarket NO
                        edge = 1.0 - min(k_yes, p_no) - min(k_no, p_yes)
                        trade_direction = {
                            "buy_yes_on": "Polymarket (sell NO)" if k_yes < p_no else "Kalshi",
                            "buy_no_on": "Polymarket (sell YES)" if k_no < p_yes else "Kalshi"
                        }

                    result.update({
                        "kalshi_yes": round(k_yes, 4),
                        "kalshi_no": round(k_no, 4),
                        "polymarket_yes": round(p_yes, 4),
                        "polymarket_no": round(p_no, 4),
                        "edge": round(edge, 4),
                        "edge_percent": round(edge * 100, 2),
                        "trade_direction": trade_direction,
                        "kalshi_fee": kalshi_price.get("fee_rate", 0),
                        "polymarket_fee": polymarket_price.get("fee_rate", 0)
                    })
            else:
                result["missing_prices"] = []
                if not kalshi_price:
                    result["missing_prices"].append("kalshi")
                if not polymarket_price:
                    result["missing_prices"].append("polymarket")

            edges.append(result)

        return edges

    def analyze_fee_impact(self, edges: List[Dict]) -> Dict:
        """Analyze which edges are profitable after fees."""
        profitable = []
        unprofitable = []
        missing_data = []

        for edge_data in edges:
            if "edge" not in edge_data:
                missing_data.append(edge_data["kalshi_ticker"])
                continue

            edge = edge_data["edge"]
            k_fee = edge_data.get("kalshi_fee", 0.04)
            p_fee = edge_data.get("polymarket_fee", 0.02)

            # Net edge after both fees (conservative: assume we pay both)
            total_fees = k_fee + p_fee
            net_edge = edge - total_fees

            result = {
                "pair": f"{edge_data['kalshi_ticker']} ↔ {edge_data['polymarket_slug']}",
                "gross_edge": round(edge, 4),
                "kalshi_fee": round(k_fee, 4),
                "polymarket_fee": round(p_fee, 4),
                "total_fees": round(total_fees, 4),
                "net_edge": round(net_edge, 4),
                "profitable": net_edge > 0.001  # >0.1% profitable
            }

            if net_edge > 0.001:
                profitable.append(result)
            else:
                unprofitable.append(result)

        return {
            "profitable_count": len(profitable),
            "unprofitable_count": len(unprofitable),
            "missing_data_count": len(missing_data),
            "profitable_pairs": profitable,
            "unprofitable_pairs": unprofitable
        }

    def find_missed_opportunities(self) -> Dict:
        """Check for unconfirmed price pairs with edges."""
        if not self.raw_prices:
            return {"error": "No price data"}

        # Get all slugs
        all_slugs = set()
        kalshi_slugs = set()
        polymarket_slugs = set()

        for price in self.raw_prices.values():
            slug = price.get("raw_market_id") or price.get("metadata", {}).get("slug") or ""
            platform = price.get("platform", "")

            all_slugs.add(slug)
            if platform == "kalshi":
                kalshi_slugs.add(slug)
            elif platform == "polymarket":
                polymarket_slugs.add(slug)

        # Get confirmed
        confirmed_kalshi = {t for t, _, _ in CONFIRMED_MAPPINGS}
        confirmed_poly = {s for _, s, _ in CONFIRMED_MAPPINGS}

        unconfirmed_kalshi = kalshi_slugs - confirmed_kalshi
        unconfirmed_polymarket = polymarket_slugs - confirmed_poly

        return {
            "unconfirmed_kalshi_count": len(unconfirmed_kalshi),
            "unconfirmed_polymarket_count": len(unconfirmed_polymarket),
            "sample_unconfirmed_kalshi": sorted(list(unconfirmed_kalshi))[:5],
            "sample_unconfirmed_polymarket": sorted(list(unconfirmed_polymarket))[:5]
        }

    def run_full_audit(self) -> Dict:
        """Execute the complete audit."""
        print("=" * 80)
        print("ARBITER PRICE AUDIT - COMPREHENSIVE ANALYSIS")
        print("=" * 80)
        print(f"Timestamp: {datetime.now().isoformat()}\n")

        # Fetch data
        print("[1/5] Fetching live prices from localhost:8080...")
        if not self.fetch_prices():
            print("FATAL: Cannot connect to server")
            return {"error": "Connection failed"}
        print(f"✓ Data fetched successfully\n")

        # Freshness check
        print("[2/5] Analyzing data freshness and completeness...")
        freshness = self.analyze_data_freshness()
        self._print_freshness(freshness)
        print()

        # Binary validation
        print("[3/5] Validating binary price constraints (yes + no ≈ 1.0)...")
        validation = self.validate_binary_prices()
        self._print_validation(validation)
        print()

        # Edge computation
        print("[4/5] Computing arbitrage edges for all 23 confirmed mappings...")
        edges = self.compute_edges()
        self._print_edges(edges)
        print()

        # Fee analysis
        print("[5/5] Analyzing fee impact and profitability...")
        fee_analysis = self.analyze_fee_impact(edges)
        self._print_fee_analysis(fee_analysis)
        print()

        # Missed opportunities
        print("[BONUS] Checking for unconfirmed opportunities...")
        missed = self.find_missed_opportunities()
        self._print_missed_opportunities(missed)
        print()

        return {
            "freshness": freshness,
            "validation": validation,
            "edges": edges,
            "fee_analysis": fee_analysis,
            "missed_opportunities": missed,
            "audit_timestamp": self.timestamp.isoformat()
        }

    def _print_freshness(self, data: Dict):
        print(f"  Total prices: {data.get('total_prices', 0)}")
        print(f"  Kalshi prices: {data.get('kalshi_count', 0)}")
        print(f"  Polymarket prices: {data.get('polymarket_count', 0)}")
        print(f"  Stale prices (>2min old): {data.get('stale_count', 0)}")

        if data.get('stale_prices'):
            print(f"\n  Stale price samples:")
            for stale in data['stale_prices'][:3]:
                print(f"    {stale['identifier']} ({stale['platform']}): {stale['age_minutes']} min old")

    def _print_validation(self, data: Dict):
        print(f"  Valid binary prices: {data.get('total_prices', 0)}")
        print(f"  Invalid markets (yes+no not ≈ 1.0): {data.get('invalid_count', 0)}")

        if data.get('invalid_markets'):
            print(f"\n  Invalid market samples:")
            for invalid in data['invalid_markets'][:3]:
                print(f"    {invalid['identifier']} ({invalid['platform']}): "
                      f"yes={invalid['yes_price']:.4f} no={invalid['no_price']:.4f} sum={invalid['sum']:.4f}")

    def _print_edges(self, edges: List[Dict]):
        with_edges = [e for e in edges if "edge" in e]
        missing = [e for e in edges if "missing_prices" in e]

        print(f"  Confirmed mappings with BOTH prices: {len(with_edges)} / 23")
        print(f"  Missing at least one price: {len(missing)} / 23")

        if with_edges:
            print(f"\n  Top 15 edges by size:")
            sorted_edges = sorted(with_edges, key=lambda e: e.get("edge", 0), reverse=True)
            for i, edge in enumerate(sorted_edges[:15], 1):
                print(f"\n  {i}. {edge['kalshi_ticker']} ↔ {edge['polymarket_slug']}")
                print(f"     Kalshi:     YES={edge['kalshi_yes']:.4f} NO={edge['kalshi_no']:.4f}")
                print(f"     Polymarket: YES={edge['polymarket_yes']:.4f} NO={edge['polymarket_no']:.4f}")
                print(f"     EDGE: {edge['edge_percent']:.2f}%")
                if "trade_direction" in edge:
                    td = edge["trade_direction"]
                    print(f"     Trade: Buy YES on {td['buy_yes_on']}, Buy NO on {td['buy_no_on']}")

    def _print_fee_analysis(self, data: Dict):
        print(f"  Profitable pairs (net edge > 0.1%): {data['profitable_count']} / 23")
        print(f"  Unprofitable pairs: {data['unprofitable_count']} / 23")
        print(f"  Missing prices: {data['missing_data_count']} / 23")

        if data['profitable_pairs']:
            print(f"\n  Profitable pairs (sorted by net edge):")
            for pair in sorted(data['profitable_pairs'], key=lambda p: p['net_edge'], reverse=True)[:10]:
                print(f"    {pair['pair']}")
                print(f"      Gross: {pair['gross_edge']:.2%} | Fees: {pair['total_fees']:.2%} | Net: {pair['net_edge']:.2%}")

    def _print_missed_opportunities(self, data: Dict):
        print(f"  Unconfirmed Kalshi markets: {data.get('unconfirmed_kalshi_count', 0)}")
        print(f"  Unconfirmed Polymarket markets: {data.get('unconfirmed_polymarket_count', 0)}")


if __name__ == "__main__":
    auditor = PriceAuditor()
    result = auditor.run_full_audit()

    # Save to JSON
    with open("/tmp/arbiter_audit_result.json", "w") as f:
        json.dump(result, f, indent=2, default=str)

    print("\n" + "=" * 80)
    print(f"Audit complete. Results saved to: /tmp/arbiter_audit_result.json")
    print("=" * 80)
