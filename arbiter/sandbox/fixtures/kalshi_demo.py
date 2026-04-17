"""Demo Kalshi adapter fixture (hard-asserts KALSHI_BASE_URL is the demo host).

Constructs a real KalshiAdapter using the production constructor signature
(config, session, auth, rate_limiter, circuit) observed in arbiter/main.py:218-224.
The plan's placeholder `KalshiAdapter(cfg)` does not match the real signature;
this fixture mirrors production wiring so scenarios exercise the same code path.
"""
from __future__ import annotations

import os

import aiohttp
import pytest

from arbiter.collectors.kalshi import KalshiAuth
from arbiter.config.settings import load_config
from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.utils.retry import CircuitBreaker, RateLimiter


@pytest.fixture
async def demo_kalshi_adapter(sandbox_db_pool):
    """KalshiAdapter wired to demo-api.kalshi.co. Built from load_config() which picks up KALSHI_BASE_URL env."""
    base_url = os.getenv("KALSHI_BASE_URL", "")
    assert "demo-api.kalshi.co" in base_url, (
        f"SAFETY: KALSHI_BASE_URL must contain 'demo-api.kalshi.co' for Phase 4; "
        f"got {base_url!r}. Source .env.sandbox before running `pytest -m live`."
    )

    cfg = load_config()
    session = aiohttp.ClientSession()
    auth = KalshiAuth(cfg.kalshi.api_key_id, cfg.kalshi.private_key_path)
    rate_limiter = RateLimiter(name="kalshi-exec", max_requests=10, window_seconds=1.0)
    circuit = CircuitBreaker(name="kalshi-exec", failure_threshold=5, recovery_timeout=30.0)

    adapter = KalshiAdapter(
        config=cfg,
        session=session,
        auth=auth,
        rate_limiter=rate_limiter,
        circuit=circuit,
    )
    try:
        yield adapter
    finally:
        # Release the shared aiohttp session. Adapter has no explicit close.
        if not session.closed:
            await session.close()
