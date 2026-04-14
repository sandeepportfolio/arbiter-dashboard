"""
ARBITER — Prediction Market Arbitrage System
Central configuration for all collectors, scanners, and execution agents.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# Load .env file from arbiter root directory
try:
    from dotenv import load_dotenv
    # Search upward from this file to find .env
    _config_dir = Path(__file__).resolve().parent
    _arbiter_dir = _config_dir.parent
    _env_file = _arbiter_dir / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=True)
except ImportError:
    pass


# ─── Platform Fee Models ───────────────────────────────────────────────
def kalshi_fee(price: float) -> float:
    """Kalshi charges 7% of price × (1 - price), capped at contract value."""
    return 0.07 * price * (1.0 - price)


def polymarket_fee(price: float, category: str = "politics") -> float:
    """Polymarket dynamic fees by category. Politics = 1%."""
    rates = {"politics": 0.01, "sports": 0.02, "crypto": 0.015, "default": 0.02}
    rate = rates.get(category, rates["default"])
    return rate * price


def predictit_fee(profit: float) -> float:
    """PredictIt: 10% profit fee + 5% withdrawal fee on profit."""
    if profit <= 0:
        return 0.0
    return profit * 0.10 + profit * 0.05  # 15% effective on profit


# ─── API Endpoints ─────────────────────────────────────────────────────
@dataclass
class KalshiConfig:
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    api_key_id: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID", ""))
    private_key_path: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH", ""))
    poll_interval: float = 1.0  # seconds between REST polls when WS unavailable


@dataclass
class PolymarketConfig:
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    private_key: str = field(default_factory=lambda: os.getenv("POLY_PRIVATE_KEY", ""))
    chain_id: int = 137  # Polygon mainnet
    poll_interval: float = 1.0
    fee_category: str = "politics"


@dataclass
class PredictItConfig:
    base_url: str = "https://www.predictit.org/api/marketdata/all/"
    poll_interval: float = 5.0  # PredictIt updates ~every 60s, poll every 5s to catch changes
    # No trade API — manual execution only
    # No auth required for market data


# ─── Balance Thresholds & Alerts ───────────────────────────────────────
@dataclass
class AlertConfig:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    # Low balance thresholds (USD)
    kalshi_low: float = 50.0
    polymarket_low: float = 25.0
    predictit_low: float = 100.0
    # Alert cooldown (seconds) — don't spam
    cooldown: float = 300.0


# ─── Arbitrage Scanner Config ──────────────────────────────────────────
@dataclass
class ScannerConfig:
    min_edge_cents: float = 2.0        # minimum profit in cents after fees
    max_position_usd: float = 100.0    # max position size per leg
    predictit_cap: float = 850.0       # PredictIt $850 position limit
    scan_interval: float = 1.0         # seconds between full scans
    confidence_threshold: float = 0.8  # minimum confidence for auto-execution
    dry_run: bool = True               # start in simulation mode


# ─── Redis & Postgres ──────────────────────────────────────────────────
@dataclass
class RedisConfig:
    host: str = field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"))
    port: int = 6379
    db: int = 0
    price_ttl: int = 10  # seconds — prices expire fast


@dataclass
class PostgresConfig:
    host: str = field(default_factory=lambda: os.getenv("PG_HOST", "localhost"))
    port: int = 5432
    database: str = "arbiter"
    user: str = field(default_factory=lambda: os.getenv("PG_USER", "arbiter"))
    password: str = field(default_factory=lambda: os.getenv("PG_PASSWORD", ""))


# ─── Market Mapping ───────────────────────────────────────────────────
# Maps canonical event names to platform-specific identifiers.
# Each platform value is used differently:
#   - kalshi: event_ticker (markets looked up via event_ticker param)
#   - polymarket: event slug (looked up via Gamma API /events?slug=...)
#   - predictit: market ID (integer, from /api/marketdata/all/)
# polymarket_question: substring to match the specific market question within
#   a multi-market Polymarket event (e.g. "Democratic Party" for the Dem market).
MARKET_MAP: Dict[str, Dict[str, str]] = {
    "DEM_HOUSE_2026": {
        "kalshi": "KXPRESPARTY-2028",  # Kalshi has no 2026 House market; placeholder
        "polymarket": "which-party-will-win-the-house-in-2026",
        "polymarket_question": "Democratic Party",
        "predictit": "8157",
        "description": "Democrats win House 2026 midterms",
    },
    "DEM_SENATE_2026": {
        "polymarket": "which-party-will-win-the-senate-in-2026",
        "polymarket_question": "Democratic Party",
        "predictit": "8155",
        "description": "Democrats win Senate 2026 midterms",
    },
    "GOP_SENATE_2026": {
        "polymarket": "which-party-will-win-the-senate-in-2026",
        "polymarket_question": "Republican Party",
        "predictit": "8155",  # same PI market, Republican contract
        "description": "Republicans win Senate 2026 midterms",
    },
    "VANCE_NOM_2028": {
        "polymarket": "republican-presidential-nominee-2028",
        "polymarket_question": "J.D. Vance",
        "predictit": "8152",
        "description": "JD Vance wins 2028 GOP presidential nomination",
    },
    "RUBIO_NOM_2028": {
        "polymarket": "republican-presidential-nominee-2028",
        "polymarket_question": "Marco Rubio",
        "predictit": "8152",  # same PI market, Rubio contract
        "description": "Marco Rubio wins 2028 GOP presidential nomination",
    },
    "NEWSOM_NOM_2028": {
        "polymarket": "democratic-presidential-nominee-2028",
        "polymarket_question": "Gavin Newsom",
        "predictit": "8153",
        "description": "Gavin Newsom wins 2028 DEM presidential nomination",
    },
    "GA_SEN_2026": {
        "polymarket": "georgia-senate-election-winner",
        "polymarket_question": "Democrats win",
        "predictit": "8156",
        "description": "Georgia Senate 2026 — Democratic win",
    },
    "MI_SEN_2026": {
        "polymarket": "michigan-senate-election-winner",
        "polymarket_question": "Democrats win",
        "predictit": "8158",
        "description": "Michigan Senate 2026 — Democratic win",
    },
}


# ─── Master Config ─────────────────────────────────────────────────────
@dataclass
class ArbiterConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    predictit: PredictItConfig = field(default_factory=PredictItConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)


def load_config() -> ArbiterConfig:
    """Load config with env var overrides."""
    cfg = ArbiterConfig()
    # Resolve relative key paths against the arbiter directory
    if cfg.kalshi.private_key_path and not os.path.isabs(cfg.kalshi.private_key_path):
        arbiter_dir = Path(__file__).resolve().parent.parent
        cfg.kalshi.private_key_path = str(arbiter_dir / cfg.kalshi.private_key_path)
    return cfg
