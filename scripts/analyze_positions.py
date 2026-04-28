#!/usr/bin/env python3
"""
Analyze open positions and determine recovery strategy.
Shows what each position is, its likely outcome, and recovery options.
"""
import json
import urllib.request
from datetime import datetime

BASE = "http://localhost:8080"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

def fetch_kalshi_market(ticker):
    try:
        url = "%s/markets/%s" % (KALSHI_API, ticker)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("market", data)
    except Exception as e:
        return {"error": str(e)}

def main():
    print("=" * 70)
    print("POSITION ANALYSIS — %s" % datetime.now().isoformat())
    print("=" * 70)

    e = fetch("/api/executions")
    execs = e if isinstance(e, list) else e.get("executions", [])

    total_deployed = 0
    total_potential_recovery = 0

    for x in execs:
        arb_id = x.get("arb_id", "?")
        status = x.get("status", "?")
        opp = x.get("opportunity", {})
        leg_yes = x.get("leg_yes", {})
        leg_no = x.get("leg_no", {})
        cid = opp.get("canonical_id", "?")

        # Find which legs actually filled
        yes_filled = leg_yes.get("status") == "filled" and leg_yes.get("fill_qty", 0) > 0
        no_filled = leg_no.get("status") == "filled" and leg_no.get("fill_qty", 0) > 0
        yes_submitted = leg_yes.get("status") == "submitted"
        no_submitted = leg_no.get("status") == "submitted"

        if not (yes_filled or no_filled):
            if status == "failed":
                print("\n%s [%s]: FAILED — no fills, no money at risk" % (arb_id, cid[:50]))
                continue

        print("\n%s [%s]" % (arb_id, cid[:50]))
        print("  Status: %s" % status)

        # Analyze each filled leg
        for leg_name, leg in [("YES", leg_yes), ("NO", leg_no)]:
            if leg.get("status") not in ("filled", "submitted"):
                if leg.get("status") == "aborted":
                    print("  %s leg: ABORTED (no fill)" % leg_name)
                continue

            plat = leg.get("platform", "?")
            market_id = leg.get("market_id", "?")
            qty = leg.get("fill_qty", 0) or leg.get("quantity", 0)
            price = leg.get("fill_price", 0) or leg.get("price", 0)
            cost = price * qty

            print("  %s leg [%s]: %s %d @ $%.2f = $%.2f (market: %s)" % (
                leg_name, plat, leg.get("status"), qty, price, cost, market_id
            ))

            if leg.get("status") == "filled" and qty > 0:
                total_deployed += cost

                # Get current market price from Kalshi
                if plat == "kalshi":
                    km = fetch_kalshi_market(market_id)
                    k_status = km.get("status", "?")
                    k_result = km.get("result", "?")
                    k_yes = km.get("yes_bid", 0) or 0
                    k_no = km.get("no_bid", 0) or 0
                    k_close = km.get("close_time", "?")

                    print("    Market status: %s, result: %s" % (k_status, k_result))
                    print("    Current prices: yes=$%.2f no=$%.2f" % (k_yes/100 if k_yes > 1 else k_yes, k_no/100 if k_no > 1 else k_no))
                    print("    Closes: %s" % k_close)

                    if k_result == "yes" and leg_name == "YES":
                        recovery = qty * 1.0 - cost
                        print("    SETTLED YES — recover $%.2f" % (qty * 1.0))
                        total_potential_recovery += qty * 1.0
                    elif k_result == "no" and leg_name == "NO":
                        recovery = qty * 1.0 - cost
                        print("    SETTLED NO — recover $%.2f" % (qty * 1.0))
                        total_potential_recovery += qty * 1.0
                    elif k_result == "yes" and leg_name == "NO":
                        print("    SETTLED YES — NO leg loses $%.2f" % cost)
                    elif k_result == "no" and leg_name == "YES":
                        print("    SETTLED NO — YES leg loses $%.2f" % cost)
                    elif k_status in ("open", "active"):
                        # Could potentially sell back
                        sell_price = k_yes/100 if leg_name == "YES" and k_yes > 1 else k_no/100 if leg_name == "NO" and k_no > 1 else 0
                        if sell_price > 0:
                            recovery = sell_price * qty
                            print("    Could sell back at ~$%.2f per contract (recover ~$%.2f)" % (sell_price, recovery))
                            total_potential_recovery += recovery
                        else:
                            print("    Position open, awaiting settlement")
                            total_potential_recovery += cost  # Assume breakeven

    pnl = fetch("/api/pnl")
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("Total deployed capital: $%.2f" % total_deployed)
    print("Potential recovery: $%.2f" % total_potential_recovery)
    print("Current balance: K=$%.2f P=$%.2f = $%.2f" % (
        pnl["current_balances"]["kalshi"],
        pnl["current_balances"]["polymarket"],
        pnl["total_balance"],
    ))
    print("Starting balance: $%.2f" % (pnl["starting_balances"]["kalshi"] + pnl["starting_balances"]["polymarket"]))
    print("Net change: $%.2f" % pnl["net_balance_change"])


if __name__ == "__main__":
    main()
