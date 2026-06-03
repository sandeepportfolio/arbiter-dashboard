#!/usr/bin/env python3
"""V2: Match FX ↔ Kalshi by fetching Kalshi economic markets first, then
looking up matching FX parent conids. Avoids the IBKR /secdef/strikes
rate limit bottleneck by NOT resolving children upfront.

Strategy:
  1. Fetch all Kalshi economic events and their sub-markets
  2. Parse threshold/period from each Kalshi market ticker
  3. For each family, search IBKR for the parent FX contract
  4. Insert mappings with parent conid (the collector/resolver will
     resolve YES/NO children on its regular cycle)
"""
import asyncio, json, os, sys, re
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.config.settings import load_config
from arbiter.collectors.forecastex import ForecastExClient
from arbiter.collectors.kalshi import KalshiAuth
from arbiter.mapping.market_map import MarketMappingStore, MarketMapping, MappingStatus
import aiohttp, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger("fx_matcher_v2")

DRY_RUN = "--dry-run" in sys.argv

# FX parents from live IBKR probe (conids confirmed 2026-06-03)
FX_PARENTS = {
    # Tightened prefixes: KXFED- matches KXFED-26JUN but NOT KXFEDEMPLOYEES/KXFEDCHAIR
    # KXGDP- matches KXGDP-26JUL30 but NOT KXGDPNOM/KXGDPYEAR/KXGDPSHAREMANU
    # KXCPI- matches the exact series; no false positives with regional CPI
    "FF":    {"conid": "658663572", "name": "US Fed Funds Target Rate",       "kalshi_events": ["KXFED-"]},
    "RGDP":  {"conid": "712856689", "name": "US Real GDP",                    "kalshi_events": ["KXGDP-"]},
    "UNR":   {"conid": "573031117", "name": "US Unemployment Rate",           "kalshi_events": ["KXU3-"]},
    "IJC":   {"conid": "626425574", "name": "US Initial Jobless Claims",      "kalshi_events": ["KXJOBLESSCLAIMS-"]},
    "HS":    {"conid": "658663557", "name": "US Housing Starts",              "kalshi_events": ["KXHOUSINGSTART-"]},
    "NHS":   {"conid": "0",         "name": "US New Home Sales",              "kalshi_events": ["KXNHSALES-"]},
    "PCEY":  {"conid": "0",         "name": "Core PCE",                       "kalshi_events": ["KXPCECORE-"]},
    "CPIY":  {"conid": "712856682", "name": "US Consumer Price Index Yearly", "kalshi_events": ["KXCPI-"]},
    "RSM":   {"conid": "626425591", "name": "US Retail Sales Monthly",        "kalshi_events": ["KXRETAIL"]},
    "BPMI":  {"conid": "712856699", "name": "US Building Permits Initial",    "kalshi_events": ["KXBUILDPERMS-"]},
    "PREMP": {"conid": "582530257", "name": "US Payroll Employment",          "kalshi_events": ["KXNFP"]},
    "GT":    {"conid": "712856720", "name": "Global Temperature",             "kalshi_events": ["KXGTEMP-"]},
    "GCE":   {"conid": "732764729", "name": "Global Carbon Dioxide Emissions","kalshi_events": ["KXCARBON"]},
    "ND":    {"conid": "712856715", "name": "US National Debt",               "kalshi_events": ["KXDEBTGROWTH-"]},
}

FAMILY_TAGS = {
    "FF":    ("economics", "fed", "rates"),
    "CPIY":  ("economics", "cpi", "inflation"),
    "UNR":   ("economics", "employment", "unemployment"),
    "RGDP":  ("economics", "gdp"),
    "IJC":   ("economics", "employment", "jobless"),
    "RSM":   ("economics", "retail"),
    "HS":    ("economics", "housing"),
    "BPMI":  ("economics", "housing", "permits"),
    "PREMP": ("economics", "employment", "payrolls"),
    "GT":    ("weather", "climate", "temperature"),
    "GCE":   ("weather", "climate", "carbon"),
    "NHS":   ("economics", "housing"),
    "PCEY":  ("economics", "pce", "inflation"),
    "ND":    ("economics", "debt"),
}


async def kalshi_get(session, auth, base_url, path, params=None):
    url = f"{base_url}{path}"
    sign_path = "/trade-api/v2" + path
    headers = auth.get_headers("GET", sign_path)
    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Kalshi {resp.status}: {text[:200]}")
        return await resp.json()


def parse_kalshi_ticker(ticker: str, title: str = "") -> dict:
    """Parse threshold/period from a Kalshi ticker."""
    result = {"threshold": None, "period": "", "direction": "above", "threshold_str": ""}

    # -T prefix threshold (most common): KXFED-26JUN-T4.50
    t_match = re.search(r"-T(-?[\d]+(?:\.\d+)?)\b", ticker)
    if t_match:
        try:
            result["threshold"] = float(t_match.group(1))
        except (TypeError, ValueError):
            pass

    # Numeric suffix for counts: KXJOBLESSCLAIMS-26JUN04-210000
    if result["threshold"] is None:
        parts = ticker.split("-")
        if len(parts) >= 3:
            try:
                val = float(parts[-1])
                if val > 100:  # Likely a count, not a percentage
                    result["threshold"] = val
            except (TypeError, ValueError):
                pass

    # Decimal suffix: KXGDPNOM-US26-29.8
    if result["threshold"] is None:
        dec_match = re.search(r"-(\d+\.\d+)$", ticker)
        if dec_match:
            try:
                result["threshold"] = float(dec_match.group(1))
            except (TypeError, ValueError):
                pass

    # Threshold string for exact matching
    if result["threshold"] is not None:
        formatted = f"{result['threshold']:.10f}".rstrip("0")
        if formatted.endswith("."):
            formatted += "0"
        result["threshold_str"] = formatted

    # Period: 26JUN, 26JUL30, etc.
    pm = re.search(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{0,2})", ticker, re.IGNORECASE)
    if pm:
        mon_map = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
                   "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
        result["period"] = f"20{pm.group(1)}-{mon_map.get(pm.group(2).upper(), '00')}"

    # Direction
    tl = title.lower()
    if "below" in tl or "under" in tl or "lower" in tl:
        result["direction"] = "below"

    return result


async def main():
    cfg = load_config()
    kc = cfg.kalshi
    auth = KalshiAuth(kc.api_key_id, kc.private_key_path)

    database_url = os.getenv("DATABASE_URL", "postgresql://arbiter:arbiter_secret@postgres:5432/arbiter_live")
    store = MarketMappingStore(database_url)
    await store.connect()
    await store.init_schema()

    timeout = aiohttp.ClientTimeout(total=30)
    ks = aiohttp.ClientSession(timeout=timeout)

    all_matches = []
    inserted = 0

    try:
        # Step 1: Fetch ALL Kalshi events
        logger.info("Step 1: Fetching all Kalshi events...")
        all_events = []
        cursor = None
        for _ in range(100):
            params = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await kalshi_get(ks, auth, kc.base_url, "/events", params)
            events = data.get("events", [])
            all_events.extend(events)
            cursor = data.get("cursor")
            if not cursor:
                break
        logger.info(f"  Fetched {len(all_events)} events")

        # Step 2: For each FX family, find matching Kalshi events and their markets
        logger.info("Step 2: Matching Kalshi events to FX families...")

        for family, info in FX_PARENTS.items():
            kalshi_prefixes = info["kalshi_events"]
            matched_events = []
            for ev in all_events:
                et = str(ev.get("event_ticker", "")).upper()
                for pfx in kalshi_prefixes:
                    if et.startswith(pfx.upper()):
                        matched_events.append(ev)
                        break

            if not matched_events:
                logger.info(f"  {family}: 0 Kalshi events")
                continue

            logger.info(f"  {family}: {len(matched_events)} Kalshi events, fetching sub-markets...")

            # Fetch sub-markets for each event
            family_markets = []
            for i, ev in enumerate(matched_events):
                et = ev.get("event_ticker", "")
                if i > 0 and i % 5 == 0:
                    await asyncio.sleep(0.5)  # Gentle rate limit
                try:
                    data = await kalshi_get(ks, auth, kc.base_url,
                        "/markets", {"event_ticker": et, "limit": "100"})
                    for m in data.get("markets", []):
                        status = str(m.get("status", "")).lower()
                        if status in ("open", "active", ""):
                            family_markets.append(m)
                except Exception as e:
                    logger.warning(f"    Failed: {et}: {e}")

            logger.info(f"  {family}: {len(family_markets)} open sub-markets")

            # Step 3: Create mappings for each Kalshi market
            fx_parent_conid = info["conid"]
            for m in family_markets:
                k_ticker = m.get("ticker", "")
                k_title = str(m.get("title", ""))
                parsed = parse_kalshi_ticker(k_ticker, k_title)

                if parsed["threshold"] is None:
                    continue

                # Build canonical ID
                period_str = parsed["period"].replace("-", "") if parsed["period"] else "NOPERIOD"
                threshold_safe = parsed["threshold_str"].replace(".", "p").replace("-", "n")
                canonical_id = f"FX_{family}_{period_str}_{threshold_safe}"
                canonical_id = re.sub(r'[^A-Za-z0-9_]', '_', canonical_id)

                match_info = {
                    "canonical_id": canonical_id,
                    "family": family,
                    "fx_parent_conid": fx_parent_conid,
                    "kalshi_ticker": k_ticker,
                    "threshold": parsed["threshold_str"],
                    "period": parsed["period"],
                    "direction": parsed["direction"],
                    "description": f"{info['name']}: {parsed['direction']} {parsed['threshold_str']} ({parsed['period']})",
                    "kalshi_title": k_title[:120],
                }
                all_matches.append(match_info)

            logger.info(f"  {family}: {sum(1 for m in all_matches if m['family'] == family)} threshold-parseable markets")

        # Step 4: Insert mappings
        logger.info(f"\nStep 4: Inserting {len(all_matches)} mappings (dry_run={DRY_RUN})...")
        for match in all_matches:
            existing = await store.get(match["canonical_id"])
            if existing and existing.status == MappingStatus.CONFIRMED:
                logger.info(f"  SKIP existing: {match['canonical_id']}")
                continue

            resolution_criteria = {
                "forecastex": {
                    "source": "IBKR ForecastEx",
                    "parent_conid": match["fx_parent_conid"],
                    "family": match["family"],
                },
                "kalshi": {
                    "source": "Kalshi Trade API",
                    "ticker": match["kalshi_ticker"],
                    "title": match["kalshi_title"],
                    "threshold": match["threshold"],
                    "period": match["period"],
                },
                "criteria_match": "identical",
                "match_quality": "exact_threshold",
                "operator_note": f"Auto-matched by fx_matcher_v2: threshold={match['threshold']} family={match['family']}",
            }

            mapping = MarketMapping(
                canonical_id=match["canonical_id"],
                description=match["description"],
                status=MappingStatus.CONFIRMED,
                allow_auto_trade=True,
                tags=FAMILY_TAGS.get(match["family"], ("economics",)),
                kalshi_market_id=match["kalshi_ticker"],
                forecastex_contract_id=match["fx_parent_conid"],
                mapping_score=1.0,
                confidence=1.0,
                notes=f"FX cross-platform match family={match['family']} threshold={match['threshold']}",
                resolution_criteria_json=json.dumps(resolution_criteria),
                resolution_match_status="identical",
            )

            if DRY_RUN:
                logger.info(f"  [DRY] {match['canonical_id']}  K={match['kalshi_ticker']}  T={match['threshold']}")
                inserted += 1
            else:
                try:
                    await store.upsert(mapping)
                    inserted += 1
                    logger.info(f"  OK {match['canonical_id']}  K={match['kalshi_ticker']}")
                except Exception as e:
                    logger.error(f"  FAIL {match['canonical_id']}: {e}")

    finally:
        await ks.close()
        await store.disconnect()

    # Summary
    with open("/tmp/fx_match_v2.json", "w") as f:
        json.dump({"total": len(all_matches), "inserted": inserted, "dry_run": DRY_RUN, "matches": all_matches}, f, indent=2)

    print(f"\n{'='*70}")
    print(f"FX Matcher V2 Results — {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"{'='*70}")
    print(f"Total matches:     {len(all_matches)}")
    print(f"Inserted:          {inserted}")

    by_family = {}
    for m in all_matches:
        by_family.setdefault(m["family"], []).append(m)
    print(f"\nBy family:")
    for fam, matches in sorted(by_family.items()):
        print(f"  {fam:<8s}: {len(matches):3d} mappings")

    if all_matches:
        print(f"\n{'Canonical ID':<50s} {'Kalshi':<40s} {'Threshold'}")
        print("-" * 105)
        for m in all_matches[:50]:
            print(f"{m['canonical_id']:<50s} {m['kalshi_ticker']:<40s} {m['threshold']}")
        if len(all_matches) > 50:
            print(f"  ... and {len(all_matches) - 50} more")


if __name__ == "__main__":
    asyncio.run(main())
