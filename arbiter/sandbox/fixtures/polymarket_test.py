"""Test-wallet PolymarketAdapter fixture (hard-asserts PHASE4_MAX_ORDER_USD is set).

Constructs a real PolymarketAdapter using the production constructor signature
(config, clob_client_factory, rate_limiter, circuit) observed in
arbiter/main.py:225-231. The plan's placeholder `PolymarketAdapter(cfg)` does not
match the real signature; this fixture mirrors production wiring.

For sandbox fixture-build we construct a local ClobClient factory: we do NOT
reuse the engine's cached client because no engine is built here. Scenarios that
need engine-cached-client semantics build their own engine on top of these fixtures.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pytest

from arbiter.config.settings import load_config
from arbiter.execution.adapters.polymarket import PolymarketAdapter
from arbiter.utils.retry import CircuitBreaker, RateLimiter


def _lazy_clob_client_factory():
    """Return a factory that lazily instantiates a ClobClient the first time it's invoked.

    Returns None if py-clob-client is unavailable or required config missing — matches
    the engine's `_get_poly_clob_client` None-handling semantics.
    """
    cache: dict[str, Any] = {"client": None, "built": False}

    def factory() -> Optional[Any]:
        if cache["built"]:
            return cache["client"]
        cache["built"] = True
        try:
            from py_clob_client.client import ClobClient  # type: ignore
            from py_clob_client.constants import POLYGON  # type: ignore
        except Exception:
            cache["client"] = None
            return None

        cfg = load_config()
        pk = getattr(cfg.polymarket, "private_key", None)
        host = getattr(cfg.polymarket, "clob_url", None) or "https://clob.polymarket.com"
        if not pk:
            cache["client"] = None
            return None
        try:
            client = ClobClient(host, key=pk, chain_id=POLYGON)
            # Best-effort: derive API creds for authenticated flows.
            try:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
            except Exception:
                pass
            cache["client"] = client
            return client
        except Exception:
            cache["client"] = None
            return None

    return factory


@pytest.fixture
async def poly_test_adapter(sandbox_db_pool):
    """PolymarketAdapter wired to the throwaway test wallet; refuses to build without the $5 hard-lock env var."""
    assert os.getenv("PHASE4_MAX_ORDER_USD"), (
        "SAFETY: PHASE4_MAX_ORDER_USD must be set before building Polymarket adapter for Phase 4. "
        "Source .env.sandbox (which pins PHASE4_MAX_ORDER_USD=5) before running `pytest -m live`."
    )

    cfg = load_config()
    rate_limiter = RateLimiter(name="poly-exec", max_requests=5, window_seconds=1.0)
    circuit = CircuitBreaker(name="poly-exec", failure_threshold=5, recovery_timeout=30.0)

    adapter = PolymarketAdapter(
        config=cfg,
        clob_client_factory=_lazy_clob_client_factory(),
        rate_limiter=rate_limiter,
        circuit=circuit,
    )
    try:
        yield adapter
    finally:
        # Adapter does not own the ClobClient; nothing to close here.
        pass
