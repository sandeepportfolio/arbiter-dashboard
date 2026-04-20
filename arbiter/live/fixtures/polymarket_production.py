"""Production PolymarketAdapter fixture (hard-asserts PHASE5_MAX_ORDER_USD <= 10).

Inverse of arbiter/sandbox/fixtures/polymarket_test.py: refuses to construct
the adapter unless ``PHASE5_MAX_ORDER_USD`` is set to a value <= $10. This is
the fixture-level guard that stops Plan 05-02's live-fire test from running
against a production wallet without the $10 notional cap in place.

Polymarket has no sandbox — Phase 4 already uses production with a throwaway
wallet + $5 cap. Phase 5 moves to the operator's real wallet with a $10 cap.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pytest

from arbiter.config.settings import load_config
from arbiter.execution.adapters.polymarket import PolymarketAdapter
from arbiter.utils.retry import CircuitBreaker, RateLimiter


def _lazy_clob_client_factory():
    """Return a factory that lazily instantiates a ClobClient on first invocation.

    Matches ``arbiter.sandbox.fixtures.polymarket_test._lazy_clob_client_factory``
    so the production fixture wires identically to the sandbox fixture.
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
async def production_polymarket_adapter(production_db_pool):
    """PolymarketAdapter for Phase 5 — refuses to build without PHASE5_MAX_ORDER_USD <= 10."""
    raw = os.getenv("PHASE5_MAX_ORDER_USD")
    assert raw, (
        "SAFETY: PHASE5_MAX_ORDER_USD must be set before building Polymarket "
        "adapter for Phase 5. Source .env.production (which pins "
        "PHASE5_MAX_ORDER_USD=10) before running live-fire."
    )
    try:
        cap = float(raw)
    except (TypeError, ValueError) as exc:
        raise AssertionError(
            f"SAFETY: PHASE5_MAX_ORDER_USD must parse as a number; got {raw!r}: {exc}"
        )
    assert cap <= 10.0, (
        f"SAFETY: PHASE5_MAX_ORDER_USD must be <= $10 for the first live trade; "
        f"got ${cap:.2f}. Phase 5 starts with a dollar cap and relaxes in v2."
    )

    assert os.getenv("POLY_PRIVATE_KEY"), (
        "SAFETY: POLY_PRIVATE_KEY required for Phase 5 live-fire."
    )
    assert os.getenv("POLY_FUNDER"), (
        "SAFETY: POLY_FUNDER required for Phase 5 live-fire."
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
        pass
