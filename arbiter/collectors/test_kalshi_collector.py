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


def test_kalshi_yes_ask_does_not_fall_back_to_last_price():
    """yes_ask must come ONLY from real ask fields, never from last_price/last_price_dollars."""
    async def runner():
        original = MARKET_MAP.get("TEST_KALSHI_LAST")
        MARKET_MAP["TEST_KALSHI_LAST"] = {
            "description": "Test",
            "status": "confirmed",
            "kalshi": "TEST",
            "mapping_score": 0.9,
        }
        try:
            collector = KalshiCollector(KalshiConfig(), PriceStore(ttl=120))
            # Only last_price is populated — no real ask side.
            price = collector._build_price_point(
                "TEST_KALSHI_LAST",
                "TEST",
                {
                    "ticker": "TEST-MKT",
                    "title": "Stale market",
                    "yes_bid_dollars": "0.4000",
                    "no_bid_dollars": "0.5500",
                    "last_price_dollars": "0.7500",
                    "response_price_units": "usd_cent",
                },
            )

            # yes_ask must NOT pick up last_price (0.75). It should be 0
            # because no real yes_ask field is present.
            assert price is not None
            assert price.yes_ask == 0.0, f"yes_ask leaked from last_price: {price.yes_ask}"
            # no_ask must be 0 too (no_ask field absent), not synthesized.
            assert price.no_ask == 0.0, f"no_ask leaked: {price.no_ask}"
        finally:
            if original is None:
                MARKET_MAP.pop("TEST_KALSHI_LAST", None)
            else:
                MARKET_MAP["TEST_KALSHI_LAST"] = original

    asyncio.run(runner())


def test_kalshi_no_price_does_not_synthesize_from_yes_price():
    """When no real no orderbook side, no_price must be 0.0 — not 1.0 - yes_price."""
    async def runner():
        original = MARKET_MAP.get("TEST_KALSHI_SYNTH")
        MARKET_MAP["TEST_KALSHI_SYNTH"] = {
            "description": "Test",
            "status": "confirmed",
            "kalshi": "TEST",
            "mapping_score": 0.9,
        }
        try:
            collector = KalshiCollector(KalshiConfig(), PriceStore(ttl=120))
            price = collector._build_price_point(
                "TEST_KALSHI_SYNTH",
                "TEST",
                {
                    "ticker": "TEST-MKT",
                    "title": "One-sided market",
                    "yes_ask_dollars": "0.6000",
                    "yes_bid_dollars": "0.5500",
                    # no side absent — must NOT synthesize 1 - yes_price = 0.40
                    "response_price_units": "usd_cent",
                },
            )

            assert price is not None
            assert price.yes_price == 0.60
            assert price.no_price == 0.0, f"no_price was synthesized: {price.no_price}"
            assert price.no_ask == 0.0
            assert price.no_bid == 0.0
        finally:
            if original is None:
                MARKET_MAP.pop("TEST_KALSHI_SYNTH", None)
            else:
                MARKET_MAP["TEST_KALSHI_SYNTH"] = original

    asyncio.run(runner())


def test_kalshi_yes_price_does_not_use_last_price():
    """yes_price must be ask-or-bid only — last_price must NEVER reach it."""
    async def runner():
        original = MARKET_MAP.get("TEST_KALSHI_YPRICE")
        MARKET_MAP["TEST_KALSHI_YPRICE"] = {
            "description": "Test",
            "status": "confirmed",
            "kalshi": "TEST",
            "mapping_score": 0.9,
        }
        try:
            collector = KalshiCollector(KalshiConfig(), PriceStore(ttl=120))
            # last_price is 0.99 (stale, post-resolution print). No live quote.
            price = collector._build_price_point(
                "TEST_KALSHI_YPRICE",
                "TEST",
                {
                    "ticker": "TEST-MKT",
                    "title": "Stale-only market",
                    "last_price_dollars": "0.9900",
                    "response_price_units": "usd_cent",
                },
            )

            # With no real orderbook, _build_price_point returns None.
            assert price is None
        finally:
            if original is None:
                MARKET_MAP.pop("TEST_KALSHI_YPRICE", None)
            else:
                MARKET_MAP["TEST_KALSHI_YPRICE"] = original

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


def test_list_all_events_pages_through_event_catalog():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def json(self):
            return self.payload

    class FakeSession:
        def get(self, url, params=None, headers=None):
            cursor = (params or {}).get("cursor")
            if cursor == "page-2":
                return FakeResponse({
                    "events": [
                        {"event_ticker": "CONTROLS-2026", "title": "Which party will win the Senate?"},
                    ],
                    "cursor": None,
                })
            return FakeResponse({
                "events": [
                    {"event_ticker": "CONTROLH-2026", "title": "Which party will win the House?"},
                ],
                "cursor": "page-2",
            })

    async def runner():
        collector = KalshiCollector(KalshiConfig(), PriceStore(ttl=120))
        collector.rate_limiter.acquire = lambda: asyncio.sleep(0)
        collector._get_session = lambda: asyncio.sleep(0, result=FakeSession())
        events = await collector.list_all_events(page_size=100, max_pages=5)

        assert [event["event_ticker"] for event in events] == [
            "CONTROLH-2026",
            "CONTROLS-2026",
        ]

    asyncio.run(runner())
