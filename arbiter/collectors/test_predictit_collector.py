import asyncio
import time

from arbiter.collectors.predictit import PredictItCollector
from arbiter.config.settings import MARKET_MAP, PredictItConfig
from arbiter.utils.price_store import PriceStore


def test_predictit_cached_market_data_keeps_original_source_timestamp():
    async def runner():
        original = MARKET_MAP.get("TEST_PI_STALE")
        MARKET_MAP["TEST_PI_STALE"] = {
            "description": "PredictIt stale source timestamp",
            "status": "confirmed",
            "predictit": "9999",
            "predictit_contract_keywords": ("test",),
            "mapping_score": 0.8,
        }
        try:
            store = PriceStore(ttl=120)
            collector = PredictItCollector(PredictItConfig(), store)
            source_timestamp = time.time() - 45.0
            collector._last_full_fetch = source_timestamp

            await collector.extract_prices(
                {
                    "9999": {
                        "id": 9999,
                        "name": "Stale test market",
                        "contracts": [
                            {
                                "id": 1,
                                "name": "Test contract",
                                "shortName": "Test",
                                "lastTradePrice": 0.41,
                                "bestBuyYesCost": 0.42,
                                "bestBuyNoCost": 0.57,
                                "totalSharesTraded": 1000,
                            }
                        ],
                    }
                }
            )

            stored = await store.get("predictit", "TEST_PI_STALE")
            assert stored is not None
            assert stored.timestamp == source_timestamp
            assert stored.metadata["source_timestamp"] == source_timestamp
            assert stored.metadata["stale_source_seconds"] >= 45.0
        finally:
            if original is None:
                MARKET_MAP.pop("TEST_PI_STALE", None)
            else:
                MARKET_MAP["TEST_PI_STALE"] = original

    asyncio.run(runner())
