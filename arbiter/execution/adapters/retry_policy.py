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
    stop_after_delay,
    wait_exponential_jitter,
)

log = logging.getLogger("arbiter.adapters.retry")


class KalshiTransientInfraError(Exception):
    """Kalshi-SIDE infra failure that is safe to retry (P1 #5).

    Raised by ``KalshiAdapter`` when an HTTP response is an *infrastructure*
    error rather than an application/auth rejection — e.g. an Envoy "service
    unavailable" body returned as 401 ``authentication_error`` (the 8 observed
    burns), or any 5xx. These are NOT signing failures; re-signing the SAME
    pre-built body + ``client_order_id`` and re-POSTing is capital-safe because
    Kalshi dedups on ``client_order_id`` (no double-place).

    A genuine signing/auth failure (e.g. ``INCORRECT_API_KEY_SIGNATURE``,
    ``forbidden``) is deliberately NOT wrapped in this type, so tenacity treats
    it as terminal and the leg fails immediately instead of burning the retry
    budget (and widening the naked window) on something a retry can't fix.
    """

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Kalshi infra {status}: {body[:200]}")


# Network-level transients that are always safe to retry regardless of venue.
_NETWORK_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectionError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
)

# Public tuple consumed by adapters in their ``except`` clauses. Includes the
# Kalshi infra sentinel so the order-placement path can map "retries exhausted
# on a persistent infra error" to a FAILED order + circuit failure.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *_NETWORK_TRANSIENT_EXCEPTIONS,
    KalshiTransientInfraError,
)


def transient_retry(*, max_attempts: int = 3):
    """Tenacity decorator factory for transient NETWORK failures.

    Defaults: 3 attempts, exponential jitter starting at 0.5s up to 10s,
    reraises original exception after final attempt. Retries only network-level
    transients (connection drops/timeouts), NOT HTTP status codes — most call
    sites inspect the returned status themselves. For paths that must also
    retry a Kalshi-side infra response (401-infra / 5xx), use
    ``infra_transient_retry``.

    SAFE for Kalshi (`client_order_id` is the idempotency key).
    UNSAFE for Polymarket order POSTs — use reconcile-before-retry there.
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=10),
        retry=retry_if_exception_type(_NETWORK_TRANSIENT_EXCEPTIONS),
        before_sleep=before_sleep_log(log, logging.WARNING),
    )


def infra_transient_retry(*, max_attempts: int = 3, max_seconds: float = 3.5):
    """Retry decorator for Kalshi paths that must survive a transient infra blip.

    Retries the same NETWORK transients as ``transient_retry`` PLUS
    ``KalshiTransientInfraError`` (infra-401 / 5xx). Bounded by BOTH an attempt
    count (2-3) AND a WALL-CLOCK total-time cap, so a *persistent* infra-401
    still fails fast (and trips the circuit breaker) instead of spinning — the
    wall-clock stop is what keeps a stuck Envoy from widening the naked window
    on the secondary leg.

    Capital-safe on Kalshi POSTs because the caller re-sends the SAME pre-built
    body + ``client_order_id`` (Kalshi dedups → no double-place).
    """
    return retry(
        reraise=True,
        # Fire when EITHER bound is hit: attempt count OR wall-clock seconds.
        stop=(stop_after_attempt(max_attempts) | stop_after_delay(max_seconds)),
        wait=wait_exponential_jitter(initial=0.25, max=1.0),
        retry=retry_if_exception_type(
            (*_NETWORK_TRANSIENT_EXCEPTIONS, KalshiTransientInfraError),
        ),
        before_sleep=before_sleep_log(log, logging.WARNING),
    )
