#!/usr/bin/env python3
"""Analyze the 280 remaining mappings that need NO conids."""
import asyncio, asyncpg, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.collectors.forecastex import ForecastExClient
from arbiter.config.settings import ForecastExConfig

POLITICAL_SYMBOLS = {"SROHM", "SRPAM", "SRMIR", "SRMTS", "SRFLM", "SENM",
                     "PRPA", "GROH", "FEDRO", "HORC"}

async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL",
        "postgresql://arbiter:arbiter_secret@arbiter-postgres-prod:5432/arbiter_live"))

    cfg = ForecastExConfig()
    client = ForecastExClient(gateway_url=cfg.gateway_url, account_id=cfg.account_id,
                              verify_ssl=cfg.verify_ssl, paper_trading=cfg.paper_trading)
    await client._ensure_iserver_bridge()

    # Get unique remaining conids
    rows = await conn.fetch("""
        SELECT DISTINCT forecastex_contract_id
        FROM market_mappings
        WHERE forecastex_contract_id != $1 AND forecastex_contract_id != $2
          AND (forecastex_no_contract_id = $1 OR forecastex_no_contract_id IS NULL)
          AND forecastex_not_available = FALSE
    """, '', '0')

    print(f"Remaining: {len(rows)} unique parent conids\n")

    political_parents = []
    econ_parents = []

    for r in rows:
        conid = r[0]
        info = await client.get_contract_info(conid)
        sym = info.get("symbol", "?")
        name = (info.get("company_name") or "")[:45]
        itype = info.get("instrument_type", "?")

        cnt = await conn.fetchval(
            "SELECT count(*) FROM market_mappings WHERE forecastex_contract_id = $1", conid)

        is_political = sym in POLITICAL_SYMBOLS or any(
            kw in name.upper() for kw in ("SENATE", "HOUSE", "GOVERNOR", "RACE", "ELECTION", "PRESIDENT")
        )

        tag = "POLITICAL" if is_political else "ECONOMIC"
        print(f"  {conid:>12s}  {sym:8s}  {itype:4s}  x{cnt:<4d}  {tag:10s}  {name}")

        if is_political:
            political_parents.append((conid, sym, name, cnt))
        else:
            econ_parents.append((conid, sym, name, cnt))

        await asyncio.sleep(0.12)

    pol_mappings = sum(p[3] for p in political_parents)
    econ_mappings = sum(p[3] for p in econ_parents)
    print(f"\nPolitical: {len(political_parents)} parents, {pol_mappings} mappings")
    print(f"Economic:  {len(econ_parents)} parents, {econ_mappings} mappings")
    print(f"\nNote: Economic parents (CPI, GDP, FF, etc.) are a known IBKR limitation —")
    print(f"the Client Portal API does not expose child OPT conids for multi-outcome events.")
    print(f"Political parents have binary children but secdef/info can't find them either.")
    print(f"The only proven method is adjacent-conid scan from known child OPT conids.")

    await client.close()
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
