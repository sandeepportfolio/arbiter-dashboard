"""
Structured logging for ARBITER.
Logs to stdout (for Docker) and optionally to file.
"""
import logging
import sys
from datetime import datetime


def setup_logging(level: str = "INFO", log_file: str = None) -> logging.Logger:
    """Configure structured logging for the arbiter system."""
    root = logging.getLogger("arbiter")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler with color
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(name)-28s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    console.setFormatter(fmt)
    root.addHandler(console)

    # Optional file handler
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s │ %(levelname)-7s │ %(name)-28s │ %(message)s"
        ))
        root.addHandler(fh)

    return root


class TradeLogger:
    """Structured trade event logger for audit trail."""

    def __init__(self):
        self.logger = logging.getLogger("arbiter.trades")

    def log_opportunity(self, canonical_id: str, platform_a: str, platform_b: str,
                        yes_a: float, no_b: float, edge_cents: float, fees_cents: float):
        self.logger.info(
            f"ARB │ {canonical_id:20s} │ "
            f"YES@{platform_a}={yes_a:.2f} + NO@{platform_b}={no_b:.2f} │ "
            f"edge={edge_cents:.1f}¢ fees={fees_cents:.1f}¢ net={(edge_cents - fees_cents):.1f}¢"
        )

    def log_execution(self, canonical_id: str, platform: str, side: str,
                      price: float, quantity: int, order_id: str):
        self.logger.info(
            f"EXEC │ {canonical_id:20s} │ {platform} {side} "
            f"@ {price:.2f} × {quantity} │ order={order_id}"
        )

    def log_balance(self, platform: str, balance: float, threshold: float):
        if balance < threshold:
            self.logger.warning(
                f"BALANCE │ {platform:12s} │ ${balance:.2f} < ${threshold:.2f} threshold │ FUND NOW"
            )
        else:
            self.logger.info(
                f"BALANCE │ {platform:12s} │ ${balance:.2f} (threshold ${threshold:.2f})"
            )

    def log_error(self, component: str, error: str):
        self.logger.error(f"ERROR │ {component:20s} │ {error}")
