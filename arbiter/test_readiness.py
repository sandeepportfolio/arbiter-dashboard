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


class ScopedProfitability(StubProfitability):
    def __init__(self, verdict: str, scoped_allowed: bool, scoped_reason: str):
        super().__init__(verdict)
        self.scoped_allowed = scoped_allowed
        self.scoped_reason = scoped_reason

    def validate_opportunity_scope(self, opportunity):
        return self.scoped_allowed, self.scoped_reason, {"scope": "test"}


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


def test_allow_execution_uses_scoped_profitability_for_specific_pair():
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
            "forecastex": StubCollector(circuit_state="open"),
        }
        readiness = OperationalReadiness(
            config,
            engine=StubEngine(),
            monitor=StubMonitor(balances),
            profitability=ScopedProfitability(
                "collecting_evidence",
                True,
                "platform and pair profitability validated",
            ),
            collectors=collectors,
        )

        allowed, reason, _ = readiness.allow_execution(make_opportunity())

        assert allowed is True
        assert reason == "ready for live execution"
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


def test_snapshot_exposes_venue_scoped_pair_readiness_when_forecastex_degraded():
    """Operator readiness must show ForecastEx outage blocks only FX pairs."""
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
            "forecastex": SimpleNamespace(balance=100.0, is_low=False, timestamp=1.0),
        }
        readiness = OperationalReadiness(
            config,
            engine=StubEngine(),
            monitor=StubMonitor(balances),
            profitability=ScopedProfitability("validated_profitable", True, "ok"),
            collectors={
                "kalshi": StubCollector(authenticated=True),
                "polymarket": StubCollector(authenticated=True),
                "forecastex": StubCollector(authenticated=True, circuit_state="open"),
            },
        )

        payload = readiness.refresh().to_dict()

        assert payload["ready_for_live_trading"] is False
        assert payload["blocking_reasons"] == ["Collector health is degraded: forecastex"]
        assert payload["venue_readiness"]["forecastex"]["ready_for_live_trading"] is False
        assert payload["venue_readiness"]["kalshi"]["ready_for_live_trading"] is True
        assert payload["venue_readiness"]["polymarket"]["ready_for_live_trading"] is True
        assert payload["venue_pairs"]["kalshi:polymarket"]["ready_for_live_trading"] is True
        assert payload["venue_pairs"]["kalshi:forecastex"]["ready_for_live_trading"] is False
        assert payload["venue_pairs"]["polymarket:forecastex"]["ready_for_live_trading"] is False
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


def test_allow_execution_blocks_fx_leg_when_forecastex_collector_degraded():
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
            "forecastex": SimpleNamespace(balance=100.0, is_low=False, timestamp=1.0),
        }
        collectors = {
            "kalshi": StubCollector(authenticated=True),
            "polymarket": StubCollector(authenticated=True),
            "forecastex": StubCollector(authenticated=True, circuit_state="open"),
        }
        readiness = OperationalReadiness(
            config,
            engine=StubEngine(),
            monitor=StubMonitor(balances),
            profitability=ScopedProfitability("validated_profitable", True, "ok"),
            collectors=collectors,
        )
        opp = make_opportunity()
        opp.no_platform = "forecastex"
        opp.no_market_id = "FX-NO"

        allowed, reason, _ = readiness.allow_execution(opp)

        assert allowed is False
        assert reason == "Collector health is degraded: forecastex"
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


def test_allow_execution_blocks_kp_when_kalshi_collector_degraded():
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
        readiness = OperationalReadiness(
            config,
            engine=StubEngine(),
            monitor=StubMonitor(balances),
            profitability=ScopedProfitability("validated_profitable", True, "ok"),
            collectors={
                "kalshi": StubCollector(authenticated=True, circuit_state="open"),
                "polymarket": StubCollector(authenticated=True),
                "forecastex": StubCollector(authenticated=True),
            },
        )

        allowed, reason, _ = readiness.allow_execution(make_opportunity())

        assert allowed is False
        assert reason == "Collector health is degraded: kalshi"
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


def test_allow_execution_blocks_unprofitable_scoped_pair():
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
        readiness = OperationalReadiness(
            config,
            engine=StubEngine(),
            monitor=StubMonitor(balances),
            profitability=ScopedProfitability(
                "validated_profitable",
                False,
                "Venue-pair profitability for kalshi_polymarket is not_profitable",
            ),
            collectors={
                "kalshi": StubCollector(authenticated=True),
                "polymarket": StubCollector(authenticated=True),
            },
        )

        allowed, reason, context = readiness.allow_execution(make_opportunity())

        assert allowed is False
        assert "kalshi_polymarket" in reason
        assert "scoped_profitability" in context
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


def test_allow_execution_ignores_incidents_on_uninvolved_venues():
    """BUG #3: A ForecastEx-only critical incident must NOT block a
    Kalshi×Polymarket trade. The legacy ``incidents`` gate counted
    every open critical incident regardless of which venue produced it,
    so a single FX runtime failure paused the entire system. Narrow the
    check to the venues actually involved in the opportunity.
    """
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
        # Critical incident tagged with platform=forecastex — must not
        # block a kalshi×polymarket opportunity.
        fx_only_incident = ExecutionIncident(
            incident_id="INC-FX-ONLY",
            arb_id="ARB-FX-1",
            canonical_id="FX_SOMETHING",
            severity="critical",
            message="ForecastEx auth failed mid-cycle",
            timestamp=1.0,
            metadata={"platform": "forecastex"},
        )
        engine = SimpleNamespace(incidents=[fx_only_incident])
        readiness = OperationalReadiness(
            config,
            engine=engine,
            monitor=StubMonitor(balances),
            profitability=ScopedProfitability(
                "validated_profitable", True, "ok",
            ),
            collectors={
                "kalshi": StubCollector(authenticated=True),
                "polymarket": StubCollector(authenticated=True),
                "forecastex": StubCollector(authenticated=True),
            },
        )

        allowed, reason, _ = readiness.allow_execution(make_opportunity())

        assert allowed is True, f"FX-only incident should not block K×P: {reason}"
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


def test_allow_execution_still_blocks_for_incidents_on_involved_venue():
    """BUG #3 (counterpart): an incident on a venue the opportunity DOES
    touch must still block the trade. Narrowing must not become a free
    pass for the trade venue itself.
    """
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
        kalshi_incident = ExecutionIncident(
            incident_id="INC-K-1", arb_id="ARB-K-1",
            canonical_id="K_BAD", severity="critical",
            message="Kalshi naked leg",
            timestamp=1.0, metadata={"platform": "kalshi"},
        )
        engine = SimpleNamespace(incidents=[kalshi_incident])
        readiness = OperationalReadiness(
            config, engine=engine, monitor=StubMonitor(balances),
            profitability=ScopedProfitability(
                "validated_profitable", True, "ok",
            ),
            collectors={
                "kalshi": StubCollector(authenticated=True),
                "polymarket": StubCollector(authenticated=True),
            },
        )

        allowed, reason, _ = readiness.allow_execution(make_opportunity())

        assert allowed is False, "Kalshi incident must block K×P trade"
        assert "critical" in reason.lower() or "incident" in reason.lower()
    finally:
        if original is None:
            MARKET_MAP.pop("TEST_READY", None)
        else:
            MARKET_MAP["TEST_READY"] = original


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
