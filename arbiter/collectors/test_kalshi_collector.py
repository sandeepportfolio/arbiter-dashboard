import asyncio

from arbiter.collectors.kalshi import KalshiCollector
from arbiter.config.settings import KalshiConfig, MARKET_MAP
from arbiter.utils.price_store import PriceStore


def test_kalshi_build_price_point_reads_live_dollar_fields():
    async def runner():
        original = MARKET_MAP.get("TEST_KALSHI_DEM")
        MARKET_MAP["TEST_KALSHI_DEM"] = {
            "description": "Democratic party wins presidency",
            "status": "confirmed",
            "kalshi": "KXPRESPARTY-2028-D",
            "mapping_score": 0.9,
        }
        try:
            collector = KalshiCollector(KalshiConfig(), PriceStore(ttl=120))
            price = collector._build_price_point(
                "TEST_KALSHI_DEM",
                "KXPRESPARTY-2028",
                {
                    "ticker": "KXPRESPARTY-2028-D",
                    "title": "Will Democratic win the Presidency in 2028?",
                    "yes_bid_dollars": "0.6000",
                    "yes_ask_dollars": "0.6100",
                    "no_bid_dollars": "0.3900",
                    "no_ask_dollars": "0.4000",
                    "last_price_dollars": "0.6100",
                    "yes_ask_size_fp": "1245.05",
                    "no_ask_size_fp": "853.10",
                    "response_price_units": "usd_cent",
                },
            )

            assert price is not None
            assert price.yes_price == 0.61
            assert price.no_price == 0.4
            assert price.yes_bid == 0.6
            assert price.no_bid == 0.39
            assert price.yes_volume == 1245.05
            assert price.no_volume == 853.1
        finally:
            if original is None:
                MARKET_MAP.pop("TEST_KALSHI_DEM", None)
            else:
                MARKET_MAP["TEST_KALSHI_DEM"] = original

    asyncio.run(runner())


def test_kalshi_skips_ambiguous_submarkets_without_confident_match():
    collector = KalshiCollector(KalshiConfig(), PriceStore(ttl=120))
    market = collector._select_market_for_canonical(
        "DEM_HOUSE_2026",
        "KXPRESPARTY-2028",
        [
            {
                "ticker": "KXPRESPARTY-2028-D",
                "title": "Will Democratic win the Presidency in 2028?",
                "yes_sub_title": "Democratic party",
            },
            {
                "ticker": "KXPRESPARTY-2028-R",
                "title": "Will Republican win the Presidency in 2028?",
                "yes_sub_title": "Republican party",
            },
        ],
    )

    assert market is None
