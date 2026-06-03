#!/usr/bin/env python3
"""Probe IBKR gateway for all economic FX families and Kalshi for matching events.

Quick diagnostic to see what FX contracts exist and what Kalshi events match.
Runs inside Docker against the live gateway.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.collectors.forecastex import ForecastExClient


FX_FAMILIES = [
    "FF", "CPIY", "UNR", "RGDP", "IJC", "RSM", "HS", "BPMI",
    "PREMP", "CP", "ND", "GT", "GCE", "PPIY", "PCEY", "JOLT", "NHS",
]


async def main():
    client = ForecastExClient(
        gateway_url=os.getenv("IBKR_GATEWAY_URL", "https://host.docker.internal:5000/v1/api"),
        account_id=os.getenv("IBKR_ACCOUNT_ID", "U25953084"),
        verify_ssl=False,
        paper_trading=False,
    )

    print("=== ForecastEx Economic Families Probe ===\n")
    all_hits = {}

    for sym in FX_FAMILIES:
        try:
            payload = await client._request(
                "POST", "/iserver/secdef/search",
                json_body={"symbol": sym, "name": True},
            )
            items = payload.get("items", []) if isinstance(payload, dict) else (
                payload if isinstance(payload, list) else []
            )
            fx_hits = [
                i for i in items
                if isinstance(i, dict)
                and "FORECASTX" in str(i.get("companyHeader", "")).upper()
            ]
            all_hits[sym] = fx_hits
            print(f"\n{sym}: {len(fx_hits)} FORECASTX events")
            for h in fx_hits[:5]:
                conid = h.get("conid", "?")
                symbol = h.get("symbol", "?")
                header = str(h.get("companyHeader", ""))[:100]
                print(f"  conid={conid}  sym={symbol:<12s}  {header}")

                # Try resolving children for the first hit
                if fx_hits.index(h) == 0:
                    try:
                        children = await client.resolve_event_children(str(conid))
                        if children:
                            print(f"    -> {len(children)} children:")
                            for c in children[:6]:
                                print(f"       conid={c.get('conid')} right={c.get('right')} "
                                      f"strike={c.get('strike')} desc={str(c.get('desc',''))[:60]}")
                        else:
                            print(f"    -> no children (multi-outcome or unsupported)")
                    except Exception as e:
                        print(f"    -> resolve failed: {e}")

        except Exception as e:
            all_hits[sym] = []
            print(f"\n{sym}: ERROR - {e}")

    # Summary
    print("\n\n=== SUMMARY ===")
    total = sum(len(v) for v in all_hits.values())
    print(f"Total FORECASTX economic events: {total}")
    for sym in FX_FAMILIES:
        count = len(all_hits.get(sym, []))
        if count:
            print(f"  {sym:<8s}: {count:3d} events")

    if client.session:
        await client.session.close()


if __name__ == "__main__":
    asyncio.run(main())
