import asyncio
import os
import sys
import time
import aiohttp
import asyncpg
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger('audit')

sys.path.insert(0, '/app')

async def check_kalshi_batch(session, base_url, auth, rate_limiter, tickers):
    results = {}
    for i in range(0, len(tickers), 50):
        batch = tickers[i:i+50]
        await rate_limiter.acquire()
        headers = {'Accept': 'application/json'}
        if auth.is_authenticated:
            headers.update(auth.get_headers('GET', '/trade-api/v2/markets'))
        try:
            async with session.get(
                f'{base_url}/markets',
                params={'tickers': ','.join(batch), 'limit': '100'},
                headers=headers,
            ) as resp:
                if resp.status == 429:
                    ra = float(resp.headers.get('Retry-After', '5'))
                    await asyncio.sleep(ra)
                    continue
                if resp.status >= 400:
                    logger.warning('Kalshi HTTP %s on batch %d', resp.status, i//50)
                    continue
                data = await resp.json()
            for m in data.get('markets', []):
                t = m.get('ticker', '')
                status = str(m.get('status', '')).lower()
                is_open = status in ('open', 'active')
                is_expired = status in ('closed', 'settled', 'finalized', 'ceased_trading')
                yes_ask = float(m.get('yes_ask', 0) or 0)
                results[t] = {
                    'exists': True, 'is_open': is_open, 'is_expired': is_expired,
                    'has_quotes': yes_ask > 0, 'status': status,
                }
        except Exception as e:
            logger.warning('Kalshi batch error: %s', e)
        await asyncio.sleep(0.15)
    return results


async def check_poly_batch(session, slugs):
    results = {}
    for slug in slugs:
        try:
            async with session.get(
                'https://gamma-api.polymarket.com/markets',
                params={'slug': slug, 'limit': '1'},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    results[slug] = {'exists': False, 'error': f'HTTP {resp.status}'}
                    continue
                data = await resp.json()
            if not data:
                results[slug] = {'exists': False}
                continue
            m = data[0] if isinstance(data, list) else data
            results[slug] = {
                'exists': True,
                'is_open': bool(m.get('active', False)) and not bool(m.get('closed', True)),
                'is_expired': bool(m.get('closed', False)),
                'has_quotes': bool(m.get('outcomePrices')),
            }
        except Exception as e:
            results[slug] = {'exists': False, 'error': str(e)[:100]}
        await asyncio.sleep(0.25)
    return results


async def main():
    database_url = os.environ['DATABASE_URL']
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=5)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT canonical_id, kalshi_market_id, polymarket_slug, "
            "forecastex_contract_id, mapping_score, allow_auto_trade "
            "FROM market_mappings WHERE status = 'confirmed' ORDER BY canonical_id"
        )

    total = len(rows)
    print(f'Auditing {total} confirmed mappings...')

    from arbiter.config.settings import KalshiConfig
    from arbiter.collectors.kalshi import KalshiAuth
    from arbiter.utils.retry import RateLimiter

    kalshi_config = KalshiConfig(
        base_url=os.environ.get('KALSHI_API_URL', 'https://api.elections.kalshi.com/trade-api/v2'),
        api_key_id=os.environ.get('KALSHI_API_KEY_ID', ''),
        private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH', ''),
        poll_interval=10.0,
    )
    auth = KalshiAuth(kalshi_config.api_key_id, kalshi_config.private_key_path)
    rl = RateLimiter('audit_k', max_requests=6, window_seconds=1.0)

    kalshi_tickers = [r['kalshi_market_id'] for r in rows if r['kalshi_market_id']]
    poly_slugs = [r['polymarket_slug'] for r in rows if r['polymarket_slug']]

    print(f'Checking {len(kalshi_tickers)} Kalshi tickers...')
    async with aiohttp.ClientSession() as session:
        k_results = await check_kalshi_batch(session, kalshi_config.base_url, auth, rl, kalshi_tickers)
        print(f'Kalshi: got {len(k_results)} results')

        print(f'Checking {len(poly_slugs)} Polymarket slugs...')
        p_results = await check_poly_batch(session, poly_slugs)
        print(f'Polymarket: got {len(p_results)} results')

    stats = {
        'k_live': 0, 'k_dead': 0, 'k_expired': 0, 'k_notfound': 0, 'k_noticket': 0,
        'p_live': 0, 'p_dead': 0, 'p_expired': 0, 'p_notfound': 0, 'p_noslug': 0,
        'fully_live': 0, 'partial_live': 0, 'fully_dead': 0,
    }
    to_expire = []

    for row in rows:
        cid = row['canonical_id']
        kt = row['kalshi_market_id'] or ''
        ps = row['polymarket_slug'] or ''

        plat_live = 0
        plat_dead = 0
        plat_total = 0

        if kt:
            plat_total += 1
            kr = k_results.get(kt, {})
            if kr.get('is_open'):
                stats['k_live'] += 1
                plat_live += 1
            elif kr.get('is_expired'):
                stats['k_expired'] += 1
                plat_dead += 1
            elif kr.get('exists'):
                stats['k_dead'] += 1
                plat_dead += 1
            else:
                stats['k_notfound'] += 1
                plat_dead += 1
        else:
            stats['k_noticket'] += 1

        if ps:
            plat_total += 1
            pr = p_results.get(ps, {})
            if pr.get('is_open'):
                stats['p_live'] += 1
                plat_live += 1
            elif pr.get('is_expired'):
                stats['p_expired'] += 1
                plat_dead += 1
            elif pr.get('exists'):
                stats['p_dead'] += 1
                plat_dead += 1
            else:
                stats['p_notfound'] += 1
                plat_dead += 1
        else:
            stats['p_noslug'] += 1

        if plat_live > 0 and plat_live == plat_total:
            stats['fully_live'] += 1
        elif plat_live > 0:
            stats['partial_live'] += 1
        elif plat_total > 0:
            stats['fully_dead'] += 1
            to_expire.append(cid)

    sep = '=' * 60
    print(f'\n{sep}')
    print('MAPPING AUDIT RESULTS')
    print(sep)
    print(f'Total confirmed: {total}')
    print(f'\nKalshi ({len(kalshi_tickers)} checked):')
    print(f'  Live/Open: {stats["k_live"]}')
    print(f'  Expired/Closed: {stats["k_expired"]}')
    print(f'  Dead/Not found: {stats["k_dead"]}')
    print(f'  Not in API results: {stats["k_notfound"]}')
    print(f'  No ticker: {stats["k_noticket"]}')
    print(f'\nPolymarket ({len(poly_slugs)} checked):')
    print(f'  Live/Open: {stats["p_live"]}')
    print(f'  Expired/Closed: {stats["p_expired"]}')
    print(f'  Dead/Not found: {stats["p_dead"]}')
    print(f'  Not in API results: {stats["p_notfound"]}')
    print(f'  No slug: {stats["p_noslug"]}')
    print(f'\nOverall:')
    print(f'  Fully live: {stats["fully_live"]}')
    print(f'  Partially live: {stats["partial_live"]}')
    print(f'  Fully dead: {stats["fully_dead"]}')
    print(f'  Should expire: {len(to_expire)}')

    if to_expire:
        print(f'\nDead mappings to expire ({len(to_expire)}):')
        for cid in to_expire[:30]:
            print(f'  {cid}')
        if len(to_expire) > 30:
            print(f'  ... and {len(to_expire)-30} more')

    await pool.close()
    print(f'\nAudit complete.')

asyncio.run(main())
