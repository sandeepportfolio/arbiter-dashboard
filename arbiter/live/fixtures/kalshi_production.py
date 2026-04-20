"""Production KalshiAdapter fixture (hard-asserts URL and key path are NOT demo).

Inverse of arbiter/sandbox/fixtures/kalshi_demo.py: refuses to construct the
adapter unless ``KALSHI_BASE_URL`` does NOT contain ``demo`` AND the private
key on disk is at a non-demo path that actually exists. Mirrors the real
production wiring observed in arbiter/main.py.
"""
from __future__ import annotations

import os
import pathlib

import aiohttp
import pytest

from arbiter.collectors.kalshi import KalshiAuth
from arbiter.config.settings import load_config
from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.utils.retry import CircuitBreaker, RateLimiter


@pytest.fixture
async def production_kalshi_adapter(production_db_pool):
    """KalshiAdapter wired to production Kalshi. Refuses to build against demo.

    Preconditions (all hard-asserted):
    - ``KALSHI_BASE_URL`` must be set and NOT contain 'demo'.
    - ``KALSHI_PRIVATE_KEY_PATH`` must be set, must NOT contain 'demo',
      and the file must exist on disk.
    """
    base_url = os.getenv("KALSHI_BASE_URL", "")
    assert base_url, (
        "SAFETY: KALSHI_BASE_URL must be set for Phase 5. "
        "Source .env.production before running live-fire."
    )
    assert "demo" not in base_url.lower(), (
        f"SAFETY: KALSHI_BASE_URL must NOT point at demo; got {base_url!r}. "
        "Phase 5 runs against production Kalshi."
    )

    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    assert key_path, (
        "SAFETY: KALSHI_PRIVATE_KEY_PATH must be set for Phase 5."
    )
    assert "demo" not in key_path.lower(), (
        f"SAFETY: Kalshi key path must NOT be demo; got {key_path!r}. "
        "Phase 5 requires a production Kalshi RSA key."
    )
    assert pathlib.Path(key_path).exists(), (
        f"SAFETY: Kalshi prod key missing on disk: {key_path}. "
        "Generate the key from kalshi.com > Profile > API keys and save it here."
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
        if not session.closed:
            await session.close()
