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

from . import coherence
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
    # ForecastEx contract local_symbol (e.g. SENM_1126_Democratic_YES),
    # fetched best-effort so promotion can verify party/semantics against
    # the canonical (2026-07-10 Senate party-swap incident).
    contract_symbol: str = ""

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
        price_store=None,
    ):
        self.store = mapping_store
        self.kalshi = kalshi_collector
        self.polymarket = polymarket_collector
        self.forecastex = forecastex_client
        # Live-quote truth for the quarantine-release sweep: platform
        # checks probe PUBLIC venue APIs and miss PM-US markets that only
        # quote on the authenticated gateway; the price store carries the
        # quotes the scanner actually trades on.
        self.price_store = price_store
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

            # Best-effort identity fetch: the local_symbol carries the
            # party/side (SENM_1126_Democratic_YES). Promotion fails closed
            # without it on party-tokened canonicals.
            info_getter = getattr(self.forecastex, "get_contract_info", None)
            if callable(info_getter):
                try:
                    info = await info_getter(conid)
                    if isinstance(info, dict):
                        result.contract_symbol = str(
                            info.get("local_symbol")
                            or info.get("localSymbol")
                            or ""
                        )
                except Exception as info_exc:  # noqa: BLE001
                    logger.debug(
                        "ForecastEx contract-info fetch failed for %s: %s",
                        conid, info_exc,
                    )

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

    _STREAK_RE = None  # compiled lazily in sweep_quarantine_releases

    async def sweep_quarantine_releases(
        self,
        results: List["ValidationResult"],
        llm_verify=None,
    ) -> List[str]:
        """Automated release of the ``[no-auto-promote]`` quarantine marker.

        Operator directive 2026-07-31: mapping review must be a fully
        automatic process with two independent LLM reviewers. The marker
        exists because this validator once re-promoted the party-swapped
        Senate mapping minutes after an operator demoted it — so release
        is deliberately the HARDEST gate in the pipeline:

          1. env-gated (QUARANTINE_AUTO_RELEASE_ENABLED, default off);
          2. resolution_match_status must be 'identical';
          3. >=2 live venues whose YES prices agree within the coherence
             divergence gate (cheap checks first — no LLM spend otherwise);
          4. the dual-reviewer LLM service must return consensus YES for
             the venue-id pair (claude-opus-5 AND claude-sonnet-5 via the
             verifier sidecar — either reviewer's NO vetoes);
          5. all of the above sustained for QUARANTINE_RELEASE_CYCLES
             consecutive validation cycles (default 3 = ~90 min), tracked
             by a ``[quarantine-release-streak N/M]`` note tag that any
             failed gate resets.

        Release only strips the marker and leaves an audit tag — the
        mapping then faces every normal promotion gate (score, polarity,
        duplicate-canonical, divergence, LLM again, liquidity) before it
        can trade. Returns the canonical_ids released this cycle.
        """
        import re as _re

        if str(os.getenv("QUARANTINE_AUTO_RELEASE_ENABLED", "")).strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return []
        try:
            needed = max(1, int(os.getenv("QUARANTINE_RELEASE_CYCLES", "3") or "3"))
        except (TypeError, ValueError):
            needed = 3
        if llm_verify is None:
            from . import llm_verifier as _lv

            llm_verify = _lv.verify

        streak_re = _re.compile(r"\[quarantine-release-streak (\d+)/\d+\]")
        released: List[str] = []

        # Duplicate-canonical pre-check: a held mapping whose Kalshi market
        # or Polymarket slug is already owned by a confirmed mapping can
        # NEVER be promoted — releasing it just ping-pongs with the
        # promotion gate (release → refuse → re-quarantine, observed
        # 2026-08-01 on GOP_HOUSE_2026 at critical log level every cycle).
        owner_counts: Dict[str, int] = {}
        _ogetter = getattr(self.store, "market_owner_counts", None)
        if callable(_ogetter):
            try:
                owner_counts = dict(await _ogetter() or {})
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "quarantine-release owner_counts failed: %s", exc,
                )

        for vr in results:
            try:
                mapping = await self.store.get(vr.canonical_id)
            except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
                continue
            if mapping is None:
                continue
            note = mapping.review_note or ""
            held_note = f"{note} {mapping.notes or ''}"
            if "[no-auto-promote]" not in held_note:
                continue

            def _strip_streak(text: str) -> str:
                return streak_re.sub("", text).strip()

            async def _reset(reason: str) -> None:
                if streak_re.search(note):
                    await self.store.update_status(
                        vr.canonical_id,
                        mapping.status,
                        review_note=_strip_streak(note),
                        allow_auto_trade=None,
                    )
                logger.info(
                    "quarantine-release gate failed for %s: %s (streak reset)",
                    vr.canonical_id, reason,
                )

            # Cheap gates first — never spend LLM calls on a pair that
            # already fails structurally.
            if str(mapping.resolution_match_status or "") != "identical":
                # Non-identical resolution is a hard no: don't even track
                # a streak, and don't touch the row.
                continue

            if owner_counts and coherence.shared_market_owner_conflict(
                getattr(mapping, "kalshi_market_id", ""),
                getattr(mapping, "polymarket_slug", ""),
                owner_counts,
            ):
                # A confirmed mapping already owns this market — this row is
                # a duplicate and can never promote. Never release it, and
                # drop any streak so it stops cycling.
                if streak_re.search(note):
                    await self.store.update_status(
                        vr.canonical_id, mapping.status,
                        review_note=_strip_streak(note), allow_auto_trade=None,
                    )
                continue

            live_yes: Dict[str, float] = {}
            if self.price_store is not None:
                try:
                    quotes = await self.price_store.get_all_for_market(
                        vr.canonical_id
                    ) or {}
                    live_yes = {
                        plat: float(pp.yes_price)
                        for plat, pp in quotes.items()
                        if (getattr(pp, "yes_price", 0) or 0) > 0
                        and float(getattr(pp, "age_seconds", 1e9)) <= 120.0
                    }
                except Exception as exc:  # noqa: BLE001 — fall back below
                    logger.debug(
                        "quarantine-release price_store read failed for %s: %s",
                        vr.canonical_id, exc,
                    )
            if len(live_yes) < 2:
                live_yes = {
                    name: check.yes_price
                    for name, check in vr.platforms.items()
                    if getattr(check, "is_open", False)
                    and getattr(check, "has_quotes", False)
                    and (check.yes_price or 0) > 0
                }
            if len(live_yes) < 2:
                await _reset("fewer than 2 live quoting venues")
                continue
            divergence, _pair = coherence.max_yes_divergence(live_yes)
            if divergence > coherence.DEFAULT_MAX_YES_DIVERGENCE:
                await _reset(f"divergence {divergence:.4f} above gate")
                continue

            # Party-identity gate (review finding 2026-08-03): a canonical
            # carrying a party token with an FX leg must prove the FX
            # contract symbol names the SAME party — the sweep previously
            # relied on the LLM alone for the exact failure class the
            # party_conflict check was built for. Fail closed on a missing
            # symbol, exactly like the promotion gate.
            canonical_text = " ".join(
                [vr.canonical_id, getattr(mapping, "description", "") or ""]
                + [str(a) for a in (getattr(mapping, "aliases", ()) or ())]
            )
            if coherence.canonical_party(canonical_text) and getattr(
                mapping, "forecastex_contract_id", ""
            ):
                fx_check = vr.platforms.get("forecastex")
                symbol = getattr(fx_check, "contract_symbol", "") or ""
                if not symbol:
                    await _reset(
                        "party-tokened canonical with FX leg but no contract "
                        "symbol — cannot prove party match"
                    )
                    continue
                conflict = coherence.party_conflict(canonical_text, symbol)
                if conflict:
                    await _reset(f"party conflict: {conflict}")
                    continue

            # Dual-reviewer LLM consensus on the venue-id pair. The ids
            # carry the party/side tokens, so a party-swapped mapping
            # presents mismatched ids and draws a NO.
            kalshi_q = (
                f"{mapping.description} — kalshi market id: "
                f"{mapping.kalshi_market_id or 'n/a'}"
            )
            poly_q = (
                f"polymarket market id: {mapping.polymarket_slug or 'n/a'}"
                + (
                    f" / forecastex contract: {mapping.forecastex_contract_id}"
                    if getattr(mapping, "forecastex_contract_id", "")
                    else ""
                )
            )
            try:
                verdict = await llm_verify(kalshi_q, poly_q)
            except Exception as exc:  # noqa: BLE001 — fail closed
                verdict = "MAYBE"
                logger.warning(
                    "quarantine-release LLM verify failed for %s: %s",
                    vr.canonical_id, exc,
                )
            if verdict != "YES":
                await _reset(f"dual-LLM verdict {verdict}")
                continue

            match = streak_re.search(note)
            streak = int(match.group(1)) if match else 0
            streak += 1
            if streak < needed:
                new_note = (
                    f"{_strip_streak(note)} "
                    f"[quarantine-release-streak {streak}/{needed}]"
                ).strip()
                await self.store.update_status(
                    vr.canonical_id, mapping.status,
                    review_note=new_note, allow_auto_trade=None,
                )
                logger.info(
                    "quarantine-release streak %d/%d for %s "
                    "(dual-LLM YES, divergence %.4f)",
                    streak, needed, vr.canonical_id, divergence,
                )
                continue

            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            cleaned = _strip_streak(
                note.replace("[no-auto-promote]", "")
                .replace("[coherence-quarantine]", "")
            ).strip()
            new_note = (
                f"{cleaned} [quarantine-released {stamp} "
                f"dual-llm-yes divergence={divergence:.4f} "
                f"streak={needed}]"
            ).strip()
            await self.store.update_status(
                vr.canonical_id, mapping.status,
                review_note=new_note, allow_auto_trade=None,
            )
            logger.warning(
                "QUARANTINE RELEASED for %s after %d consecutive passing "
                "cycles (dual-LLM YES, divergence %.4f) — normal promotion "
                "gates now apply",
                vr.canonical_id, needed, divergence,
            )
            released.append(vr.canonical_id)

        return released

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
        # Duplicate-canonical guard (2026-07-15): a Kalshi market / Polymarket
        # slug already owned by a confirmed mapping must not get a SECOND
        # confirmed canonical. Quarantine markers live on the canonical_id, not
        # the underlying market, so an independently-discovered second canonical
        # for the same real-world market (e.g. POL_US_HOUSE_...MAJORITY_REP vs
        # the quarantined GOP_HOUSE_2026, both CONTROLH-2026-R) escapes the
        # marker. market_owner_counts is confirmed-only; this loop promotes
        # candidate/review, so any count>=1 is a genuine other owner.
        market_owner_counts: dict = {}
        _mgetter = getattr(self.store, "market_owner_counts", None)
        if callable(_mgetter):
            try:
                market_owner_counts = dict(await _mgetter() or {})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto_promote_validated: market_owner_counts failed: %s", exc,
                )
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

            # ── Coherence + identity gates (2026-07-10 party-swap) ──────
            # Quarantine marker: an operator/coherence demotion must stay
            # demoted — this loop re-promoted the swapped Senate mapping
            # minutes after the operator set it to review (21:38→21:46Z).
            held_note = f"{mapping.review_note or ''} {mapping.notes or ''}"
            if "[no-auto-promote]" in held_note:
                logger.info(
                    "Skipping promotion of %s: [no-auto-promote] marker set",
                    vr.canonical_id,
                )
                continue

            # Duplicate-canonical: refuse (and quarantine) if another confirmed
            # mapping already owns this Kalshi market / Polymarket slug.
            market_conflict = coherence.shared_market_owner_conflict(
                getattr(mapping, "kalshi_market_id", ""),
                getattr(mapping, "polymarket_slug", ""),
                market_owner_counts,
            )
            if market_conflict:
                logger.critical(
                    "Refusing promotion of %s and quarantining: %s",
                    vr.canonical_id, market_conflict,
                )
                try:
                    await self.store.update_status(
                        vr.canonical_id,
                        mapping.status,
                        review_note=f"[no-auto-promote] {market_conflict}",
                        allow_auto_trade=False,
                    )
                except Exception as q_exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to write duplicate-canonical quarantine note "
                        "for %s: %s", vr.canonical_id, q_exc,
                    )
                continue

            # Cross-venue price coherence: venues quoting the same outcome
            # must agree. The swapped-party mapping showed a persistent
            # 0.09-0.17 divergence (a phantom "edge" that never closed).
            platform_yes = {
                name: check.yes_price
                for name, check in vr.platforms.items()
                if check.is_live and check.yes_price > 0
            }
            divergence, div_pair = coherence.max_yes_divergence(platform_yes)
            if divergence > coherence.DEFAULT_MAX_YES_DIVERGENCE:
                logger.warning(
                    "Skipping promotion of %s: cross-venue yes divergence "
                    "%.4f > %.4f between %s — wrong-contract mapping or an "
                    "edge large enough to demand operator eyes",
                    vr.canonical_id, divergence,
                    coherence.DEFAULT_MAX_YES_DIVERGENCE, div_pair,
                )
                continue

            # FX party/identity: a party-tokened canonical must map to an
            # FX contract of the SAME party; missing symbol fails closed.
            fx_check = vr.platforms.get("forecastex")
            canonical_text = " ".join(
                [vr.canonical_id, *(mapping.aliases or ())]
            )
            if fx_check is not None and fx_check.is_live and (
                coherence.canonical_party(canonical_text) is not None
            ):
                symbol = getattr(fx_check, "contract_symbol", "") or ""
                if not symbol:
                    logger.warning(
                        "Skipping promotion of %s: party-tokened canonical "
                        "but ForecastEx contract symbol unavailable — "
                        "cannot prove party match (failing closed)",
                        vr.canonical_id,
                    )
                    continue
                conflict = coherence.party_conflict(canonical_text, symbol)
                if conflict:
                    logger.critical(
                        "Refusing promotion of %s and quarantining: %s",
                        vr.canonical_id, conflict,
                    )
                    try:
                        await self.store.update_status(
                            vr.canonical_id,
                            mapping.status,
                            review_note=(
                                f"[no-auto-promote] party conflict: {conflict}"
                            ),
                            allow_auto_trade=False,
                        )
                    except Exception as q_exc:  # noqa: BLE001
                        logger.warning(
                            "Failed to write quarantine note for %s: %s",
                            vr.canonical_id, q_exc,
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

    async def quarantine_incoherent_confirmed(
        self,
        results: List[ValidationResult],
    ) -> List[str]:
        """Demote CONFIRMED mappings whose live cross-venue prices are
        incoherent or whose FX contract party conflicts with the canonical.

        2026-07-10: the party-swapped Senate mappings sat CONFIRMED with
        allow_auto_trade for weeks while quoting a 0.09-0.17 cross-venue
        divergence — 115 "arbs" of one-way exposure accumulated before an
        audit caught it. This sweep runs every validator cycle; a violation
        demotes to review with auto-trade off and the [no-auto-promote]
        marker so promotion cannot resurrect it without an operator.
        """
        # Shared-conid detection uses DB metadata so it catches crossings the
        # live-quote checks below cannot (2026-07-11: POL_US_SENATE DEM/REP
        # shared one FX conid but the FX leg was deduped-dark, so divergence
        # over live venues was clean and the mapping escaped for weeks).
        owner_counts: Dict[str, int] = {}
        _getter = getattr(self.store, "fx_conid_owner_counts", None)
        if callable(_getter):
            try:
                owner_counts = dict(await _getter() or {})
            except Exception as exc:  # noqa: BLE001
                logger.warning("quarantine sweep: fx_conid_owner_counts failed: %s", exc)

        quarantined: List[str] = []
        for vr in results:
            platform_yes = {
                name: check.yes_price
                for name, check in vr.platforms.items()
                if check.is_live and check.yes_price > 0
            }
            divergence, div_pair = coherence.max_yes_divergence(platform_yes)
            reason = ""
            if owner_counts:
                _m = await self.store.get(vr.canonical_id)
                if _m is not None:
                    _cc = coherence.shared_fx_conid_conflict(
                        getattr(_m, "forecastex_contract_id", ""),
                        getattr(_m, "forecastex_no_contract_id", ""),
                        owner_counts,
                    )
                    if _cc:
                        reason = _cc
            if reason:
                pass
            elif divergence > coherence.DEFAULT_MAX_YES_DIVERGENCE:
                reason = (
                    f"cross-venue yes divergence {divergence:.4f} > "
                    f"{coherence.DEFAULT_MAX_YES_DIVERGENCE:.4f} between {div_pair}"
                )
            else:
                fx_check = vr.platforms.get("forecastex")
                if fx_check is not None and fx_check.is_live:
                    mapping = await self.store.get(vr.canonical_id)
                    canonical_text = " ".join(
                        [vr.canonical_id, *((mapping.aliases if mapping else ()) or ())]
                    )
                    symbol = getattr(fx_check, "contract_symbol", "") or ""
                    conflict = (
                        coherence.party_conflict(canonical_text, symbol)
                        if symbol else None
                    )
                    if conflict:
                        reason = f"party conflict: {conflict}"
            if not reason:
                continue
            logger.critical(
                "COHERENCE QUARANTINE %s: %s — demoting to review, "
                "auto-trade OFF",
                vr.canonical_id, reason,
            )
            try:
                await self.store.update_status(
                    vr.canonical_id,
                    MappingStatus.REVIEW,
                    review_note=(
                        f"[coherence-quarantine][no-auto-promote] {reason}"
                    ),
                    allow_auto_trade=False,
                )
                quarantined.append(vr.canonical_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to quarantine %s: %s", vr.canonical_id, exc,
                )
        if quarantined:
            try:
                await self.store.refresh_runtime_cache()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "refresh_runtime_cache after quarantine failed: %s", exc,
                )
        return quarantined

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

        # 2b. Coherence sweep — demote confirmed mappings whose live prices
        # or FX contract identity contradict the canonical (party-swap class).
        quarantined_ids = await self.quarantine_incoherent_confirmed(
            confirmed_results,
        )

        # 3. Validate candidates + review for promotion
        candidate_results = await self.validate_all(status="candidate")
        review_results = await self.validate_all(status="review")

        # 3b. Quarantine auto-release: env-gated, dual-LLM + coherence +
        # persistence. Runs BEFORE promotion so a mapping released this
        # cycle can be adjudicated by the full gate chain the same cycle.
        released_ids = await self.sweep_quarantine_releases(review_results)

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
            "quarantine_released": len(released_ids),
            "quarantine_released_ids": released_ids[:20],
            "coherence_quarantined": len(quarantined_ids),
            "coherence_quarantined_ids": quarantined_ids[:20],
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
