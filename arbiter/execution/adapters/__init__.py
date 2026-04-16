"""Per-platform execution adapters for arbiter (EXEC-04).

This package contains:
- ``base.PlatformAdapter`` — the structural Protocol every adapter implements
- ``retry_policy.transient_retry`` — tenacity decorator factory for network-level retries
- ``kalshi.KalshiAdapter`` — Kalshi platform adapter (Plan 04)
- ``polymarket.PolymarketAdapter`` — Polymarket platform adapter (Plan 05)
"""
from .base import PlatformAdapter
from .kalshi import KalshiAdapter
from .polymarket import PolymarketAdapter
from .retry_policy import TRANSIENT_EXCEPTIONS, transient_retry

__all__ = [
    "KalshiAdapter",
    "PlatformAdapter",
    "PolymarketAdapter",
    "TRANSIENT_EXCEPTIONS",
    "transient_retry",
]
