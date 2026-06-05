#!/usr/bin/env python3
"""Probe IBKR secdef endpoints to find NO conids for known YES conids.

Diagnostic script to understand which endpoint/format works for
discovering Put siblings of known Call (YES) contracts.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.collectors.forecastex import ForecastExClient
from arbiter.config.settings import ForecastExConfig


async def main():
    cfg = ForecastExConfig()
    client = ForecastExClient(
        gateway_url=cfg.gateway_url,
        account_id=cfg.account_id,
        verify_ssl=cfg.verify_ssl,
        paper_trading=cfg.paper_trading,
    )
    await client._ensure_iserver_bridge()
    print(f"Bridge ready: {client._bridge_ready}")

    # Test conids: pick a few known YES conids from different categories
    test_conids = [
        ("762089343", "DEM_HOUSE_2026"),     # HORC political
        ("745924267", "DEM_SENATE_2026"),     # SENM political
        ("773659700", "GOP_HOUSE_2026"),      # HORC political (GOP side)
    ]

    for yes_conid, label in test_conids:
        print(f"\n{'='*60}")
        print(f"Testing YES conid {yes_conid} ({label})")
        print(f"{'='*60}")

        # Step 1: contract_info
        info = await client.get_contract_info(yes_conid)
        if not info:
            print("  contract_info: EMPTY")
            continue

        parent = (
            info.get("underlying_con_id")
            or info.get("underlyingConid")
            or info.get("undConid")
            or info.get("underconid")
        )
        strike = info.get("strike")
        right = info.get("right")
        local_sym = info.get("local_symbol", "")
        contract_month = info.get("contract_month", "")
        maturity = info.get("maturity_date", "")
        symbol = info.get("symbol", "")

        print(f"  parent={parent} strike={strike} right={right}")
        print(f"  local_symbol={local_sym}")
        print(f"  contract_month={contract_month} maturity={maturity} symbol={symbol}")

        if not parent:
            print("  SKIP: no parent conid")
            continue

        # Step 2: Try resolve_event_children via the client
        print(f"\n  Calling resolve_event_children(parent={parent})...")
        try:
            children = await client.resolve_event_children(str(parent))
            print(f"  Found {len(children)} children:")
            for c in children:
                print(f"    conid={c.get('conid')} right={c.get('right')} "
                      f"strike={c.get('strike')} desc={c.get('desc', '')[:50]}")
        except Exception as e:
            print(f"  resolve_event_children FAILED: {e}")
            children = []

        # Step 3: Try direct secdef/info with various month formats
        if contract_month:
            # contract_month is like "202611" -> try NOV26
            months_map = {
                "01": "JAN", "02": "FEB", "03": "MAR", "04": "APR",
                "05": "MAY", "06": "JUN", "07": "JUL", "08": "AUG",
                "09": "SEP", "10": "OCT", "11": "NOV", "12": "DEC",
            }
            yyyy = contract_month[:4]
            mm = contract_month[4:6]
            month_str = months_map.get(mm, "???") + yyyy[2:]
            print(f"\n  Trying direct secdef/info with month={month_str} strike={strike}...")

            try:
                payload = await client._request(
                    "GET", "/iserver/secdef/info",
                    params={
                        "conid": str(parent),
                        "exchange": "FORECASTX",
                        "sectype": "OPT",
                        "month": month_str,
                        "strike": str(strike),
                    },
                    use_circuit=False,
                )
                items = payload.get("items") if isinstance(payload, dict) else payload
                if isinstance(items, list):
                    print(f"  Direct secdef/info returned {len(items)} items:")
                    for item in items:
                        print(f"    conid={item.get('conid')} right={item.get('right')} "
                              f"strike={item.get('strike')}")
                else:
                    print(f"  Direct secdef/info: {json.dumps(payload, default=str)[:200]}")
            except Exception as e:
                print(f"  Direct secdef/info FAILED: {e}")

        # Step 4: Try trsrv/secdef for more info
        print(f"\n  Trying trsrv/secdef for parent {parent}...")
        try:
            sd = await client.get_trsrv_secdef(str(parent))
            print(f"  trsrv/secdef: hasOptions={sd.get('hasOptions')} "
                  f"assetClass={sd.get('assetClass')} type={sd.get('type')} "
                  f"name={sd.get('name', '')[:50]}")
        except Exception as e:
            print(f"  trsrv/secdef FAILED: {e}")

        await asyncio.sleep(2)  # Rate limit between test conids

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
