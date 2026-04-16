"""
Structured JSON logging for ARBITER via structlog + stdlib bridge.

Existing `logging.getLogger("arbiter.X").info(...)` calls across the codebase
continue to work — the stdlib root handler is now backed by
structlog's ProcessorFormatter and emits JSON to stdout (and optionally a file).

Bound contextvars (set via structlog.contextvars.bind_contextvars) propagate
across asyncio await boundaries because Python contextvars are per-Task.
"""
import logging
import re
import sys
from typing import Any, MutableMapping

import structlog
from structlog.contextvars import merge_contextvars
from structlog.processors import JSONRenderer, TimeStamper, add_log_level
from structlog.stdlib import ExtraAdder, LoggerFactory, ProcessorFormatter, add_logger_name


_SECRET_KEY_PATTERN = re.compile(r"(_KEY|_SECRET|_DSN|^Authorization)$", re.IGNORECASE)


def _strip_secrets(_logger, _name, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor that removes any key matching *_KEY/*_SECRET/*_DSN/Authorization."""
    for key in list(event_dict.keys()):
        if _SECRET_KEY_PATTERN.search(key):
            event_dict[key] = "***REDACTED***"
    return event_dict


SHARED_PROCESSORS = [
    merge_contextvars,
    # ExtraAdder must precede _strip_secrets so `extra={}` keys from stdlib callers
    # (e.g. logger.info("e", extra={"POLY_PRIVATE_KEY": "..."})) get redacted too.
    ExtraAdder(),
    add_log_level,
    add_logger_name,
    TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    _strip_secrets,
]


def prepare_console_stream(stream):
    """Best-effort UTF-8 configuration for console streams on Windows."""
    encoding = (getattr(stream, "encoding", None) or "").lower()
    if hasattr(stream, "reconfigure") and "utf" not in encoding:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    return stream


def setup_logging(level: str = "INFO", log_file: str = None) -> logging.Logger:
    """Configure structlog + stdlib bridge for JSON output.

    Returns the `arbiter` logger to preserve compatibility with the prior signature.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Configure structlog itself (calls via structlog.get_logger(...))
    structlog.configure(
        processors=SHARED_PROCESSORS + [ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib root handler to use ProcessorFormatter -> JSON
    formatter = ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[ProcessorFormatter.remove_processors_meta, JSONRenderer()],
    )

    console_handler = logging.StreamHandler(prepare_console_stream(sys.stdout))
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.setLevel(log_level)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    arbiter_logger = logging.getLogger("arbiter")
    arbiter_logger.setLevel(log_level)
    return arbiter_logger


class TradeLogger:
    """Structured trade event logger for audit trail."""

    def __init__(self):
        self.logger = logging.getLogger("arbiter.trades")

    def log_opportunity(self, canonical_id: str, platform_a: str, platform_b: str,
                        yes_a: float, no_b: float, edge_cents: float, fees_cents: float):
        self.logger.info(
            "trade.opportunity",
            extra={
                "canonical_id": canonical_id,
                "platform_a": platform_a,
                "platform_b": platform_b,
                "yes_a": yes_a,
                "no_b": no_b,
                "edge_cents": edge_cents,
                "fees_cents": fees_cents,
                "net_cents": edge_cents - fees_cents,
            },
        )

    def log_execution(self, canonical_id: str, platform: str, side: str,
                      price: float, quantity: int, order_id: str):
        self.logger.info(
            "trade.execution",
            extra={
                "canonical_id": canonical_id,
                "platform": platform,
                "side": side,
                "price": price,
                "quantity": quantity,
                "order_id": order_id,
            },
        )

    def log_balance(self, platform: str, balance: float, threshold: float):
        if balance < threshold:
            self.logger.warning(
                "trade.balance.low",
                extra={"platform": platform, "balance": balance, "threshold": threshold},
            )
        else:
            self.logger.info(
                "trade.balance.ok",
                extra={"platform": platform, "balance": balance, "threshold": threshold},
            )

    def log_error(self, component: str, error: str):
        self.logger.error("trade.error", extra={"component": component, "error": error})
