#!/usr/bin/env python3
"""Minimal probe: search 3 FX families, write results to /tmp/fx_probe.json."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.collectors.forecastex import ForecastExClient

async def main():
    client = ForecastExClient(
        gateway_url=os.getenv("IBKR_GATEWAY_URL", "https://host.docker.internal:5000/v1/api"),
        account_id=os.getenv("IBKR_ACCOUNT_ID", "U25953084"),
        verify_ssl=False, paper_trading=False,
    )
    families = ["FF", "CPIY", "UNR", "RGDP", "IJC", "RSM", "HS", "BPMI",
                "PREMP", "CP", "ND", "GT", "GCE"]
    out = {}
    for sym in families:
        try:
            p = await client._request("POST", "/iserver/secdef/search",
                                      json_body={"symbol": sym, "name": True})
            items = p.get("items", []) if isinstance(p, dict) else (p if isinstance(p, list) else [])
            fx = [i for i in items if isinstance(i, dict) and "FORECASTX" in str(i.get("companyHeader","")).upper()]
            out[sym] = [{"conid": i.get("conid"), "symbol": i.get("symbol"),
                         "header": str(i.get("companyHeader",""))[:120]} for i in fx]
        except Exception as e:
            out[sym] = [{"error": str(e)}]
    if client.session:
        await client.session.close()
    with open("/tmp/fx_probe.json", "w") as f:
        json.dump(out, f, indent=2)
    total = sum(len(v) for v in out.values() if v and not (len(v)==1 and "error" in v[0]))
    print(f"OK: {total} FX events across {len(families)} families -> /tmp/fx_probe.json")

asyncio.run(main())
