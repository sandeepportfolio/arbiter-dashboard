"""Tests for the ForecastEx ↔ Kalshi cross-platform matcher.

Covers:
  - Threshold parsing from FX descriptions
  - Threshold parsing from Kalshi tickers/titles
  - Exact threshold matching (3.0 == 3.0, rejects 3.0 vs 2.9)
  - Direction normalization and compatibility
  - Period extraction and normalization
  - Cross-platform matching engine
  - Canonical ID generation
  - Match quality classification
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from arbiter.mapping.fx_cross_platform_matcher import (
    FX_FAMILIES,
    FXFamily,
    MatchResult,
    ParsedContract,
    _generate_canonical_id,
    _normalize_direction,
    _normalize_period,
    direction_compatible,
    discover_fx_contracts,
    insert_verified_mappings,
    match_contracts,
    parse_fx_contract,
    parse_kalshi_market,
    period_compatible,
    run_cross_platform_matching,
    search_kalshi_markets,
    threshold_matches_exactly,
    validate_live_quotes,
)


# ── Threshold parsing: ForecastEx descriptions ─────────────────────────────

class TestParseFXContract:
    def test_above_percentage(self):
        c = parse_fx_contract("US CPI YoY Above 3.0%", symbol="CPIY", family="CPIY")
        assert c.threshold == 3.0
        assert c.direction == "above"
        assert c.unit == "%"
        assert c.family == "CPIY"

    def test_below_percentage(self):
        c = parse_fx_contract("Fed Funds Rate Below 4.75%", symbol="FF", family="FF")
        assert c.threshold == 4.75
        assert c.direction == "below"

    def test_at_or_above(self):
        c = parse_fx_contract("US Unemployment At or Above 4.0%", symbol="UNR", family="UNR")
        assert c.threshold == 4.0
        assert c.direction == "at_or_above"

    def test_at_or_below(self):
        c = parse_fx_contract("Real GDP At or Below 2.5%", symbol="RGDP", family="RGDP")
        assert c.threshold == 2.5
        assert c.direction == "at_or_below"

    def test_above_count_k(self):
        c = parse_fx_contract("US Initial Jobless Claims Above 250K", symbol="IJC", family="IJC")
        assert c.threshold == 250.0
        assert c.direction == "above"

    def test_strike_as_threshold_fallback(self):
        c = parse_fx_contract("Some event", symbol="CPIY", family="CPIY", strike=3.5)
        assert c.threshold == 3.5

    def test_no_threshold(self):
        c = parse_fx_contract("Random text with no numbers", symbol="XX")
        assert c.threshold is None

    def test_negative_threshold(self):
        c = parse_fx_contract("GDP Growth Above -1.5%", symbol="RGDP", family="RGDP")
        assert c.threshold == -1.5
        assert c.direction == "above"

    def test_period_from_description(self):
        c = parse_fx_contract("US CPI YoY Jun 2026 Above 3.0%", symbol="CPIY")
        assert c.period == "2026-06"

    def test_period_from_symbol(self):
        c = parse_fx_contract("Some CPI event Above 3.0%", symbol="CPIY-26JUN")
        assert c.period == "2026-06"

    def test_conid_preserved(self):
        c = parse_fx_contract("Test Above 1.0%", conid="123456")
        assert c.conid == "123456"

    def test_right_preserved(self):
        c = parse_fx_contract("Test Above 1.0%", right="C")
        assert c.right == "C"

    def test_comma_in_threshold(self):
        c = parse_fx_contract("National Debt Above 35,000B", symbol="ND", family="ND")
        assert c.threshold == 35000.0


# ── Threshold parsing: Kalshi tickers/titles ───────────────────────────────

class TestParseKalshiMarket:
    def test_t_prefix_threshold(self):
        c = parse_kalshi_market("KXCPI-26JUN11-T3.0", "CPI YoY above 3.0%")
        assert c.threshold == 3.0
        assert c.period == "2026-06"

    def test_gdp_ticker(self):
        c = parse_kalshi_market("KXGDP-26JUL30-T2.5", "GDP growth at or above 2.5%")
        assert c.threshold == 2.5
        assert c.direction == "at_or_above"
        assert c.period == "2026-07"

    def test_direction_from_title_above(self):
        c = parse_kalshi_market("CPI-26-T3.0", "Will CPI be above 3.0%?")
        assert c.direction == "above"

    def test_direction_from_title_below(self):
        c = parse_kalshi_market("UNRATE-26JUL-T4.0", "Unemployment below 4.0%")
        assert c.direction == "below"

    def test_direction_or_higher(self):
        c = parse_kalshi_market("FED-T4.75", "Fed funds rate 4.75% or higher")
        assert c.direction == "at_or_above"

    def test_direction_or_lower(self):
        c = parse_kalshi_market("FED-T4.25", "Rate 4.25% or lower")
        assert c.direction == "at_or_below"

    def test_period_from_ticker(self):
        c = parse_kalshi_market("KXCPI-26JUN11-T3.0")
        assert c.period == "2026-06"

    def test_period_from_title(self):
        c = parse_kalshi_market("CPI-T3.0", "June 2026 CPI above 3.0%")
        assert c.period == "2026-06"

    def test_quarter_period(self):
        c = parse_kalshi_market("GDP-T2.0", "Q2 2026 GDP above 2.0%")
        assert c.period == "2026-Q2"

    def test_no_threshold(self):
        c = parse_kalshi_market("RANDOM-TICKER", "Random market")
        assert c.threshold is None

    def test_subtitle_used(self):
        c = parse_kalshi_market("CPI-T3.0", "", subtitle="CPI above 3.0% in Jun 2026")
        assert c.threshold == 3.0
        assert c.direction == "above"


# ── Threshold key formatting ───────────────────────────────────────────────

class TestThresholdKey:
    def test_integer_formatted(self):
        c = ParsedContract(raw_text="", threshold=3.0)
        assert c.threshold_key == "3.0"

    def test_decimal_preserved(self):
        c = ParsedContract(raw_text="", threshold=4.75)
        assert c.threshold_key == "4.75"

    def test_trailing_zeros_stripped(self):
        c = ParsedContract(raw_text="", threshold=3.50)
        assert c.threshold_key == "3.5"

    def test_none_threshold(self):
        c = ParsedContract(raw_text="", threshold=None)
        assert c.threshold_key == ""

    def test_negative(self):
        c = ParsedContract(raw_text="", threshold=-1.5)
        assert c.threshold_key == "-1.5"

    def test_large_number(self):
        c = ParsedContract(raw_text="", threshold=35000.0)
        assert c.threshold_key == "35000.0"


# ── Exact threshold matching ──────────────────────────────────────────────

class TestThresholdMatchesExactly:
    def test_exact_match(self):
        fx = ParsedContract(raw_text="", threshold=3.0)
        k = ParsedContract(raw_text="", threshold=3.0)
        assert threshold_matches_exactly(fx, k) is True

    def test_near_miss_rejected(self):
        """3.0 vs 2.9 MUST be rejected."""
        fx = ParsedContract(raw_text="", threshold=3.0)
        k = ParsedContract(raw_text="", threshold=2.9)
        assert threshold_matches_exactly(fx, k) is False

    def test_close_but_not_exact(self):
        """3.0 vs 3.1 MUST be rejected."""
        fx = ParsedContract(raw_text="", threshold=3.0)
        k = ParsedContract(raw_text="", threshold=3.1)
        assert threshold_matches_exactly(fx, k) is False

    def test_none_threshold_rejected(self):
        fx = ParsedContract(raw_text="", threshold=None)
        k = ParsedContract(raw_text="", threshold=3.0)
        assert threshold_matches_exactly(fx, k) is False

    def test_both_none_rejected(self):
        fx = ParsedContract(raw_text="", threshold=None)
        k = ParsedContract(raw_text="", threshold=None)
        assert threshold_matches_exactly(fx, k) is False

    def test_decimal_precision(self):
        """4.75 == 4.75."""
        fx = ParsedContract(raw_text="", threshold=4.75)
        k = ParsedContract(raw_text="", threshold=4.75)
        assert threshold_matches_exactly(fx, k) is True

    def test_decimal_precision_mismatch(self):
        """4.75 != 4.74."""
        fx = ParsedContract(raw_text="", threshold=4.75)
        k = ParsedContract(raw_text="", threshold=4.74)
        assert threshold_matches_exactly(fx, k) is False

    def test_negative_match(self):
        fx = ParsedContract(raw_text="", threshold=-1.5)
        k = ParsedContract(raw_text="", threshold=-1.5)
        assert threshold_matches_exactly(fx, k) is True

    def test_negative_mismatch(self):
        fx = ParsedContract(raw_text="", threshold=-1.5)
        k = ParsedContract(raw_text="", threshold=-1.4)
        assert threshold_matches_exactly(fx, k) is False


# ── Direction compatibility ────────────────────────────────────────────────

class TestDirectionCompatible:
    def test_same_direction(self):
        fx = ParsedContract(raw_text="", direction="above")
        k = ParsedContract(raw_text="", direction="above")
        assert direction_compatible(fx, k) is True

    def test_above_and_at_or_above(self):
        fx = ParsedContract(raw_text="", direction="above")
        k = ParsedContract(raw_text="", direction="at_or_above")
        assert direction_compatible(fx, k) is True

    def test_below_and_at_or_below(self):
        fx = ParsedContract(raw_text="", direction="below")
        k = ParsedContract(raw_text="", direction="at_or_below")
        assert direction_compatible(fx, k) is True

    def test_above_vs_below_incompatible(self):
        fx = ParsedContract(raw_text="", direction="above")
        k = ParsedContract(raw_text="", direction="below")
        assert direction_compatible(fx, k) is False

    def test_missing_direction_compatible(self):
        """Missing direction on one side allows match."""
        fx = ParsedContract(raw_text="", direction="above")
        k = ParsedContract(raw_text="", direction="")
        assert direction_compatible(fx, k) is True

    def test_both_missing_compatible(self):
        fx = ParsedContract(raw_text="", direction="")
        k = ParsedContract(raw_text="", direction="")
        assert direction_compatible(fx, k) is True


# ── Period compatibility ───────────────────────────────────────────────────

class TestPeriodCompatible:
    def test_same_period(self):
        fx = ParsedContract(raw_text="", period="2026-06")
        k = ParsedContract(raw_text="", period="2026-06")
        assert period_compatible(fx, k) is True

    def test_different_period(self):
        fx = ParsedContract(raw_text="", period="2026-06")
        k = ParsedContract(raw_text="", period="2026-07")
        assert period_compatible(fx, k) is False

    def test_missing_period_compatible(self):
        fx = ParsedContract(raw_text="", period="2026-06")
        k = ParsedContract(raw_text="", period="")
        assert period_compatible(fx, k) is True


# ── Direction normalization ────────────────────────────────────────────────

class TestNormalizeDirection:
    def test_above(self):
        assert _normalize_direction("above") == "above"
        assert _normalize_direction("Above") == "above"

    def test_below(self):
        assert _normalize_direction("below") == "below"

    def test_at_or_above(self):
        assert _normalize_direction("at or above") == "at_or_above"

    def test_at_or_below(self):
        assert _normalize_direction("at or below") == "at_or_below"

    def test_symbols(self):
        assert _normalize_direction(">") == "above"
        assert _normalize_direction("<") == "below"
        assert _normalize_direction(">=") == "at_or_above"
        assert _normalize_direction("<=") == "at_or_below"


# ── Period normalization ───────────────────────────────────────────────────

class TestNormalizePeriod:
    def test_month_year(self):
        assert _normalize_period("Jun 2026") == "2026-06"
        assert _normalize_period("June 2026") == "2026-06"
        assert _normalize_period("Dec 2026") == "2026-12"

    def test_quarter(self):
        assert _normalize_period("Q2 2026") == "2026-Q2"
        assert _normalize_period("2026 Q1") == "2026-Q1"

    def test_yymm_format(self):
        assert _normalize_period("26JUN") == "2026-06"
        assert _normalize_period("26JUL30") == "2026-07"

    def test_empty(self):
        assert _normalize_period("") == ""


# ── Canonical ID generation ────────────────────────────────────────────────

class TestGenerateCanonicalId:
    def test_basic_id(self):
        family = FXFamily(
            fx_symbol_prefix="CPIY", name="CPI", kalshi_event_prefixes=(),
            kalshi_series_prefixes=(), indicator_type="percent", tags=(),
        )
        contract = ParsedContract(
            raw_text="", threshold=3.0, direction="above", period="2026-06",
        )
        cid = _generate_canonical_id(family, contract)
        assert cid == "FX_CPIY_202606_ABOVE_T3.0"

    def test_at_or_above(self):
        family = FXFamily(
            fx_symbol_prefix="UNR", name="Unemployment", kalshi_event_prefixes=(),
            kalshi_series_prefixes=(), indicator_type="percent", tags=(),
        )
        contract = ParsedContract(
            raw_text="", threshold=4.0, direction="at_or_above", period="2026-07",
        )
        cid = _generate_canonical_id(family, contract)
        assert cid == "FX_UNR_202607_ATORABOVE_T4.0"

    def test_no_period(self):
        family = FXFamily(
            fx_symbol_prefix="FF", name="Fed Funds", kalshi_event_prefixes=(),
            kalshi_series_prefixes=(), indicator_type="rate", tags=(),
        )
        contract = ParsedContract(
            raw_text="", threshold=4.75, direction="below",
        )
        cid = _generate_canonical_id(family, contract)
        assert cid == "FX_FF_BELOW_T4.75"


# ── Cross-platform matching engine ─────────────────────────────────────────

class TestMatchContracts:
    def _make_family(self, prefix="CPIY"):
        return FXFamily(
            fx_symbol_prefix=prefix, name=f"Test {prefix}",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=("test",),
        )

    def test_exact_match(self):
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI Above 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="111", right="C", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="KXCPI-26JUN-T3.0", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="KXCPI-26JUN-T3.0",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert len(matches) == 1
        assert matches[0].kalshi_ticker == "KXCPI-26JUN-T3.0"

    def test_threshold_mismatch_rejected(self):
        """3.0 vs 2.9 must produce zero matches."""
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI Above 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="111", right="C", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="KXCPI-26JUN-T2.9", threshold=2.9,
                    direction="above", period="2026-06",
                    symbol="KXCPI-26JUN-T2.9",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert len(matches) == 0

    def test_direction_compatible_match(self):
        """above ↔ at_or_above should match."""
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI Above 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="111", right="C", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="KXCPI-T3.0", threshold=3.0,
                    direction="at_or_above", period="2026-06",
                    symbol="KXCPI-26JUN-T3.0",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert len(matches) == 1

    def test_direction_incompatible_rejected(self):
        """above vs below must be rejected even with matching threshold."""
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI Above 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="111", right="C", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="KXCPI-T3.0", threshold=3.0,
                    direction="below", period="2026-06",
                    symbol="KXCPI-26JUN-T3.0",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert len(matches) == 0

    def test_yes_no_pair_resolved(self):
        """YES and NO conids should both be captured."""
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI Above 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="111", right="C", family="CPIY",
                ),
                ParsedContract(
                    raw_text="CPI Above 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="222", right="P", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="KXCPI-T3.0", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="KXCPI-26JUN-T3.0",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert len(matches) == 1
        assert matches[0].fx_yes_conid == "111"
        assert matches[0].fx_no_conid == "222"

    def test_multiple_families(self):
        """Matches across different families don't cross-contaminate."""
        cpi_family = self._make_family("CPIY")
        gdp_family = self._make_family("RGDP")
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI 3.0%", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="CPIY", conid="111", right="C", family="CPIY",
                ),
            ],
            "RGDP": [
                ParsedContract(
                    raw_text="GDP 2.5%", threshold=2.5,
                    direction="above", period="2026-Q2",
                    symbol="RGDP", conid="333", right="C", family="RGDP",
                ),
            ],
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="CPI", threshold=3.0,
                    direction="above", period="2026-06",
                    symbol="KXCPI-26JUN-T3.0",
                ),
            ],
            "RGDP": [
                ParsedContract(
                    raw_text="GDP", threshold=2.5,
                    direction="above", period="2026-Q2",
                    symbol="KXGDP-26Q2-T2.5",
                ),
            ],
        }
        matches = match_contracts(fx, kalshi, families=(cpi_family, gdp_family))
        assert len(matches) == 2


# ── Match quality ──────────────────────────────────────────────────────────

class TestMatchQuality:
    def _make_family(self):
        return FXFamily(
            fx_symbol_prefix="CPIY", name="CPI",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=("test",),
        )

    def test_exact_with_direction(self):
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="", threshold=3.0, direction="above",
                    period="2026-06", symbol="CPIY", conid="111",
                    right="C", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="", threshold=3.0, direction="above",
                    period="2026-06", symbol="KXCPI-T3.0",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert matches[0].match_quality == "exact_with_direction"

    def test_exact_threshold_only(self):
        family = self._make_family()
        fx = {
            "CPIY": [
                ParsedContract(
                    raw_text="", threshold=3.0, direction="above",
                    period="2026-06", symbol="CPIY", conid="111",
                    right="C", family="CPIY",
                ),
            ]
        }
        kalshi = {
            "CPIY": [
                ParsedContract(
                    raw_text="", threshold=3.0, direction="",
                    period="2026-06", symbol="KXCPI-T3.0",
                ),
            ]
        }
        matches = match_contracts(fx, kalshi, families=(family,))
        assert matches[0].match_quality == "exact_threshold"


# ── Insert verified mappings ───────────────────────────────────────────────

class TestInsertVerifiedMappings:
    @pytest.mark.asyncio
    async def test_dry_run_no_writes(self):
        family = FXFamily(
            fx_symbol_prefix="CPIY", name="CPI",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=("economics",),
        )
        match = MatchResult(
            fx_contract=ParsedContract(
                raw_text="", threshold=3.0, direction="above",
                period="2026-06", symbol="CPIY", conid="111",
                right="C", family="CPIY",
            ),
            kalshi_contract=ParsedContract(
                raw_text="", threshold=3.0, direction="above",
                period="2026-06", symbol="KXCPI-26JUN-T3.0",
            ),
            family=family,
            canonical_id="FX_CPIY_202606_ABOVE_T3.0",
            description="CPI: above 3.0% (2026-06)",
            match_quality="exact_with_direction",
            fx_yes_conid="111",
            fx_no_conid="222",
            kalshi_ticker="KXCPI-26JUN-T3.0",
        )

        store = AsyncMock()
        store.get = AsyncMock(return_value=None)

        count = await insert_verified_mappings([match], store, dry_run=True)
        assert count == 1
        store.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insert_creates_confirmed_mapping(self):
        family = FXFamily(
            fx_symbol_prefix="CPIY", name="CPI",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=("economics",),
        )
        match = MatchResult(
            fx_contract=ParsedContract(
                raw_text="", threshold=3.0, direction="above",
                period="2026-06", symbol="CPIY", conid="111",
                right="C", family="CPIY",
            ),
            kalshi_contract=ParsedContract(
                raw_text="", threshold=3.0, direction="above",
                period="2026-06", symbol="KXCPI-26JUN-T3.0",
            ),
            family=family,
            canonical_id="FX_CPIY_202606_ABOVE_T3.0",
            description="CPI: above 3.0% (2026-06)",
            match_quality="exact_with_direction",
            fx_yes_conid="111",
            fx_no_conid="222",
            kalshi_ticker="KXCPI-26JUN-T3.0",
        )

        store = AsyncMock()
        store.get = AsyncMock(return_value=None)
        store.upsert = AsyncMock()

        count = await insert_verified_mappings([match], store, dry_run=False)
        assert count == 1
        store.upsert.assert_called_once()

        mapping_arg = store.upsert.call_args[0][0]
        assert mapping_arg.status.value == "confirmed"
        assert mapping_arg.allow_auto_trade is True
        assert mapping_arg.mapping_score == 1.0
        assert mapping_arg.kalshi_market_id == "KXCPI-26JUN-T3.0"
        assert mapping_arg.forecastex_contract_id == "111"
        assert mapping_arg.forecastex_no_contract_id == "222"
        assert mapping_arg.resolution_match_status == "identical"

    @pytest.mark.asyncio
    async def test_skip_existing_confirmed(self):
        """Do not overwrite existing confirmed mappings."""
        from arbiter.mapping.market_map import MarketMapping, MappingStatus

        existing = MagicMock()
        existing.status = MappingStatus.CONFIRMED

        store = AsyncMock()
        store.get = AsyncMock(return_value=existing)

        family = FXFamily(
            fx_symbol_prefix="CPIY", name="CPI",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=("economics",),
        )
        match = MatchResult(
            fx_contract=ParsedContract(raw_text="", threshold=3.0, conid="111", right="C"),
            kalshi_contract=ParsedContract(raw_text="", threshold=3.0),
            family=family,
            canonical_id="FX_CPIY_202606_ABOVE_T3.0",
            description="test",
            match_quality="exact_with_direction",
            fx_yes_conid="111", fx_no_conid="222",
            kalshi_ticker="KXCPI-T3.0",
        )

        count = await insert_verified_mappings([match], store, dry_run=False)
        assert count == 0
        store.upsert.assert_not_called()


# ── FX families coverage ──────────────────────────────────────────────────

class TestFXFamilies:
    def test_all_requested_families_defined(self):
        """All 13 requested families from the user spec must be present."""
        prefixes = {f.fx_symbol_prefix for f in FX_FAMILIES}
        expected = {"FF", "CPIY", "UNR", "RGDP", "IJC", "RSM", "HS", "BPMI",
                    "PREMP", "CP", "ND", "GT", "GCE"}
        assert expected.issubset(prefixes), f"Missing: {expected - prefixes}"

    def test_families_have_kalshi_prefixes(self):
        for f in FX_FAMILIES:
            assert f.kalshi_event_prefixes, f"{f.fx_symbol_prefix} has no kalshi_event_prefixes"
            assert f.kalshi_series_prefixes, f"{f.fx_symbol_prefix} has no kalshi_series_prefixes"

    def test_families_have_tags(self):
        for f in FX_FAMILIES:
            assert f.tags, f"{f.fx_symbol_prefix} has no tags"


# ── Validate live quotes ──────────────────────────────────────────────────

class TestValidateLiveQuotes:
    @pytest.mark.asyncio
    async def test_both_platforms_validated(self):
        fx_client = AsyncMock()
        fx_client.market_snapshot = AsyncMock(return_value={"84": "0.55", "86": "0.60"})
        fx_client.is_tradeable_snapshot = MagicMock(return_value=True)

        k_collector = AsyncMock()
        k_collector.get_orderbook = AsyncMock(return_value={"yes": [{"price": 55}]})

        family = FXFamily(
            fx_symbol_prefix="CPIY", name="CPI",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=(),
        )
        match = MatchResult(
            fx_contract=ParsedContract(raw_text="", threshold=3.0, conid="111", right="C"),
            kalshi_contract=ParsedContract(raw_text="", threshold=3.0),
            family=family,
            canonical_id="test",
            description="test",
            match_quality="exact",
            fx_yes_conid="111", fx_no_conid="222",
            kalshi_ticker="KXCPI-T3.0",
        )

        validated = await validate_live_quotes([match], fx_client, k_collector)
        assert len(validated) == 1

    @pytest.mark.asyncio
    async def test_no_quotes_skipped(self):
        fx_client = AsyncMock()
        fx_client.market_snapshot = AsyncMock(return_value={})
        fx_client.is_tradeable_snapshot = MagicMock(return_value=False)

        k_collector = AsyncMock()
        k_collector.get_orderbook = AsyncMock(return_value={})

        family = FXFamily(
            fx_symbol_prefix="CPIY", name="CPI",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=(),
        )
        match = MatchResult(
            fx_contract=ParsedContract(raw_text="", threshold=3.0, conid="111", right="C"),
            kalshi_contract=ParsedContract(raw_text="", threshold=3.0),
            family=family,
            canonical_id="test",
            description="test",
            match_quality="exact",
            fx_yes_conid="111", fx_no_conid="222",
            kalshi_ticker="KXCPI-T3.0",
        )

        validated = await validate_live_quotes([match], fx_client, k_collector)
        assert len(validated) == 0


class TestFailClosedInserts:
    """2026-07-21 regression: matches without a resolved YES/NO child pair
    (parent-conid fallback contracts, right="") were inserted as
    status=confirmed + allow_auto_trade=true. Parent IND conids never trade
    — the collector probes them 8×, disables them, the resolver gets
    no_children 3×, and the mapping lands in forecastex_not_available —
    burning ~24 rate-limited strikes calls per resolver pass on the way
    (the FX_PCEY_*/FX_PREMP lifecycle). Such matches must fail closed into
    operator review with auto-trade off.
    """

    @staticmethod
    def _family():
        return FXFamily(
            fx_symbol_prefix="PREMP", name="Payroll",
            kalshi_event_prefixes=(), kalshi_series_prefixes=(),
            indicator_type="percent", tags=("economics",),
        )

    def _match(self, *, fx_yes_conid="582530257", fx_no_conid="", right=""):
        return MatchResult(
            fx_contract=ParsedContract(
                raw_text="", threshold=3.0, direction="above",
                period="2027-03", symbol="PREMP", conid=fx_yes_conid,
                right=right, family="PREMP",
            ),
            kalshi_contract=ParsedContract(
                raw_text="", threshold=3.0, direction="above",
                period="2027-03", symbol="KXPAYROLL-27MAR-T3.0",
            ),
            family=self._family(),
            canonical_id="FX_PREMP_202703_3p0",
            description="Payroll: above 3.0 (2027-03)",
            match_quality="exact_threshold",
            fx_yes_conid=fx_yes_conid,
            fx_no_conid=fx_no_conid,
            kalshi_ticker="KXPAYROLL-27MAR-T3.0",
        )

    @pytest.mark.asyncio
    async def test_parent_conid_without_no_child_inserts_as_review(self):
        store = AsyncMock()
        store.get = AsyncMock(return_value=None)
        store.upsert = AsyncMock()

        count = await insert_verified_mappings([self._match()], store, dry_run=False)
        assert count == 1
        mapping_arg = store.upsert.call_args[0][0]
        assert mapping_arg.status.value == "review"
        assert mapping_arg.allow_auto_trade is False
        assert "unresolved YES/NO child pair" in mapping_arg.review_note

    @pytest.mark.asyncio
    async def test_resolved_child_pair_still_inserts_confirmed(self):
        store = AsyncMock()
        store.get = AsyncMock(return_value=None)
        store.upsert = AsyncMock()

        match = self._match(
            fx_yes_conid="762089343", fx_no_conid="762089344", right="C",
        )
        count = await insert_verified_mappings([match], store, dry_run=False)
        assert count == 1
        mapping_arg = store.upsert.call_args[0][0]
        assert mapping_arg.status.value == "confirmed"
        assert mapping_arg.allow_auto_trade is True

    @pytest.mark.asyncio
    async def test_conid_zero_match_is_skipped_entirely(self):
        store = AsyncMock()
        store.get = AsyncMock(return_value=None)
        store.upsert = AsyncMock()

        match = self._match(fx_yes_conid="0", fx_no_conid="", right="C")
        count = await insert_verified_mappings([match], store, dry_run=False)
        assert count == 0
        store.upsert.assert_not_called()
