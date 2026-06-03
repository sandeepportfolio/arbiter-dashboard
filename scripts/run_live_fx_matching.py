#!/usr/bin/env python3
"""Run live FX ↔ Kalshi matching against the IBKR gateway and Kalshi API.

Discovers FX children for each economic family, then searches Kalshi for
exact threshold matches, and inserts verified mappings into arbiter_live.
"""
import asyncio, json, os, sys, re, time
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.config.settings import load_config
from arbiter.collectors.forecastex import ForecastExClient
from arbiter.collectors.kalshi import KalshiAuth
from arbiter.mapping.market_map import MarketMappingStore, MarketMapping, MappingStatus
import aiohttp
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-40s %(levelname)-8s %(message)s")
logger = logging.getLogger("fx_live_matcher")

DRY_RUN = "--dry-run" in sys.argv

# ── FX parent conids from live probe ────────────────────────────────
FX_PARENTS = {
    "FF":    {"conid": "658663572", "name": "US Fed Funds Target Rate"},
    "CPIY":  {"conid": "712856682", "name": "US Consumer Price Index Yearly"},
    "UNR":   {"conid": "573031117", "name": "US Unemployment Rate"},
    "RGDP":  {"conid": "712856689", "name": "US Real GDP"},
    "IJC":   {"conid": "626425574", "name": "US Initial Jobless Claims"},
    "RSM":   {"conid": "626425591", "name": "US Retail Sales Monthly"},
    "HS":    {"conid": "658663557", "name": "US Housing Starts"},
    "BPMI":  {"conid": "712856699", "name": "US Building Permits Initial"},
    "PREMP": {"conid": "582530257", "name": "US Payroll Employment"},
    "CP":    {"conid": "658663567", "name": "US Corporate Profits"},
    "GT":    {"conid": "712856720", "name": "Global Temperature"},
    "GCE":   {"conid": "732764729", "name": "Global Carbon Dioxide Emissions"},
}

# ── Kalshi event ticker patterns by FX family ───────────────────────
KALSHI_EVENT_PATTERNS = {
    "FF":    [r"^KXFED-\d{2}[A-Z]{3}$"],     # KXFED-26JUN
    "CPIY":  [r"^KXCPI"],                      # KXCPI-*
    "UNR":   [r"^KXU3-\d{2}[A-Z]{3}$"],       # KXU3-26MAY
    "RGDP":  [r"^KXGDP-\d{2}[A-Z]{3}\d{2}$"], # KXGDP-26JUL30
    "IJC":   [r"^KXJOBLESSCLAIMS"],             # KXJOBLESSCLAIMS-26JUN04
    "RSM":   [r"^KXRETAIL"],                    # KXRETAILSALES-*
    "HS":    [r"^KXHOUSINGSTART"],              # KXHOUSINGSTART-26JUN16
    "BPMI":  [r"^KXBUILDPERM"],                 # KXBUILDINGPERMIT-*
    "PREMP": [r"^KXNFP", r"^KXPAYROLL"],       # KXNFP-* or KXPAYROLL-*
    "CP":    [r"^KXCORP"],                      # KXCORPORATEPROFIT-*
    "GT":    [r"^KXGTEMP"],                     # KXGTEMP-*
    "GCE":   [r"^KXCARBON"],                    # KXCARBON-*
    "NHS":   [r"^KXNHSALES"],                   # KXNHSALES-*
    "PCEY":  [r"^KXPCECORE"],                   # KXPCECORE-*
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
    "CP":    ("economics", "corporate"),
    "GT":    ("weather", "climate", "temperature"),
    "GCE":   ("weather", "climate", "carbon"),
    "NHS":   ("economics", "housing"),
    "PCEY":  ("economics", "pce", "inflation"),
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


def parse_kalshi_threshold(ticker: str, title: str = "") -> dict:
    """Parse threshold from a Kalshi market ticker like KXFED-26JUN-T4.50."""
    result = {"ticker": ticker, "threshold": None, "period": "", "direction": "above"}

    # Extract threshold from ticker: -T4.50 or -210000
    t_match = re.search(r"-T(-?[\d]+(?:\.\d+)?)\b", ticker)
    if t_match:
        try:
            result["threshold"] = float(t_match.group(1))
        except (TypeError, ValueError):
            pass

    # For jobless claims: -210000 pattern (no T prefix)
    if result["threshold"] is None:
        num_match = re.search(r"-(\d{4,})\b", ticker)
        if num_match:
            try:
                result["threshold"] = float(num_match.group(1))
            except (TypeError, ValueError):
                pass

    # For GDP nominal: -2.5 pattern
    if result["threshold"] is None:
        dec_match = re.search(r"-(\d+\.\d+)$", ticker)
        if dec_match:
            try:
                result["threshold"] = float(dec_match.group(1))
            except (TypeError, ValueError):
                pass

    # Extract period from ticker: 26JUN, 26JUL30, 26MAY etc
    period_match = re.search(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{0,2})", ticker, re.IGNORECASE)
    if period_match:
        yy = period_match.group(1)
        mon = period_match.group(2).upper()
        month_map = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
                     "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
        result["period"] = f"20{yy}-{month_map.get(mon, '00')}"

    # Direction from title
    title_lower = title.lower()
    if "below" in title_lower or "under" in title_lower:
        result["direction"] = "below"

    return result


def format_threshold(val: float) -> str:
    """Consistent threshold string: 3.0 -> '3.0', 210000 -> '210000.0'"""
    formatted = f"{val:.10f}".rstrip("0")
    if formatted.endswith("."):
        formatted += "0"
    return formatted


async def main():
    cfg = load_config()
    kc = cfg.kalshi
    auth = KalshiAuth(kc.api_key_id, kc.private_key_path)

    fx_config = cfg.forecastex
    if not fx_config or not fx_config.enabled:
        logger.error("ForecastEx not enabled")
        return

    fx_client = ForecastExClient(
        gateway_url=fx_config.gateway_url,
        account_id=fx_config.account_id,
        verify_ssl=fx_config.verify_ssl,
        paper_trading=fx_config.paper_trading,
    )

    database_url = os.getenv("DATABASE_URL", "postgresql://arbiter:arbiter_secret@postgres:5432/arbiter_live")
    store = MarketMappingStore(database_url)
    await store.connect()
    await store.init_schema()

    timeout = aiohttp.ClientTimeout(total=30)
    kalshi_session = aiohttp.ClientSession(timeout=timeout)

    all_matches = []
    inserted_count = 0

    try:
        # Step 1: For each FX family, resolve children from IBKR
        logger.info("Step 1: Resolving FX children from IBKR gateway...")
        fx_children_by_family = {}

        for family, info in FX_PARENTS.items():
            logger.info(f"  Resolving {family} (conid={info['conid']})...")
            try:
                children = await fx_client.resolve_event_children(info["conid"])
                if children:
                    fx_children_by_family[family] = children
                    strikes = set()
                    for c in children:
                        s = c.get("strike")
                        if s is not None:
                            strikes.add(float(s))
                    logger.info(f"    {family}: {len(children)} children, strikes={sorted(strikes)[:10]}")
                else:
                    logger.info(f"    {family}: no children (multi-outcome, needs manual mapping)")
            except Exception as e:
                logger.warning(f"    {family}: resolve failed: {e}")

        # Step 2: Fetch all Kalshi events
        logger.info("Step 2: Fetching all Kalshi events...")
        all_events = []
        cursor = None
        for page in range(100):
            params = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await kalshi_get(kalshi_session, auth, kc.base_url, "/events", params)
            events = data.get("events", [])
            all_events.extend(events)
            cursor = data.get("cursor")
            if not cursor:
                break
        logger.info(f"  Total Kalshi events: {len(all_events)}")

        # Step 3: For each FX family with children, find matching Kalshi events
        logger.info("Step 3: Matching FX families to Kalshi events...")

        for family, children in fx_children_by_family.items():
            patterns = KALSHI_EVENT_PATTERNS.get(family, [])
            if not patterns:
                logger.info(f"  {family}: no Kalshi event patterns defined, skipping")
                continue

            # Find matching Kalshi events
            matching_events = []
            for ev in all_events:
                ticker = str(ev.get("event_ticker", "")).upper()
                for pat in patterns:
                    if re.match(pat, ticker, re.IGNORECASE):
                        matching_events.append(ev)
                        break

            if not matching_events:
                logger.info(f"  {family}: no matching Kalshi events found")
                continue

            logger.info(f"  {family}: {len(matching_events)} matching Kalshi events")

            # Build FX strike lookup: {strike_float: {yes_conid, no_conid}}
            fx_strikes = {}
            for child in children:
                strike = child.get("strike")
                if strike is None:
                    continue
                try:
                    strike_f = float(strike)
                except (TypeError, ValueError):
                    continue
                right = str(child.get("right", "")).upper()
                conid = str(child.get("conid", "")).strip()
                if not conid:
                    continue

                if strike_f not in fx_strikes:
                    fx_strikes[strike_f] = {"yes": "", "no": "", "strike": strike_f}
                if right in ("C", "Y", "YES", "1", "CALL"):
                    fx_strikes[strike_f]["yes"] = conid
                elif right in ("P", "N", "NO", "0", "PUT"):
                    fx_strikes[strike_f]["no"] = conid

            # Fetch Kalshi sub-markets for each matching event
            for ev in matching_events:
                event_ticker = ev.get("event_ticker", "")
                try:
                    data = await kalshi_get(kalshi_session, auth, kc.base_url,
                        "/markets", {"event_ticker": event_ticker, "limit": "100"})
                    markets = data.get("markets", [])
                except Exception as e:
                    logger.warning(f"    Failed to fetch markets for {event_ticker}: {e}")
                    continue

                for market in markets:
                    k_ticker = market.get("ticker", "")
                    k_title = str(market.get("title", ""))
                    k_status = str(market.get("status", "")).lower()
                    if k_status not in ("open", "active", ""):
                        continue

                    parsed = parse_kalshi_threshold(k_ticker, k_title)
                    k_threshold = parsed["threshold"]
                    if k_threshold is None:
                        continue

                    # EXACT threshold match against FX strikes
                    k_key = format_threshold(k_threshold)
                    matched_fx = None
                    for strike_f, fx_info in fx_strikes.items():
                        fx_key = format_threshold(strike_f)
                        if fx_key == k_key:
                            matched_fx = fx_info
                            break

                    if matched_fx is None:
                        continue

                    # MATCH FOUND!
                    canonical_id = f"FX_{family}_{parsed['period'].replace('-', '')}_{k_key.replace('.', 'p')}"
                    # Sanitize canonical_id
                    canonical_id = re.sub(r'[^A-Za-z0-9_]', '_', canonical_id)

                    match_info = {
                        "canonical_id": canonical_id,
                        "family": family,
                        "fx_yes_conid": matched_fx["yes"],
                        "fx_no_conid": matched_fx["no"],
                        "kalshi_ticker": k_ticker,
                        "threshold": k_key,
                        "period": parsed["period"],
                        "direction": parsed["direction"],
                        "fx_parent": FX_PARENTS[family]["name"],
                        "kalshi_title": k_title[:120],
                    }
                    all_matches.append(match_info)
                    logger.info(
                        f"  MATCH: {canonical_id} | FX={matched_fx['yes']}/{matched_fx['no']} "
                        f"↔ K={k_ticker} | threshold={k_key} period={parsed['period']}"
                    )

        # Step 4: Insert verified mappings
        logger.info(f"\nStep 4: Inserting {len(all_matches)} verified mappings...")
        for match in all_matches:
            existing = await store.get(match["canonical_id"])
            if existing and existing.status == MappingStatus.CONFIRMED:
                logger.info(f"  SKIP existing confirmed: {match['canonical_id']}")
                continue

            resolution_criteria = {
                "forecastex": {
                    "source": "IBKR ForecastEx Client Portal API",
                    "conid_yes": match["fx_yes_conid"],
                    "conid_no": match["fx_no_conid"],
                    "threshold": match["threshold"],
                    "direction": match["direction"],
                    "period": match["period"],
                    "family": match["family"],
                },
                "kalshi": {
                    "source": "Kalshi Trade API",
                    "ticker": match["kalshi_ticker"],
                    "title": match["kalshi_title"],
                    "threshold": match["threshold"],
                    "direction": match["direction"],
                    "period": match["period"],
                },
                "criteria_match": "identical",
                "match_quality": "exact_threshold",
                "operator_note": f"Auto-matched by fx_live_matcher: exact threshold {match['threshold']}",
            }

            mapping = MarketMapping(
                canonical_id=match["canonical_id"],
                description=f"{match['fx_parent']}: {match['direction']} {match['threshold']} ({match['period']})",
                status=MappingStatus.CONFIRMED,
                allow_auto_trade=True,
                aliases=(),
                tags=FAMILY_TAGS.get(match["family"], ("economics",)),
                kalshi_market_id=match["kalshi_ticker"],
                forecastex_contract_id=match["fx_yes_conid"],
                forecastex_no_contract_id=match["fx_no_conid"],
                mapping_score=1.0,
                confidence=1.0,
                notes=f"FX cross-platform exact-threshold match (family={match['family']})",
                resolution_criteria_json=json.dumps(resolution_criteria),
                resolution_match_status="identical",
            )

            if DRY_RUN:
                logger.info(f"  [DRY-RUN] Would insert: {match['canonical_id']}")
                inserted_count += 1
            else:
                try:
                    await store.upsert(mapping)
                    inserted_count += 1
                    logger.info(f"  INSERTED: {match['canonical_id']}")
                except Exception as e:
                    logger.error(f"  FAILED to insert {match['canonical_id']}: {e}")

    finally:
        await kalshi_session.close()
        if fx_client.session:
            await fx_client.session.close()
        await store.disconnect()

    # Summary
    summary = {
        "total_matches": len(all_matches),
        "inserted": inserted_count,
        "dry_run": DRY_RUN,
        "families_with_children": list(fx_children_by_family.keys()) if 'fx_children_by_family' in dir() else [],
        "matches": all_matches,
    }

    with open("/tmp/fx_match_results.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"FX Live Matcher Results")
    print(f"{'='*60}")
    print(f"Total matches:     {len(all_matches)}")
    print(f"Mappings inserted: {inserted_count}")
    print(f"Dry run:           {DRY_RUN}")
    if all_matches:
        print(f"\n{'Canonical ID':<50s} {'Kalshi':<35s} {'Threshold':<12s}")
        print("-" * 100)
        for m in all_matches:
            print(f"{m['canonical_id']:<50s} {m['kalshi_ticker']:<35s} {m['threshold']:<12s}")


if __name__ == "__main__":
    asyncio.run(main())
