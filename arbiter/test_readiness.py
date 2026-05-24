from types import SimpleNamespace

from arbiter.config.settings import ArbiterConfig, MARKET_MAP, load_config
from arbiter.execution.engine import ExecutionIncident
from arbiter.readiness import OperationalReadiness
from arbiter.scanner.arbitrage import ArbitrageOpportunity


class StubCollector:
    def __init__(self, *, total_fetches=1, consecutive_errors=0, circuit_state="closed", authenticated=True):
        self.total_fetches = total_fetches
        self.total_errors = consecutive_errors
        self.consecutive_errors = consecutive_errors
        self.circuit = SimpleNamespace(stats={"state": circuit_state})
        self.auth = SimpleNamespace(is_authenticated=authenticated)


class StubProfitability:
    def __init__(self, verdict: str):
        self._snapshot = SimpleNamespace(
            verdict=verdict,
            progress=0.4,
            total_realized_pnl=3.25,
            completed_executions=12,
        )

    def get_snapshot(self):
        return self._snapshot


class StubMonitor:
    def __init__(self, balances):
        self.current_balances = balances


class StubEngine:
    incidents = []


def make_opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id="TEST_READY",
        description="Readiness gate test",
        yes_platform="kalshi",
        yes_price=0.40,
        yes_fee=0.01,
        yes_market_id="K-READY",
        no_platform="polymarket",
        no_price=0.45,
        no_fee=0.01,
        no_market_id="P-READY",
        gross_edge=0.15,
        total_fees=0.02,
        net_edge=0.13,
        net_edge_cents=13.0,
        suggested_qty=10,
        max_profit_usd=1.3,
        timestamp=0.0,
        confidence=0.95,
        status="tradable",
        persistence_count=3,
        quote_age_seconds=1.0,
        min_available_liquidity=100.0,
        mapping_status="confirmed",
        mapping_score=0.95,
        requires_manual=False,
        yes_fee_rate=0.07,
        no_fee_rate=0.01,
    )


def test_startup_preflight_requires_verified_live_mappings_and_credentials(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)

    original_map = {key: dict(value) for key, value in MARKET_MAP.items()}
    for mapping in MARKET_MAP.values():
        mapping["allow_auto_trade"] = False
        mapping["status"] = "candidate"

    try:
        readiness = OperationalReadiness(ArbiterConfig())
        failures = readiness.startup_failures()
    finally:
        MARKET_MAP.clear()
        MARKET_MAP.update(original_map)

    assert "No confirmed auto-trade mappings are enabled" in failures
    assert "Kalshi API credentials are not configured" in failures
    assert "Polymarket private key is not configured" in failures


def test_startup_preflight_accepts_polymarket_us_credentials(monkeypatch):
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "pm-us-key")
    monkeypatch.setenv(
        "POLYMARKET_US_API_SECRET",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kalshi")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/kalshi.pem")

    readiness = OperationalReadiness(load_config())
    failures = readiness.startup_failures()
    check = readiness._check_platform_credentials()

    assert "Polymarket US credentials are not configured" not in failures
    assert check.status == "pass"
    assert check.details["polymarket_variant"] == "us"



def test_allow_execution_stays_closed_until_profitability_validates():
    original = MARKET_MAP.get("TEST_READY")
    MARKET_MAP["TEST_READY"] = {
        "description": "Readiness gate market",
        "status": "confirmed",
        "allow_auto_trade": True,
        "mapping_score": 0.95,
    }
    try:
        config = ArbiterConfig()
        config.scanner.dry_run = False
        config.alerts.telegram_bot_token = "token"
        config.alerts.telegram_chat_id = "chat"
        config.polymarket.private_key = "poly"
        config.kalshi.api_key_id = "kalshi"
        config.kalshi.private_key_path = "/tmp/key.pem"

        balances = {
            "kalshi": SimpleNamespace(balance=100.0, is_low=False, timestamp=1.0),
            "polymarket": SimpleNamespace(balance=100.0, is_low=False, timestamp=1.0),
        }
        collectors = {
            "kalshi": StubCollector(authenticated=True),
            "polymarket": StubCollector(authenticated=True),
        }
        readiness = OperationalReadiness(
            config,
            engine=StubEngine(),
            monitor=StubMonitor(balances),
            profitability=StubProfitability("collecting_evidence"),
            collectors=collectors,
        )

        allowed, reason, context = readiness.allow_execution(make_opportunity())
        assert allowed is False
        assert "Profitability is still collecting evidence" in reason
        assert context["ready_for_live_trading"] is False
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


def test_collectors_check_identifies_only_the_failing_platform():
    """A degraded kalshi collector must not drag polymarket into the failure
    list. The dashboard surfaces each failing collector by name; lumping them
    together hides which venue actually needs operator attention."""
    collectors = {
        "kalshi": StubCollector(circuit_state="open"),
        "polymarket": StubCollector(),
    }
    readiness = OperationalReadiness(ArbiterConfig(), collectors=collectors)
    check = readiness._check_collectors()

    assert check.status == "fail"
    assert "kalshi" in check.summary
    assert "polymarket" not in check.summary


def test_collectors_check_distinguishes_warming_from_failing_per_platform():
    """Mixed states: one warming-up, one failing. Warming-up shouldn't mask
    the failure, and the failure shouldn't mis-label the warming-up venue."""
    collectors = {
        "kalshi": StubCollector(circuit_state="open"),
        "polymarket": StubCollector(total_fetches=0),
        "forecastex": StubCollector(),
    }
    readiness = OperationalReadiness(ArbiterConfig(), collectors=collectors)
    check = readiness._check_collectors()

    # A hard failure takes precedence over warming-up.
    assert check.status == "fail"
    assert "kalshi" in check.summary
    # Per-platform telemetry is still recorded for both other platforms.
    assert set(check.details.keys()) == {"kalshi", "polymarket", "forecastex"}
    assert check.details["polymarket"]["total_fetches"] == 0
    assert check.details["forecastex"]["total_fetches"] == 1


def test_collectors_check_reports_per_platform_warming_only_for_warming_platforms():
    """When no collector has failed but one is warming up, the warning must
    name just that platform — not every collector in the dict."""
    collectors = {
        "kalshi": StubCollector(),
        "polymarket": StubCollector(total_fetches=0),
    }
    readiness = OperationalReadiness(ArbiterConfig(), collectors=collectors)
    check = readiness._check_collectors()

    assert check.status == "warning"
    assert "polymarket" in check.summary
    assert "kalshi" not in check.summary


def test_incident_check_counts_half_recorded_summary_arb_count():
    incident = ExecutionIncident(
        incident_id="INC-HALF-RECORDED-SUMMARY",
        arb_id="MULTIPLE",
        canonical_id="HALF_RECORDED_ARBS",
        severity="critical",
        message="32 half-recorded arb(s) remain unresolved after startup recovery.",
        timestamp=1.0,
        metadata={
            "event_type": "half_recorded_arb_summary",
            "count": 32,
            "sample_arb_ids": ["ARB-000220"],
        },
    )
    readiness = OperationalReadiness(
        ArbiterConfig(),
        engine=SimpleNamespace(incidents=[incident]),
    )

    check = readiness._check_incidents()

    assert check.status == "fail"
    assert check.summary == "32 critical incidents remain unresolved"
    assert check.details["critical_incident_count"] == 32
    assert check.details["runtime_critical_incident_count"] == 1
