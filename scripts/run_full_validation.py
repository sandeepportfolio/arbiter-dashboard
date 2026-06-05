#!/usr/bin/env python3
"""
Phase 5: Run initial full validation + discovery cycle.

Runs inside Docker via:
    docker exec arbiter-api-prod python3 /app/scripts/run_full_validation.py

Validates all 361 confirmed mappings, checks candidates/review for promotion,
reports on live vs dead, and auto-expires/promotes as appropriate.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/app')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
)
logger = logging.getLogger('full_validation')


async def main():
    import asyncpg
    from arbiter.mapping.market_map import MarketMappingStore
    from arbiter.mapping.auto_validator import (
        MappingAutoValidator,
        ValidationRecommendation,
    )
    from arbiter.config.settings import KalshiConfig
    from arbiter.collectors.kalshi import KalshiCollector
    from arbiter.utils.price_store import PriceStore

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        print('ERROR: DATABASE_URL not set')
        sys.exit(1)

    # Set up mapping store
    store = MarketMappingStore(database_url)
    await store.connect()

    # Set up Kalshi collector (for live API checks)
    kalshi_config = KalshiConfig(
        base_url=os.environ.get('KALSHI_API_URL', 'https://api.elections.kalshi.com/trade-api/v2'),
        api_key_id=os.environ.get('KALSHI_API_KEY_ID', ''),
        private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH', ''),
        poll_interval=10.0,
    )
    price_store = PriceStore()
    kalshi = KalshiCollector(kalshi_config, price_store)

    # Set up ForecastEx client if available
    fx_client = None
    try:
        from arbiter.collectors.forecastex import ForecastExClient
        gateway_url = os.environ.get('IBKR_GATEWAY_URL', '')
        account_id = os.environ.get('IBKR_ACCOUNT_ID', '')
        if gateway_url and account_id:
            fx_client = ForecastExClient(
                gateway_url=gateway_url,
                account_id=account_id,
            )
            logger.info('ForecastEx client initialized')
    except Exception as e:
        logger.warning('ForecastEx client not available: %s', e)

    # Build validator (no polymarket collector for standalone mode - gamma-api is public)
    validator = MappingAutoValidator(
        mapping_store=store,
        kalshi_collector=kalshi,
        polymarket_collector=None,  # validator uses gamma-api directly
        forecastex_client=fx_client,
    )

    print('=' * 70)
    print('FULL VALIDATION + DISCOVERY CYCLE')
    print('=' * 70)

    # 1. Validate confirmed
    print('\n--- Step 1: Validate confirmed mappings ---')
    confirmed_results = await validator.validate_all(
        status='confirmed',
        batch_size=15,
        inter_batch_delay=1.5,
    )

    # Classify results
    live = [r for r in confirmed_results if r.recommendation == ValidationRecommendation.CONFIRM]
    expired = [r for r in confirmed_results if r.recommendation == ValidationRecommendation.EXPIRE]
    review = [r for r in confirmed_results if r.recommendation == ValidationRecommendation.REVIEW]

    print(f'\nConfirmed validation results:')
    print(f'  Total checked: {len(confirmed_results)}')
    print(f'  Live (all platforms OK): {len(live)}')
    print(f'  Should expire: {len(expired)}')
    print(f'  Needs review: {len(review)}')

    if expired:
        print(f'\n  Expired mappings:')
        for r in expired[:20]:
            print(f'    {r.canonical_id}: {r.reason}')

    if review:
        print(f'\n  Review-needed mappings:')
        for r in review[:10]:
            print(f'    {r.canonical_id}: {r.reason}')

    # 2. Auto-expire dead confirmed
    print('\n--- Step 2: Auto-expire dead confirmed ---')
    expired_ids = await validator.auto_expire_dead(results=confirmed_results)
    print(f'  Auto-expired: {len(expired_ids)} mappings')

    # 3. Validate candidates for promotion
    print('\n--- Step 3: Validate candidates ---')
    candidate_results = await validator.validate_all(
        status='candidate',
        batch_size=15,
        inter_batch_delay=1.5,
    )
    cand_live = [r for r in candidate_results if r.recommendation == ValidationRecommendation.CONFIRM]
    print(f'  Candidates checked: {len(candidate_results)}')
    print(f'  Candidates with live platforms: {len(cand_live)}')

    # 4. Validate review for promotion
    print('\n--- Step 4: Validate review mappings ---')
    review_results = await validator.validate_all(
        status='review',
        batch_size=15,
        inter_batch_delay=1.5,
    )
    rev_live = [r for r in review_results if r.recommendation == ValidationRecommendation.CONFIRM]
    print(f'  Review checked: {len(review_results)}')
    print(f'  Review with live platforms: {len(rev_live)}')

    # 5. Auto-promote qualified
    print('\n--- Step 5: Auto-promote validated ---')
    promoted_ids = await validator.auto_promote_validated(
        results=candidate_results + review_results,
    )
    print(f'  Auto-promoted: {len(promoted_ids)} mappings')
    if promoted_ids:
        for pid in promoted_ids[:10]:
            print(f'    PROMOTED: {pid}')

    # 6. Stamp last_validated_at
    print('\n--- Step 6: Stamp validation timestamps ---')
    all_results = confirmed_results + candidate_results + review_results
    stamped = await validator.update_last_validated(all_results)
    print(f'  Stamped: {stamped} mappings')

    # 7. Final status counts
    print('\n--- Final status counts ---')
    counts = await store.count_by_status()
    for status, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        print(f'  {status}: {cnt}')

    # Summary
    print('\n' + '=' * 70)
    print('SUMMARY')
    print('=' * 70)
    print(f'  Confirmed checked: {len(confirmed_results)}')
    print(f'  Confirmed live: {len(live)}')
    print(f'  Auto-expired: {len(expired_ids)}')
    print(f'  Candidates checked: {len(candidate_results)}')
    print(f'  Review checked: {len(review_results)}')
    print(f'  Auto-promoted: {len(promoted_ids)}')
    print(f'  Validation timestamps set: {stamped}')
    print(f'\nDone at {datetime.now(timezone.utc).isoformat()}')

    await store.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
