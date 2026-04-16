"""Per-platform execution adapters for arbiter (EXEC-04).

This package contains:
- base.PlatformAdapter — the structural Protocol every adapter implements
- retry_policy.transient_retry — tenacity decorator factory for network-level retries
- kalshi.KalshiAdapter (added in Plan 04)
- polymarket.PolymarketAdapter (added in Plan 05)
"""
from .base import PlatformAdapter
from .retry_policy import TRANSIENT_EXCEPTIONS, transient_retry

__all__ = ["PlatformAdapter", "TRANSIENT_EXCEPTIONS", "transient_retry"]
