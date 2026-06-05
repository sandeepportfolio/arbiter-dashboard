"""
Auto-discovery pipeline for cross-platform mapping.

Wraps the existing `discover()` function from auto_discovery.py and the
ForecastEx matcher into a unified periodic pipeline that:
1. Discovers new markets on all platforms
2. Matches cross-platform pairs
3. Validates matches against live APIs
4. Auto-promotes validated matches with score >= 0.95

Usage:
    pipeline = MappingAutoDiscovery(mapping_store, kalshi, polymarket, forecastex)
    summary = await pipeline.run_discovery_cycle()

Periodic:
    await pipeline.run_periodic(interval_seconds=7200)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .market_map import MarketMappingStore, MappingStatus

logger = logging.getLogger("arbiter.mapping.auto_discovery_pipeline")


@dataclass
class DiscoveryCycleResult:
    """Summary of a single discovery + validation cycle."""
    timestamp: str = ""
    kalshi_markets_found: int = 0
    polymarket_markets_found: int = 0
    forecastex_events_found: int = 0
    new_candidates_written: int = 0
    auto_promoted: int = 0
    auto_expired: int = 0
    forecastex_attached: int = 0
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "kalshi_markets_found": self.kalshi_markets_found,
            "polymarket_markets_found": self.polymarket_markets_found,
            "forecastex_events_found": self.forecastex_events_found,
            "new_candidates_written": self.new_candidates_written,
            "auto_promoted": self.auto_promoted,
            "auto_expired": self.auto_expired,
            "forecastex_attached": self.forecastex_attached,
            "errors": self.errors[:10],
            "duration_seconds": round(self.duration_seconds, 1),
        }


class MappingAutoDiscovery:
    """Unified auto-discovery pipeline across all platforms.

    Orchestrates:
    - Kalshi market discovery (via existing auto_discovery.discover())
    - ForecastEx event attachment (via forecastex_discovery)
    - Live-quote validation of new candidates
    - Auto-promotion of validated candidates
    """

    def __init__(
        self,
        mapping_store: MarketMappingStore,
        kalshi_collector=None,
        polymarket_collector=None,
        forecastex_client=None,
        *,
        auto_validator=None,
    ):
        self.store = mapping_store
        self.kalshi = kalshi_collector
        self.polymarket = polymarket_collector
        self.forecastex_client = forecastex_client
        self.auto_validator = auto_validator
        self._last_run: Optional[datetime] = None
        self._stats = {
            "total_cycles": 0,
            "total_candidates": 0,
            "total_promoted": 0,
            "total_expired": 0,
            "total_fx_attached": 0,
        }

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "last_run": self._last_run.isoformat() if self._last_run else None,
        }

    async def discover_new_kalshi_markets(self) -> int:
        """Fetch open Kalshi markets and find unmapped ones.

        Delegates to the existing auto_discovery.discover() which handles:
        - Full catalog fetch with pagination
        - Inverted-index matching against Polymarket
        - Structural fingerprint matching
        - Auto-promote gates when configured
        """
        if not self.kalshi:
            logger.warning("No Kalshi collector configured, skipping Kalshi discovery")
            return 0

        poly_client = getattr(self.polymarket, "client", self.polymarket)
        if not poly_client:
            logger.warning("No Polymarket collector configured, skipping K×P discovery")
            return 0

        try:
            from .auto_discovery import discover as discover_market_mappings
            from ..operator_settings import OperatorSettingsStore, load_market_discovery_settings

            settings_store = OperatorSettingsStore()
            runtime = load_market_discovery_settings(settings_store)

            if not runtime.get("auto_discovery_enabled", True):
                logger.info("Market discovery disabled by operator settings")
                return 0

            written = await discover_market_mappings(
                self.kalshi,
                poly_client,
                self.store,
                budget_rps=float(runtime.get("auto_discovery_budget_rps", 2.0)),
                min_score=float(runtime.get("auto_discovery_min_score", 0.25)),
                max_candidates=int(runtime.get("auto_discovery_max_candidates", 500)),
                promotion_settings=runtime,
            )
            logger.info("K×P discovery pass: wrote %d candidates", written)
            return written

        except Exception as e:
            logger.error("Kalshi-Polymarket discovery failed: %s", e, exc_info=True)
            return 0

    async def discover_forecastex_attachments(self) -> int:
        """Attach ForecastEx conids to confirmed mappings.

        Uses the existing forecastex_discovery module to walk confirmed
        K↔P mappings and find matching FORECASTX events.
        """
        if not self.forecastex_client:
            return 0

        try:
            from .forecastex_discovery import discover as discover_forecastex_mappings
            attached = await discover_forecastex_mappings(
                self.forecastex_client,
                self.store,
            )
            logger.info("ForecastEx attachment pass: attached %d conids", attached)
            return attached

        except Exception as e:
            logger.error("ForecastEx attachment failed: %s", e, exc_info=True)
            return 0

    async def validate_and_promote_candidates(self) -> tuple:
        """Validate candidates and promote those meeting threshold.

        Returns (promoted_count, expired_count).
        """
        if not self.auto_validator:
            logger.debug("No auto-validator configured, skipping validation")
            return 0, 0

        try:
            # Run validation on candidates and review mappings
            candidate_results = await self.auto_validator.validate_all(status="candidate")
            review_results = await self.auto_validator.validate_all(status="review")

            # Auto-promote qualified
            promoted = await self.auto_validator.auto_promote_validated(
                results=candidate_results + review_results,
            )

            # Also validate confirmed and expire dead
            confirmed_results = await self.auto_validator.validate_all(status="confirmed")
            expired = await self.auto_validator.auto_expire_dead(results=confirmed_results)

            # Stamp last_validated_at
            all_results = candidate_results + review_results + confirmed_results
            await self.auto_validator.update_last_validated(all_results)

            return len(promoted), len(expired)

        except Exception as e:
            logger.error("Validation/promotion failed: %s", e, exc_info=True)
            return 0, 0

    async def run_discovery_cycle(self) -> DiscoveryCycleResult:
        """Run a complete discovery + validation + promotion cycle.

        Steps:
        1. Discover new K×P market pairs
        2. Attach ForecastEx conids to confirmed mappings
        3. Validate candidates and auto-promote qualified ones
        4. Refresh runtime cache
        """
        result = DiscoveryCycleResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        t0 = time.monotonic()

        logger.info("=== AUTO-DISCOVERY CYCLE START ===")

        # Step 1: Discover new K×P pairs
        try:
            result.new_candidates_written = await self.discover_new_kalshi_markets()
        except Exception as e:
            result.errors.append(f"K×P discovery: {e}")

        # Step 2: Attach ForecastEx
        try:
            result.forecastex_attached = await self.discover_forecastex_attachments()
        except Exception as e:
            result.errors.append(f"FX attachment: {e}")

        # Step 3: Validate and promote
        try:
            promoted, expired = await self.validate_and_promote_candidates()
            result.auto_promoted = promoted
            result.auto_expired = expired
        except Exception as e:
            result.errors.append(f"Validation: {e}")

        # Step 4: Refresh runtime cache
        try:
            await self.store.refresh_runtime_cache()
            # Refresh tracked markets on collectors
            if self.kalshi and hasattr(self.kalshi, "refresh_tracked_markets"):
                self.kalshi.refresh_tracked_markets()
            poly = getattr(self.polymarket, "client", self.polymarket)
            if poly and hasattr(poly, "refresh_tracked_markets"):
                poly.refresh_tracked_markets()
        except Exception as e:
            result.errors.append(f"Cache refresh: {e}")

        result.duration_seconds = time.monotonic() - t0
        self._last_run = datetime.now(timezone.utc)
        self._stats["total_cycles"] += 1
        self._stats["total_candidates"] += result.new_candidates_written
        self._stats["total_promoted"] += result.auto_promoted
        self._stats["total_expired"] += result.auto_expired
        self._stats["total_fx_attached"] += result.forecastex_attached

        logger.info(
            "=== AUTO-DISCOVERY CYCLE DONE in %.1fs: "
            "candidates=%d, promoted=%d, expired=%d, fx_attached=%d, errors=%d ===",
            result.duration_seconds,
            result.new_candidates_written,
            result.auto_promoted,
            result.auto_expired,
            result.forecastex_attached,
            len(result.errors),
        )

        return result

    async def run_periodic(
        self,
        interval_seconds: float = 7200,
        notifier=None,
    ) -> None:
        """Run discovery cycles on a timer.

        Args:
            interval_seconds: How often to run (default 2 hours).
            notifier: Optional Telegram notifier for sending summaries.
        """
        logger.info(
            "Auto-discovery periodic loop starting (interval=%ds)",
            interval_seconds,
        )

        # Initial delay to let collectors warm up
        await asyncio.sleep(60)

        while True:
            try:
                result = await self.run_discovery_cycle()

                # Send notification if anything happened
                if notifier and (
                    result.new_candidates_written > 0
                    or result.auto_promoted > 0
                    or result.auto_expired > 0
                    or result.forecastex_attached > 0
                ):
                    msg = (
                        f"🔎 Auto-discovery cycle complete ({result.duration_seconds:.0f}s)\n"
                        f"  New candidates: {result.new_candidates_written}\n"
                        f"  Auto-promoted: {result.auto_promoted}\n"
                        f"  Auto-expired: {result.auto_expired}\n"
                        f"  FX attached: {result.forecastex_attached}\n"
                    )
                    if result.errors:
                        msg += f"  Errors: {len(result.errors)}\n"
                    try:
                        await notifier.send(msg)
                    except Exception as e:
                        logger.warning("Failed to send discovery notification: %s", e)

            except asyncio.CancelledError:
                logger.info("Auto-discovery loop cancelled")
                return
            except Exception as e:
                logger.error("Auto-discovery cycle failed: %s", e, exc_info=True)

            await asyncio.sleep(interval_seconds)
