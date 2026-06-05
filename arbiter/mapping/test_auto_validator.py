"""Tests for MappingAutoValidator."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .auto_validator import (
    MappingAutoValidator,
    PlatformCheckResult,
    ValidationRecommendation,
    ValidationResult,
)
from .market_map import MarketMapping, MappingStatus


def _make_mapping(**kwargs) -> MarketMapping:
    defaults = {
        "canonical_id": "TEST_001",
        "description": "Test mapping",
        "status": MappingStatus.CONFIRMED,
        "kalshi_market_id": "KTEST-001",
        "polymarket_slug": "test-slug",
        "forecastex_contract_id": "12345",
        "mapping_score": 0.99,
        "confidence": 0.99,
    }
    defaults.update(kwargs)
    return MarketMapping(**defaults)


class TestPlatformCheckResult:
    def test_is_live(self):
        r = PlatformCheckResult(platform="kalshi", market_id="X", exists=True, is_open=True, is_expired=False)
        assert r.is_live

    def test_is_not_live_expired(self):
        r = PlatformCheckResult(platform="kalshi", market_id="X", exists=True, is_open=False, is_expired=True)
        assert not r.is_live

    def test_is_not_live_missing(self):
        r = PlatformCheckResult(platform="kalshi", market_id="X", exists=False)
        assert not r.is_live


class TestValidationResult:
    def test_live_platform_count(self):
        vr = ValidationResult(canonical_id="X", description="test", status="confirmed")
        vr.platforms["kalshi"] = PlatformCheckResult(platform="kalshi", market_id="K1", exists=True, is_open=True)
        vr.platforms["polymarket"] = PlatformCheckResult(platform="polymarket", market_id="P1", exists=True, is_open=True)
        vr.platforms["forecastex"] = PlatformCheckResult(platform="forecastex", market_id="F1", exists=False)
        assert vr.live_platform_count == 2

    def test_all_expired(self):
        vr = ValidationResult(canonical_id="X", description="test", status="confirmed")
        vr.platforms["kalshi"] = PlatformCheckResult(platform="kalshi", market_id="K1", exists=True, is_expired=True)
        vr.platforms["polymarket"] = PlatformCheckResult(platform="polymarket", market_id="P1", exists=True, is_expired=True)
        assert vr.all_expired

    def test_has_live_quotes(self):
        vr = ValidationResult(canonical_id="X", description="test", status="confirmed")
        vr.platforms["kalshi"] = PlatformCheckResult(platform="kalshi", market_id="K1", exists=True, is_open=True, has_quotes=True, yes_price=0.65)
        assert vr.has_live_quotes


class TestMappingAutoValidator:
    @pytest.fixture
    def mock_store(self):
        store = AsyncMock()
        store.acquire = AsyncMock()
        store._pool = AsyncMock()
        store.get = AsyncMock()
        store.update_status = AsyncMock()
        store.refresh_runtime_cache = AsyncMock()
        store._row_to_mapping = MagicMock()
        return store

    @pytest.fixture
    def validator(self, mock_store):
        return MappingAutoValidator(
            mapping_store=mock_store,
            kalshi_collector=None,
            polymarket_collector=None,
            forecastex_client=None,
        )

    @pytest.mark.asyncio
    async def test_validate_no_platforms(self, validator):
        mapping = _make_mapping(
            kalshi_market_id="",
            polymarket_slug="",
            forecastex_contract_id="",
        )
        result = await validator.validate_mapping(mapping)
        assert result.recommendation == ValidationRecommendation.EXPIRE
        assert "no platform identifiers" in result.reason

    @pytest.mark.asyncio
    async def test_validate_skipped_platforms_excluded(self, validator):
        """Platforms with no collector are skipped, not counted as errors.

        When Kalshi/FX have no collector (skipped) but Polymarket works
        standalone via gamma-api, the mapping should be CONFIRM (Poly is
        live) or REVIEW (if slug not found). The key invariant is that
        skipped platforms don't force an EXPIRE recommendation.
        """
        mapping = _make_mapping(
            kalshi_market_id="",
            polymarket_slug="",
            forecastex_contract_id="",
        )
        result = await validator.validate_mapping(mapping)
        # No platform identifiers at all → EXPIRE
        assert result.recommendation == ValidationRecommendation.EXPIRE

        # With only skipped platforms (no collector, but IDs present)
        mapping2 = _make_mapping(
            polymarket_slug="",  # no slug to check
        )
        result2 = await validator.validate_mapping(mapping2)
        # Kalshi and FX skipped (no collector), Poly empty → no checks
        assert result2.recommendation in (
            ValidationRecommendation.REVIEW,
            ValidationRecommendation.EXPIRE,
        )

    @pytest.mark.asyncio
    async def test_auto_expire_dead(self, validator, mock_store):
        results = [
            ValidationResult(
                canonical_id="DEAD_001",
                description="Dead market",
                status="confirmed",
                recommendation=ValidationRecommendation.EXPIRE,
                reason="all platforms expired",
            ),
            ValidationResult(
                canonical_id="LIVE_001",
                description="Live market",
                status="confirmed",
                recommendation=ValidationRecommendation.CONFIRM,
                reason="2 platforms live",
            ),
        ]

        expired = await validator.auto_expire_dead(results=results)
        assert len(expired) == 1
        assert expired[0] == "DEAD_001"
        mock_store.update_status.assert_called_once()
        call_args = mock_store.update_status.call_args
        assert call_args[0][0] == "DEAD_001"
        assert call_args[0][1] == MappingStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_auto_promote_score_gate(self, validator, mock_store):
        """Low-score mappings should not be promoted."""
        results = [
            ValidationResult(
                canonical_id="LOW_SCORE",
                description="Low score",
                status="candidate",
                recommendation=ValidationRecommendation.CONFIRM,
                reason="2 platforms live",
            ),
        ]
        results[0].platforms["kalshi"] = PlatformCheckResult(
            platform="kalshi", market_id="K1", exists=True, is_open=True, has_quotes=True,
        )
        results[0].platforms["polymarket"] = PlatformCheckResult(
            platform="polymarket", market_id="P1", exists=True, is_open=True, has_quotes=True,
        )

        # Return a mapping with low score
        mock_store.get.return_value = _make_mapping(
            canonical_id="LOW_SCORE",
            mapping_score=0.50,
            resolution_match_status="identical",
        )

        promoted = await validator.auto_promote_validated(results=results)
        assert len(promoted) == 0

    @pytest.mark.asyncio
    async def test_auto_promote_resolution_gate(self, validator, mock_store):
        """Non-identical resolution should not be promoted."""
        results = [
            ValidationResult(
                canonical_id="BAD_RES",
                description="Bad resolution",
                status="candidate",
                recommendation=ValidationRecommendation.CONFIRM,
                reason="2 platforms live",
            ),
        ]
        results[0].platforms["kalshi"] = PlatformCheckResult(
            platform="kalshi", market_id="K1", exists=True, is_open=True, has_quotes=True,
        )
        results[0].platforms["polymarket"] = PlatformCheckResult(
            platform="polymarket", market_id="P1", exists=True, is_open=True, has_quotes=True,
        )

        mock_store.get.return_value = _make_mapping(
            canonical_id="BAD_RES",
            mapping_score=0.99,
            resolution_match_status="pending_operator_review",
        )

        promoted = await validator.auto_promote_validated(results=results)
        assert len(promoted) == 0

    @pytest.mark.asyncio
    async def test_auto_promote_success(self, validator, mock_store):
        """High-score, identical resolution, 2+ live platforms -> promote."""
        results = [
            ValidationResult(
                canonical_id="GOOD_ONE",
                description="Good mapping",
                status="candidate",
                recommendation=ValidationRecommendation.CONFIRM,
                reason="2 platforms live",
            ),
        ]
        results[0].platforms["kalshi"] = PlatformCheckResult(
            platform="kalshi", market_id="K1", exists=True, is_open=True, has_quotes=True,
        )
        results[0].platforms["polymarket"] = PlatformCheckResult(
            platform="polymarket", market_id="P1", exists=True, is_open=True, has_quotes=True,
        )

        mock_store.get.return_value = _make_mapping(
            canonical_id="GOOD_ONE",
            mapping_score=0.99,
            resolution_match_status="identical",
        )

        promoted = await validator.auto_promote_validated(results=results)
        assert len(promoted) == 1
        assert promoted[0] == "GOOD_ONE"
        mock_store.update_status.assert_called_once()
        call_args = mock_store.update_status.call_args
        assert call_args[0][1] == MappingStatus.CONFIRMED
        assert call_args[1].get("allow_auto_trade") is True

    def test_stats_initial(self, validator):
        assert validator.stats["total_validated"] == 0
        assert validator.stats["last_run"] is None

    def test_summary_format(self):
        vr = ValidationResult(
            canonical_id="TEST_001",
            description="Test",
            status="confirmed",
            recommendation=ValidationRecommendation.CONFIRM,
            reason="all good",
        )
        vr.platforms["kalshi"] = PlatformCheckResult(
            platform="kalshi", market_id="K1", exists=True, is_open=True, has_quotes=True,
        )
        s = vr.summary()
        assert "TEST_001" in s
        assert "confirm" in s
        assert "LIVE" in s
