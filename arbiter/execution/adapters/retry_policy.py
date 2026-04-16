"""Tenacity-backed retry decorator for adapter HTTP calls (OPS-03).

Coexists with arbiter/utils/retry.py CircuitBreaker per CONTEXT D-18:
  - tenacity handles per-call transient retry (timeouts, connection drops)
  - CircuitBreaker handles sustained-outage gating (5+ failures → open)
The two are layered, not redundant.

WARNING — DO NOT use this decorator on Polymarket order POSTs without a
reconcile-before-retry guard (see Pitfall 2 / RESEARCH lines 582-587).
Polymarket has no idempotency key; blind retries can create duplicate orders.
Kalshi is safe because `client_order_id` is the idempotency key.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger("arbiter.adapters.retry")

TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectionError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
)


def transient_retry(*, max_attempts: int = 3):
    """Tenacity decorator factory for transient network failures.

    Defaults: 3 attempts, exponential jitter starting at 0.5s up to 10s,
    reraises original exception after final attempt.

    SAFE for Kalshi (`client_order_id` is the idempotency key).
    UNSAFE for Polymarket order POSTs — use reconcile-before-retry there.
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=10),
        retry=retry_if_exception_type(TRANSIENT_EXCEPTIONS),
        before_sleep=before_sleep_log(log, logging.WARNING),
    )
