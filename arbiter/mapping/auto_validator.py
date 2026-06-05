"""
Self-validating mapping pipeline.

Continuously validates confirmed mappings against live platform APIs
(Kalshi, Polymarket, ForecastEx) and auto-expires dead markets, auto-promotes
validated candidates with live quotes on 2+ platforms.

Usage:
    validator = MappingAutoValidator(mapping_store, kalshi, polymarket, forecastex)
    results = await validator.validate_all()
    expired = await validator.auto_expire_dead()
    promoted = await validator.auto_promote_validated()

Periodic:
    await validator.run_periodic(interval_seconds=1800)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .market_map import MarketMapping, MarketMappingStore, MappingStatus

logger = logging.getLogger("arbiter.mapping.auto_validator")


# ── Rate-limit budgets (env-tunable) ──────────────────────────────

KALSHI_RPS = float(os.getenv("VALIDATOR_KALSHI_RPS", "8"))
POLYMARKET_RPS = float(os.getenv("VALIDATOR_POLYMARKET_RPS", "4"))
FORECASTEX_RPS = float(os.getenv("VALIDATOR_FORECASTEX_RPS", "2"))

# Minimum score + live platforms required for auto-confirmation
AUTO_CONFIRM_MIN_SCORE = float(os.getenv("AUTO_CONFIRM_MIN_SCORE", "0.95"))
AUTO_CONFIRM_MIN_LIVE_PLATFORMS = int(os.getenv("AUTO_CONFIRM_MIN_LIVE_PLATFORMS", "2"))


class ValidationRecommendation(str, Enum):
    CONFIRM = "confirm"      # All checks pass, keep confirmed
    REVIEW = "review"        # Some checks failed, needs operator look
    EXPIRE = "expire"        # Market is dead/settled/closed
    PROMOTE = "promote"      # Candidate should be promoted to confirmed


@dataclass
class PlatformCheckResult:
    """Result of checking a single platform for a mapping."""
    platform: str
    market_id: str
    exists: bool = False
    is_open: bool = False
    is_expired: bool = False
    has_quotes: bool = False
    yes_price: float = 0.0
    no_price: float = 0.0
    volume: float = 0.0
    last_trade_time: Optional[datetime] = None
    error: str = ""
    check_time: float = 0.0  # seconds taken

    @property
    def is_live(self) -> bool:
        return self.exists and self.is_open and not self.is_expired


@dataclass
class ValidationResult:
    """Result of validating a single mapping against all its platforms."""
    canonical_id: str
    description: str
    status: str
    platforms: Dict[str, PlatformCheckResult] = field(default_factory=dict)
    recommendation: ValidationRecommendation = ValidationRecommendation.REVIEW
    reason: str = ""
    validated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def live_platform_count(self) -> int:
        return sum(1 for p in self.platforms.values() if p.is_live)

    @property
    def has_live_quotes(self) -> bool:
        return any(p.has_quotes for p in self.platforms.values())

    @property
    def all_expired(self) -> bool:
        if not self.platforms:
            return False
        return all(p.is_expired or not p.exists for p in self.platforms.values())

    def summary(self) -> str:
        parts = [f"{self.canonical_id}: {self.recommendation.value}"]
        for name, check in self.platforms.items():
            status = "LIVE" if check.is_live else ("EXPIRED" if check.is_expired else "DEAD")
            parts.append(f"  {name}: {status} (exists={check.exists}, quotes={check.has_quotes})")
        if self.reason:
            parts.append(f"  reason: {self.reason}")
        return "\n".join(parts)


class MappingAutoValidator:
    """Continuously validates mappings against live platform APIs."""

    def __init__(
        self,
        mapping_store: MarketMappingStore,
        kalshi_collector=None,
        polymarket_collector=None,
        forecastex_client=None,
    ):
        self.store = mapping_store
        self.kalshi = kalshi_collector
        self.polymarket = polymarket_collector
        self.forecastex = forecastex_client
        self._last_run: Optional[datetime] = None
        self._stats = {
            "total_validated": 0,
            "live": 0,
            "expired": 0,
            "dead": 0,
            "errors": 0,
            "auto_expired": 0,
            "auto_promoted": 0,
        }

    @property
    def stats(self) -> Dict[str, Any]:
        return {**self._stats, "last_run": self._last_run.isoformat() if self._last_run else None}

    # ── Platform checks ──────────────────────────────────────────

    async def _check_kalshi(self, ticker: str) -> PlatformCheckResult:
        """Check if a Kalshi market exists and is live."""
        result = PlatformCheckResult(platform="kalshi", market_id=ticker)
        if not self.kalshi or not ticker:
            result.error = "no collector or empty ticker"
            return result

        t0 = time.monotonic()
        try:
            # Use the bulk fetch endpoint to check a single market
            session = await self.kalshi._get_session()
            await self.kalshi.rate_limiter.acquire()

            headers = {"Accept": "application/json"}
            if self.kalshi.auth.is_authenticated:
                headers.update(self.kalshi.auth.get_headers("GET", "/trade-api/v2/markets"))

            async with session.get(
                f"{self.kalshi.config.base_url}/markets",
                params={"tickers": ticker, "limit": "1"},
                headers=headers,
            ) as resp:
                if resp.status == 404:
                    result.exists = False
                    return result
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "5")
                    await asyncio.sleep(float(retry_after))
                    result.error = "rate_limited"
                    return result
                resp.raise_for_status()
                data = await resp.json()

            markets = data.get("markets", [])
            if not markets:
                result.exists = False
                return result

            market = markets[0]
            result.exists = True
            status = str(market.get("status", "")).lower()
            result.is_open = status in ("open", "active")
            result.is_expired = status in ("closed", "settled", "finalized", "ceased_trading")
            # Kalshi price detection: check all price fields (yes_ask, no_ask,
            # yes_bid, no_bid, last_price). A market with status=open IS live
            # even when the orderbook is empty (spread markets with no resting
            # orders still accept trades).
            yes_ask = float(market.get("yes_ask", 0) or 0)
            no_ask = float(market.get("no_ask", 0) or 0)
            yes_bid = float(market.get("yes_bid", 0) or 0)
            no_bid = float(market.get("no_bid", 0) or 0)
            last_price = float(market.get("last_price", 0) or 0)
            result.yes_price = yes_ask or last_price
            result.no_price = no_ask
            result.has_quotes = (
                yes_ask > 0 or no_ask > 0 or yes_bid > 0
                or no_bid > 0 or last_price > 0
            )
            # For Kalshi, status=open itself is sufficient proof of liveness
            # even if orderbook is currently empty (thin market).
            if result.is_open and not result.has_quotes:
                result.has_quotes = True
            result.volume = float(market.get("volume", 0) or 0)

        except Exception as e:
            result.error = str(e)[:200]
            logger.debug("Kalshi check failed for %s: %s", ticker, e)
        finally:
            result.check_time = time.monotonic() - t0

        return result

    async def _check_polymarket(self, slug: str) -> PlatformCheckResult:
        """Check if a Polymarket condition is live.

        Works both WITH a collector (reuses its session/rate-limiter) and
        WITHOUT one (creates an ad-hoc aiohttp session against the public
        gamma-api). This allows standalone scripts to validate Polymarket
        slugs without bootstrapping the full collector stack.
        """
        result = PlatformCheckResult(platform="polymarket", market_id=slug)
        if not slug:
            result.error = "empty slug"
            return result

        t0 = time.monotonic()
        try:
            import aiohttp

            # Try to reuse the collector's session if available
            session = None
            _created_session = False
            if self.polymarket:
                client = getattr(self.polymarket, "client", self.polymarket)
                if hasattr(client, "rate_limiter"):
                    await client.rate_limiter.acquire()
                if hasattr(client, "_session") and client._session and not client._session.closed:
                    session = client._session
                elif hasattr(client, "_get_session"):
                    session = await client._get_session()

            if session is None:
                session = aiohttp.ClientSession()
                _created_session = True
                # Standalone rate-limit: 250ms between calls
                await asyncio.sleep(0.25)

            try:
                # Try gamma-api for market data
                gamma_url = "https://gamma-api.polymarket.com/markets"
                async with session.get(
                    gamma_url,
                    params={"slug": slug, "limit": "1"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 404:
                        result.exists = False
                        return result
                    if resp.status == 429:
                        result.error = "rate_limited"
                        return result
                    if resp.status >= 400:
                        result.error = f"HTTP {resp.status}"
                        return result
                    data = await resp.json()

                if not data:
                    # Try condition_id-based lookup
                    async with session.get(
                        gamma_url,
                        params={"condition_id": slug, "limit": "1"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()

                if not data:
                    result.exists = False
                    return result

                market = data[0] if isinstance(data, list) else data
                result.exists = True
                result.is_open = bool(market.get("active", False)) and not bool(market.get("closed", True))
                result.is_expired = bool(market.get("closed", False)) or bool(market.get("archived", False))
                result.has_quotes = bool(
                    market.get("bestAsk") or market.get("bestBid")
                    or market.get("outcomePrices")
                )
                # Parse prices
                outcome_prices = market.get("outcomePrices")
                if outcome_prices:
                    try:
                        import json as _json
                        if isinstance(outcome_prices, str):
                            prices = _json.loads(outcome_prices)
                        else:
                            prices = outcome_prices
                        if isinstance(prices, list) and len(prices) >= 1:
                            result.yes_price = float(prices[0])
                            if len(prices) >= 2:
                                result.no_price = float(prices[1])
                    except (ValueError, TypeError, IndexError):
                        pass
                result.volume = float(market.get("volume", 0) or 0)

            finally:
                if _created_session:
                    await session.close()

        except Exception as e:
            result.error = str(e)[:200]
            logger.debug("Polymarket check failed for %s: %s", slug, e)
        finally:
            result.check_time = time.monotonic() - t0

        return result

    async def _check_forecastex(self, conid: str) -> PlatformCheckResult:
        """Check if a ForecastEx contract is live via IBKR snapshot."""
        result = PlatformCheckResult(platform="forecastex", market_id=conid)
        if not self.forecastex or not conid:
            result.error = "no client or empty conid"
            return result

        t0 = time.monotonic()
        try:
            # Use the ForecastEx client's snapshot endpoint
            if hasattr(self.forecastex, "rate_limiter"):
                await self.forecastex.rate_limiter.acquire()
            elif hasattr(self.forecastex, "live_rate_limiter"):
                await self.forecastex.live_rate_limiter.acquire()

            # IBKR market data snapshot
            snapshot = await self.forecastex.market_snapshot(conid)
            if snapshot is None:
                result.exists = False
                return result

            result.exists = True
            # Parse snapshot response
            if isinstance(snapshot, list) and snapshot:
                snap = snapshot[0]
            elif isinstance(snapshot, dict):
                snap = snapshot
            else:
                result.exists = False
                return result

            # IBKR returns various field codes
            bid = float(snap.get("84", 0) or snap.get("bid", 0) or 0)
            ask = float(snap.get("86", 0) or snap.get("ask", 0) or 0)
            last = float(snap.get("31", 0) or snap.get("last", 0) or 0)

            result.yes_price = ask if ask > 0 else last
            result.no_price = bid
            result.has_quotes = bid > 0 or ask > 0 or last > 0
            result.is_open = result.has_quotes
            result.is_expired = not result.is_open and not result.has_quotes

        except Exception as e:
            result.error = str(e)[:200]
            logger.debug("ForecastEx check failed for %s: %s", conid, e)
        finally:
            result.check_time = time.monotonic() - t0

        return result

    # ── Core validation ──────────────────────────────────────────

    async def validate_mapping(self, mapping: MarketMapping) -> ValidationResult:
        """Check a single mapping against all its platforms."""
        result = ValidationResult(
            canonical_id=mapping.canonical_id,
            description=mapping.description,
            status=mapping.status.value if isinstance(mapping.status, Enum) else str(mapping.status),
        )

        # Build check tasks
        checks: List[Tuple[str, Any]] = []
        if mapping.kalshi_market_id:
            checks.append(("kalshi", self._check_kalshi(mapping.kalshi_market_id)))
        if mapping.polymarket_slug:
            checks.append(("polymarket", self._check_polymarket(mapping.polymarket_slug)))
        if mapping.forecastex_contract_id:
            checks.append(("forecastex", self._check_forecastex(mapping.forecastex_contract_id)))

        if not checks:
            result.recommendation = ValidationRecommendation.EXPIRE
            result.reason = "no platform identifiers"
            return result

        # Run checks concurrently
        tasks = [asyncio.create_task(coro) for _, coro in checks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (name, _), check_result in zip(checks, results):
            if isinstance(check_result, Exception):
                result.platforms[name] = PlatformCheckResult(
                    platform=name,
                    market_id="",
                    error=str(check_result)[:200],
                )
            else:
                result.platforms[name] = check_result

        # Determine recommendation.
        # Separate actually-checked platforms from those skipped due to
        # missing collector/empty ID ("no collector or empty ticker" etc.).
        # Skipped checks are NOT failures — a K-only mapping with no Poly
        # slug should be judged solely on its Kalshi check.
        checked = {
            name: p for name, p in result.platforms.items()
            if not p.error or not p.error.startswith("no ")
        }
        skipped = {
            name: p for name, p in result.platforms.items()
            if p.error and p.error.startswith("no ")
        }

        live_count = sum(1 for p in checked.values() if p.is_live)
        expired_count = sum(1 for p in checked.values() if p.is_expired or not p.exists)
        error_count = sum(1 for p in checked.values() if p.error)
        checked_count = len(checked)

        if checked_count == 0:
            # Nothing was actually checked (all skipped / no collectors)
            # Don't expire — can't confirm death without evidence
            result.recommendation = ValidationRecommendation.REVIEW
            result.reason = f"no platforms could be checked ({len(skipped)} skipped)"
        elif live_count > 0:
            result.recommendation = ValidationRecommendation.CONFIRM
            result.reason = f"{live_count}/{checked_count} platform(s) live"
        elif checked_count > 0 and error_count == checked_count:
            # All checked platforms errored (rate-limited, timeout, etc.)
            result.recommendation = ValidationRecommendation.REVIEW
            result.reason = f"all {error_count} checked platform(s) returned errors"
        elif expired_count == checked_count:
            result.recommendation = ValidationRecommendation.EXPIRE
            result.reason = f"all {checked_count} checked platform(s) expired/settled"
        else:
            result.recommendation = ValidationRecommendation.EXPIRE
            result.reason = f"no live platforms found ({checked_count} checked, {expired_count} expired)"

        return result

    async def validate_all(
        self,
        status: str = "confirmed",
        *,
        batch_size: int = 20,
        inter_batch_delay: float = 2.0,
    ) -> List[ValidationResult]:
        """Validate all mappings with a given status.

        Processes in batches to respect rate limits.
        """
        logger.info("Starting validation of all '%s' mappings", status)

        # Fetch all mappings with this status
        conn = await self.store.acquire()
        try:
            rows = await conn.fetch(
                "SELECT * FROM market_mappings WHERE status = $1 ORDER BY updated_at DESC",
                status,
            )
        finally:
            await self.store._pool.release(conn)

        mappings = [self.store._row_to_mapping(r) for r in rows]
        logger.info("Found %d '%s' mappings to validate", len(mappings), status)

        results: List[ValidationResult] = []
        for i in range(0, len(mappings), batch_size):
            batch = mappings[i : i + batch_size]
            batch_results = []
            for mapping in batch:
                try:
                    vr = await self.validate_mapping(mapping)
                    batch_results.append(vr)
                except Exception as e:
                    logger.warning("Validation failed for %s: %s", mapping.canonical_id, e)
                    self._stats["errors"] += 1

            results.extend(batch_results)
            logger.info(
                "Validated batch %d/%d (%d mappings)",
                i // batch_size + 1,
                (len(mappings) + batch_size - 1) // batch_size,
                len(batch),
            )

            # Rate-limit between batches
            if i + batch_size < len(mappings):
                await asyncio.sleep(inter_batch_delay)

        # Update stats
        self._stats["total_validated"] = len(results)
        self._stats["live"] = sum(1 for r in results if r.recommendation == ValidationRecommendation.CONFIRM)
        self._stats["expired"] = sum(1 for r in results if r.recommendation == ValidationRecommendation.EXPIRE)
        self._stats["dead"] = sum(1 for r in results if r.recommendation == ValidationRecommendation.REVIEW)
        self._last_run = datetime.now(timezone.utc)

        logger.info(
            "Validation complete: %d total, %d live, %d expired, %d review, %d errors",
            len(results),
            self._stats["live"],
            self._stats["expired"],
            self._stats["dead"],
            self._stats["errors"],
        )

        return results

    # ── Auto-actions ─────────────────────────────────────────────

    async def auto_expire_dead(
        self,
        results: Optional[List[ValidationResult]] = None,
    ) -> List[str]:
        """Move expired/settled mappings to expired status.

        Only expires mappings where the recommendation is EXPIRE and all
        platform checks confirm the market is gone. Logs every decision
        for audit trail.
        """
        if results is None:
            results = await self.validate_all(status="confirmed")

        expired_ids: List[str] = []
        for vr in results:
            if vr.recommendation != ValidationRecommendation.EXPIRE:
                continue

            try:
                note = f"[auto-validator {vr.validated_at.isoformat()[:19]}] " \
                       f"expired: {vr.reason}"
                await self.store.update_status(
                    vr.canonical_id,
                    MappingStatus.EXPIRED,
                    review_note=note,
                    allow_auto_trade=False,
                )
                expired_ids.append(vr.canonical_id)
                logger.info(
                    "AUTO-EXPIRE %s: %s",
                    vr.canonical_id,
                    vr.reason,
                )
            except Exception as e:
                logger.warning("Failed to expire %s: %s", vr.canonical_id, e)

        self._stats["auto_expired"] += len(expired_ids)
        if expired_ids:
            await self.store.refresh_runtime_cache()
            logger.info("Auto-expired %d mappings", len(expired_ids))
        return expired_ids

    async def auto_promote_validated(
        self,
        results: Optional[List[ValidationResult]] = None,
    ) -> List[str]:
        """Promote candidate/review mappings that pass all validation checks.

        Only auto-confirms mappings with:
        - mapping_score >= AUTO_CONFIRM_MIN_SCORE (default 0.95)
        - Live quotes on >= AUTO_CONFIRM_MIN_LIVE_PLATFORMS (default 2)
        - resolution_match_status == 'identical'

        Logs every promotion decision for audit trail.
        """
        if results is None:
            results_candidates = await self.validate_all(status="candidate")
            results_review = await self.validate_all(status="review")
            results = results_candidates + results_review

        promoted_ids: List[str] = []
        for vr in results:
            if vr.recommendation not in (
                ValidationRecommendation.CONFIRM,
                ValidationRecommendation.PROMOTE,
            ):
                continue

            # Only promote if live on 2+ platforms
            if vr.live_platform_count < AUTO_CONFIRM_MIN_LIVE_PLATFORMS:
                continue

            # Fetch the mapping to check score and resolution
            mapping = await self.store.get(vr.canonical_id)
            if mapping is None:
                continue

            # Score gate
            if mapping.mapping_score < AUTO_CONFIRM_MIN_SCORE:
                logger.debug(
                    "Skipping promotion of %s: score %.4f < %.4f",
                    vr.canonical_id,
                    mapping.mapping_score,
                    AUTO_CONFIRM_MIN_SCORE,
                )
                continue

            # Resolution match gate
            if mapping.resolution_match_status != "identical":
                logger.debug(
                    "Skipping promotion of %s: resolution_match_status=%s",
                    vr.canonical_id,
                    mapping.resolution_match_status,
                )
                continue

            try:
                note = (
                    f"[auto-validator {vr.validated_at.isoformat()[:19]}] "
                    f"promoted: {vr.live_platform_count} platforms live, "
                    f"score={mapping.mapping_score:.4f}, "
                    f"resolution={mapping.resolution_match_status}"
                )
                await self.store.update_status(
                    vr.canonical_id,
                    MappingStatus.CONFIRMED,
                    review_note=note,
                    allow_auto_trade=True,
                )
                promoted_ids.append(vr.canonical_id)
                logger.info(
                    "AUTO-PROMOTE %s: %d platforms live, score=%.4f",
                    vr.canonical_id,
                    vr.live_platform_count,
                    mapping.mapping_score,
                )
            except Exception as e:
                logger.warning("Failed to promote %s: %s", vr.canonical_id, e)

        self._stats["auto_promoted"] += len(promoted_ids)
        if promoted_ids:
            await self.store.refresh_runtime_cache()
            logger.info("Auto-promoted %d mappings", len(promoted_ids))
        return promoted_ids

    async def update_last_validated(
        self,
        results: List[ValidationResult],
    ) -> int:
        """Stamp last_validated_at on all validated mappings."""
        conn = await self.store.acquire()
        count = 0
        try:
            for vr in results:
                try:
                    await conn.execute(
                        "UPDATE market_mappings SET last_validated_at = $2 WHERE canonical_id = $1",
                        vr.canonical_id,
                        vr.validated_at,
                    )
                    count += 1
                except Exception as e:
                    logger.debug("Failed to stamp %s: %s", vr.canonical_id, e)
        finally:
            await self.store._pool.release(conn)
        return count

    # ── Full cycle ───────────────────────────────────────────────

    async def run_full_cycle(self) -> Dict[str, Any]:
        """Run a complete validation + expire + promote cycle.

        Returns a summary dict suitable for Telegram/logging.
        """
        logger.info("=== AUTO-VALIDATOR FULL CYCLE START ===")

        # 1. Validate confirmed
        confirmed_results = await self.validate_all(status="confirmed")

        # 2. Auto-expire dead confirmed
        expired_ids = await self.auto_expire_dead(results=confirmed_results)

        # 3. Validate candidates + review for promotion
        candidate_results = await self.validate_all(status="candidate")
        review_results = await self.validate_all(status="review")

        # 4. Auto-promote qualified
        promoted_ids = await self.auto_promote_validated(
            results=candidate_results + review_results,
        )

        # 5. Stamp last_validated_at
        all_results = confirmed_results + candidate_results + review_results
        stamped = await self.update_last_validated(all_results)

        summary = {
            "confirmed_checked": len(confirmed_results),
            "confirmed_live": sum(
                1 for r in confirmed_results
                if r.recommendation == ValidationRecommendation.CONFIRM
            ),
            "confirmed_expired": len(expired_ids),
            "candidates_checked": len(candidate_results),
            "review_checked": len(review_results),
            "promoted": len(promoted_ids),
            "promoted_ids": promoted_ids[:20],
            "expired_ids": expired_ids[:20],
            "stamped": stamped,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "=== AUTO-VALIDATOR FULL CYCLE DONE: "
            "confirmed=%d (live=%d, expired=%d), "
            "candidates=%d, review=%d, promoted=%d ===",
            summary["confirmed_checked"],
            summary["confirmed_live"],
            summary["confirmed_expired"],
            summary["candidates_checked"],
            summary["review_checked"],
            summary["promoted"],
        )

        return summary

    # ── Periodic runner ──────────────────────────────────────────

    async def run_periodic(
        self,
        interval_seconds: float = 1800,
        notifier=None,
    ) -> None:
        """Run validation cycles on a timer.

        Args:
            interval_seconds: How often to run (default 30 min).
            notifier: Optional Telegram notifier for sending summaries.
        """
        logger.info(
            "Auto-validator periodic loop starting (interval=%ds)",
            interval_seconds,
        )

        # Small initial delay to let collectors start
        await asyncio.sleep(30)

        while True:
            try:
                summary = await self.run_full_cycle()

                # Send notification if anything changed
                if notifier and (summary["confirmed_expired"] > 0 or summary["promoted"] > 0):
                    msg = (
                        f"🔍 Auto-validator cycle complete\n"
                        f"  Confirmed: {summary['confirmed_checked']} checked, "
                        f"{summary['confirmed_live']} live\n"
                        f"  Expired: {summary['confirmed_expired']} mappings\n"
                        f"  Promoted: {summary['promoted']} mappings\n"
                    )
                    if summary["expired_ids"]:
                        msg += f"  Expired IDs: {', '.join(summary['expired_ids'][:5])}\n"
                    if summary["promoted_ids"]:
                        msg += f"  Promoted IDs: {', '.join(summary['promoted_ids'][:5])}\n"

                    try:
                        await notifier.send(msg)
                    except Exception as e:
                        logger.warning("Failed to send validator notification: %s", e)

            except asyncio.CancelledError:
                logger.info("Auto-validator loop cancelled")
                return
            except Exception as e:
                logger.error("Auto-validator cycle failed: %s", e, exc_info=True)

            await asyncio.sleep(interval_seconds)
