"""
ARBITER configuration, fee math, and canonical market mappings.
"""
from __future__ import annotations

import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, Union

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _load_project_dotenv(anchor_file: Path | None = None) -> Path | None:
    """
    Load the first .env file found in the project/package parents.

    In local development the repo root contains `.env`, while some packaged
    layouts keep it beside the Python package. We support both so runtime
    config matches the developer shell.
    """
    if load_dotenv is None:
        return None

    anchor = (anchor_file or Path(__file__)).resolve()
    config_dir = anchor.parent
    candidate_paths = (
        config_dir.parent.parent / ".env",
        config_dir.parent / ".env",
    )
    for candidate in candidate_paths:
        if candidate.exists():
            # override=False so explicit shell vars (including subprocess envs
            # that deliberately clear a var) win over the repo .env. Fixes
            # WR-02 from the Phase 4 code review.
            load_dotenv(candidate, override=False)
            return candidate
    return None


_DOTENV_PATH = _load_project_dotenv()


KALSHI_TAKER_FEE_RATE = 0.07
POLYMARKET_DEFAULT_TAKER_FEE_RATE = 0.05
# Backwards-compatible alias for older imports.
POLYMAKET_DEFAULT_TAKER_FEE_RATE = POLYMARKET_DEFAULT_TAKER_FEE_RATE
POLYMARKET_DEFAULT_MAKER_FEE_RATE = 0.0


def _clamp_probability(price: float) -> float:
    return max(0.0, min(1.0, float(price)))


def kalshi_order_fee(price: float, quantity: float = 1.0, fee_rate: float = KALSHI_TAKER_FEE_RATE) -> float:
    """
    Kalshi fees are quadratic and rounded up to the nearest cent per order.
    """
    quantity = max(float(quantity), 0.0)
    price = _clamp_probability(price)
    if quantity <= 0 or price <= 0:
        return 0.0
    raw_fee = fee_rate * quantity * price * (1.0 - price)
    return math.ceil((raw_fee * 100.0) - 1e-9) / 100.0


def kalshi_fee(price: float, quantity: float = 1.0) -> float:
    """
    Return the effective per-contract Kalshi fee after order-level rounding.
    """
    quantity = max(float(quantity), 1.0)
    return kalshi_order_fee(price, quantity=quantity) / quantity


def polymarket_order_fee(
    price: float,
    quantity: float = 1.0,
    fee_rate: float | None = None,
    category: str = "default",
) -> float:
    """
    Polymarket uses market-specific fee schedules. When a market-specific rate
    is unavailable, fall back to a conservative taker rate.
    """
    quantity = max(float(quantity), 0.0)
    price = _clamp_probability(price)
    if quantity <= 0 or price <= 0:
        return 0.0

    fallback_rates = {
        "crypto": 0.072,
        "sports": 0.03,
        "finance": 0.04,
        "politics": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "weather": 0.05,
        "tech": 0.04,
        "mentions": 0.04,
        "geopolitics": 0.0,
        "default": POLYMARKET_DEFAULT_TAKER_FEE_RATE,
    }
    resolved_rate = fee_rate if fee_rate is not None else fallback_rates.get(category, fallback_rates["default"])
    resolved_rate = max(float(resolved_rate), 0.0)
    if resolved_rate <= 0:
        return 0.0
    return resolved_rate * quantity * price * (1.0 - price)


def polymarket_fee(
    price: float,
    category: str = "default",
    quantity: float = 1.0,
    fee_rate: float | None = None,
) -> float:
    quantity = max(float(quantity), 1.0)
    return polymarket_order_fee(price, quantity=quantity, fee_rate=fee_rate, category=category) / quantity


_TEXT_NORMALIZER = re.compile(r"[^a-z0-9]+")


def normalize_market_text(text: str) -> str:
    return _TEXT_NORMALIZER.sub(" ", text.lower()).strip()


def similarity_score(*texts: str) -> float:
    """
    Tiny token-overlap heuristic for mapping candidates and UI confidence hints.
    """
    normalized_sets = [{token for token in normalize_market_text(text).split() if token} for text in texts if text]
    if len(normalized_sets) < 2:
        return 0.0
    intersection = set.intersection(*normalized_sets)
    union = set.union(*normalized_sets)
    if not union:
        return 0.0
    return round(len(intersection) / len(union), 4)


@dataclass(frozen=True)
class MarketMappingRecord:
    canonical_id: str
    description: str
    status: str = "confirmed"
    allow_auto_trade: bool = True
    aliases: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    kalshi: str = ""
    polymarket: str = ""
    polymarket_question: str = ""
    notes: str = ""
    # SAFE-06 (plan 03-06): Optional resolution-criteria payload. Structure:
    #   {
    #     "kalshi":       {"source": str, "rule": str, "settlement_date": str},
    #     "polymarket":   {"source": str, "rule": str, "settlement_date": str},
    #     "criteria_match": "identical" | "similar" | "divergent" | "pending_operator_review",
    #     "operator_note": str,
    #   }
    # Left optional so existing MARKET_SEEDS + downstream consumers never
    # raise KeyError (Pitfall 6 of 03-RESEARCH.md).
    resolution_criteria: Optional[Dict[str, Any]] = None
    # Mirror of resolution_criteria["criteria_match"] when present; stored at
    # the top level so API clients and the dashboard can read status without
    # inspecting the criteria dict.
    resolution_match_status: str = "pending_operator_review"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["mapping_score"] = similarity_score(self.description, " ".join(self.aliases))
        # Always emit the SAFE-06 keys (None when unset) so API consumers
        # can safely .get() them without branching.
        payload["resolution_criteria"] = self.resolution_criteria
        payload["resolution_match_status"] = self.resolution_match_status
        return payload


MARKET_SEEDS: Tuple[MarketMappingRecord, ...] = (
    MarketMappingRecord(
        canonical_id="DEM_HOUSE_2026",
        description="Democrats win House 2026 midterms",
        status="candidate",
        allow_auto_trade=False,
        aliases=("democrats house 2026", "house control 2026 democrats"),
        tags=("politics", "midterms", "house"),
        kalshi="KXPRESPARTY-2028",
        polymarket="which-party-will-win-the-house-in-2026",
        polymarket_question="Democratic Party",
        notes="Kalshi lacks a true confirmed 2026 House mapping, so this stays review-only.",
    ),
    MarketMappingRecord(
        canonical_id="DEM_SENATE_2026",
        description="Democrats win Senate 2026 midterms",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("democrats senate 2026", "senate control 2026 democrats"),
        tags=("politics", "midterms", "senate"),
        polymarket="which-party-will-win-the-senate-in-2026",
        polymarket_question="Democratic Party",
        notes="Polymarket only, so keep manual-only until a confirmed Kalshi leg exists.",
    ),
    MarketMappingRecord(
        canonical_id="GOP_SENATE_2026",
        description="Republicans win Senate 2026 midterms",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("republicans senate 2026", "gop senate 2026"),
        tags=("politics", "midterms", "senate"),
        polymarket="which-party-will-win-the-senate-in-2026",
        polymarket_question="Republican Party",
    ),
    MarketMappingRecord(
        canonical_id="VANCE_NOM_2028",
        description="JD Vance wins 2028 GOP presidential nomination",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("vance nominee 2028", "jd vance republican nominee"),
        tags=("politics", "president", "nomination"),
        polymarket="republican-presidential-nominee-2028",
        polymarket_question="J.D. Vance",
    ),
    MarketMappingRecord(
        canonical_id="RUBIO_NOM_2028",
        description="Marco Rubio wins 2028 GOP presidential nomination",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("rubio nominee 2028", "marco rubio republican nominee"),
        tags=("politics", "president", "nomination"),
        polymarket="republican-presidential-nominee-2028",
        polymarket_question="Marco Rubio",
    ),
    MarketMappingRecord(
        canonical_id="NEWSOM_NOM_2028",
        description="Gavin Newsom wins 2028 Democratic presidential nomination",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("newsom nominee 2028", "gavin newsom democratic nominee"),
        tags=("politics", "president", "nomination"),
        polymarket="democratic-presidential-nominee-2028",
        polymarket_question="Gavin Newsom",
    ),
    MarketMappingRecord(
        canonical_id="GA_SEN_2026",
        description="Georgia Senate 2026 Democratic win",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("georgia senate 2026 democrats", "ga senate 2026"),
        tags=("politics", "senate", "georgia"),
        polymarket="georgia-senate-election-winner",
        polymarket_question="Democrats win",
    ),
    MarketMappingRecord(
        canonical_id="MI_SEN_2026",
        description="Michigan Senate 2026 Democratic win",
        status="confirmed",
        allow_auto_trade=False,
        aliases=("michigan senate 2026 democrats", "mi senate 2026"),
        tags=("politics", "senate", "michigan"),
        polymarket="michigan-senate-election-winner",
        polymarket_question="Democrats win",
    ),
)

MARKET_MAP: Dict[str, Dict[str, object]] = {
    record.canonical_id: record.to_dict()
    for record in MARKET_SEEDS
}


def get_market_mapping(canonical_id: str) -> dict | None:
    return MARKET_MAP.get(canonical_id)


def update_market_mapping(
    canonical_id: str,
    *,
    status: str | None = None,
    note: str | None = None,
    allow_auto_trade: bool | None = None,
    resolution_criteria: dict | None = None,
    resolution_match_status: str | None = None,
    actor: str | None = None,
) -> dict | None:
    """Mutate MARKET_MAP[canonical_id] live.

    Phase 6 Plan 06-05: append an audit-log entry per changed field so operator
    actions are durable across the in-process lifetime. Entries have shape:

        {
          "ts": <float unix>,
          "actor": "<email or 'system'>",
          "field": "status" | "allow_auto_trade" | "resolution_*",
          "old": <prior value>,
          "new": <new value>,
          "note": "<operator-supplied note, if any>"
        }

    Audit entries are stored on the mapping itself at key ``audit_log`` capped
    at 50 entries per mapping (FIFO eviction).
    """
    mapping = MARKET_MAP.get(canonical_id)
    if not mapping:
        return None

    audit_actor = (actor or "system").strip() or "system"
    audit_note = (note or "").strip() or None
    now = time.time()
    audit_entries: list[dict] = list(mapping.get("audit_log") or [])

    def _record(field: str, old, new) -> None:
        if old == new:
            return
        audit_entries.append({
            "ts": now,
            "actor": audit_actor,
            "field": field,
            "old": old,
            "new": new,
            "note": audit_note,
        })

    if status is not None:
        _record("status", mapping.get("status"), status)
        mapping["status"] = status
    if allow_auto_trade is not None:
        _record("allow_auto_trade", mapping.get("allow_auto_trade"), bool(allow_auto_trade))
        mapping["allow_auto_trade"] = bool(allow_auto_trade)
    if note:
        mapping["review_note"] = str(note).strip()
    # SAFE-06 (plan 03-06): optional resolution-criteria persistence. When the
    # caller provides a criteria dict we store it verbatim. The top-level
    # resolution_match_status mirrors criteria.criteria_match unless the
    # caller passes an explicit status (explicit kwarg wins).
    if resolution_criteria is not None:
        _record(
            "resolution_criteria",
            mapping.get("resolution_criteria"),
            resolution_criteria,
        )
        mapping["resolution_criteria"] = resolution_criteria
        criteria_match = resolution_criteria.get("criteria_match")
        if criteria_match and resolution_match_status is None:
            _record(
                "resolution_match_status",
                mapping.get("resolution_match_status"),
                criteria_match,
            )
            mapping["resolution_match_status"] = criteria_match
    if resolution_match_status is not None:
        _record(
            "resolution_match_status",
            mapping.get("resolution_match_status"),
            resolution_match_status,
        )
        mapping["resolution_match_status"] = resolution_match_status

    # Cap audit trail at 50 entries per mapping (FIFO eviction).
    if len(audit_entries) > 50:
        audit_entries = audit_entries[-50:]
    mapping["audit_log"] = audit_entries
    mapping["updated_at"] = now
    return mapping


def iter_confirmed_market_mappings(require_auto_trade: bool = False) -> Iterable[tuple[str, dict]]:
    for canonical_id, mapping in MARKET_MAP.items():
        if mapping.get("status") != "confirmed":
            continue
        if require_auto_trade and not mapping.get("allow_auto_trade", False):
            continue
        yield canonical_id, mapping


@dataclass
class KalshiConfig:
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "KALSHI_BASE_URL",
            "https://api.elections.kalshi.com/trade-api/v2",
        )
    )
    ws_url: str = field(
        default_factory=lambda: os.getenv(
            "KALSHI_WS_URL",
            "wss://api.elections.kalshi.com/trade-api/ws/v2",
        )
    )
    api_key_id: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID", ""))
    private_key_path: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH", ""))
    poll_interval: float = 1.5
    ws_enabled: bool = True


@dataclass
class PolymarketConfig:
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = field(
        default_factory=lambda: os.getenv(
            "POLYMARKET_CLOB_URL",
            "https://clob.polymarket.com",
        )
    )
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    private_key: str = field(default_factory=lambda: os.getenv("POLY_PRIVATE_KEY", ""))
    chain_id: int = 137
    poll_interval: float = 1.0
    ws_enabled: bool = True
    fee_category: str = "politics"
    signature_type: int = field(default_factory=lambda: int(os.getenv("POLY_SIGNATURE_TYPE", "2")))
    funder: str = field(default_factory=lambda: os.getenv("POLY_FUNDER", ""))


@dataclass
class PolymarketUSConfig:
    """Polymarket US (CFTC-regulated DCM) configuration. Uses Ed25519 key auth."""
    api_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1"))
    ws_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_WS_URL", "wss://api.polymarket.us/v1/ws/markets"))
    api_key_id: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_API_KEY_ID", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_US_API_SECRET", ""))
    poll_interval: float = 1.0
    ws_enabled: bool = True


@dataclass
class AlertConfig:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    kalshi_low: float = 50.0
    polymarket_low: float = 25.0
    cooldown: float = 300.0


@dataclass
class ScannerConfig:
    min_edge_cents: float = 2.5
    max_position_usd: float = 100.0
    scan_interval: float = 1.0
    confidence_threshold: float = 0.8
    persistence_scans: int = 3
    max_quote_age_seconds: float = 15.0
    min_liquidity: float = 25.0
    slippage_tolerance: float = 0.01
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() != "false")


@dataclass
class SafetyConfig:
    """Phase 3 safety-layer knobs.

    Owned by ``arbiter.safety.SafetySupervisor``. Extended in plans 03-02
    (per-platform exposure), 03-04 (rate limits), and 03-05 (shutdown).
    """
    min_cooldown_seconds: float = 30.0
    max_platform_exposure_usd: float = 300.0
    rate_limits: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {
            "kalshi": {"write_rps": 10.0, "read_rps": 100.0},
            "polymarket": {"write_rps": 5.0, "read_rps": 50.0},
        }
    )
    enable_redis_state: bool = field(
        default_factory=lambda: os.getenv("SAFETY_REDIS_STATE", "false").lower() == "true"
    )


@dataclass
class RedisConfig:
    host: str = field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"))
    port: int = 6379
    db: int = 0
    price_ttl: int = 20


@dataclass
class PostgresConfig:
    host: str = field(default_factory=lambda: os.getenv("PG_HOST", "localhost"))
    port: int = 5432
    database: str = "arbiter"
    user: str = field(default_factory=lambda: os.getenv("PG_USER", "arbiter"))
    password: str = field(default_factory=lambda: os.getenv("PG_PASSWORD", ""))


@dataclass
class ArbiterConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    polymarket: Optional[Union[PolymarketConfig, PolymarketUSConfig]] = field(default_factory=PolymarketConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)


# Polymarket US fee curve — docs.polymarket.us/fees
_THETA_TAKER = 0.05
_THETA_MAKER = -0.0125  # rebate, signed negative


def polymarket_us_order_fee(price: float, qty: float, intent: str = "taker") -> float:
    """
    Polymarket US fee formula: fee = θ × qty × price × (1−price)
    θ_taker = 0.05; θ_maker = −0.0125 (negative = rebate).
    Result is banker's-rounded to the nearest cent.
    """
    theta = _THETA_TAKER if intent == "taker" else _THETA_MAKER
    raw = theta * qty * price * (1.0 - price)
    # Banker's rounding to cent
    return round(raw * 100) / 100.0


def load_config() -> ArbiterConfig:
    # Select Polymarket variant before constructing ArbiterConfig so the right
    # config class is used. Defaults to "us" to align with the CFTC-regulated
    # DCM path. Set POLYMARKET_VARIANT=legacy to use the legacy CLOB path, or
    # POLYMARKET_VARIANT=disabled to disable Polymarket entirely.
    variant = os.getenv("POLYMARKET_VARIANT", "us").lower()
    if variant == "disabled":
        polymarket_cfg: Optional[Union[PolymarketConfig, PolymarketUSConfig]] = None
    elif variant == "legacy":
        polymarket_cfg = PolymarketConfig()
    else:  # "us" (default)
        polymarket_cfg = PolymarketUSConfig()

    cfg = ArbiterConfig()
    cfg.polymarket = polymarket_cfg

    if cfg.kalshi.private_key_path and not os.path.isabs(cfg.kalshi.private_key_path):
        config_root = _DOTENV_PATH.parent if _DOTENV_PATH else Path(__file__).resolve().parent.parent
        cfg.kalshi.private_key_path = str((config_root / cfg.kalshi.private_key_path).resolve())
    return cfg
