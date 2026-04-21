"""arbiter.notifiers — outbound alert routing.

Re-exports the authoritative TelegramNotifier from arbiter.monitor.balance
so downstream code can import from a semantically meaningful namespace
(arbiter.notifiers.telegram) rather than the monitor module where it
historically lived.
"""
from ..monitor.balance import TelegramNotifier

__all__ = ["TelegramNotifier"]
