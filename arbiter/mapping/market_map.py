"""
Durable Postgres-backed market mapping store.
Replaces the in-memory MARKET_MAP dict from settings.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

import asyncpg
from ..config.settings import (
    MARKET_SEEDS,
    MarketMappingRecord,
    normalize_market_text,
    replace_runtime_market_map,
    similarity_score,
    upsert_runtime_market_mapping,
)
from ..sql.connection import create_pool

logger = logging.getLogger("arbiter.mapping")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MappingStatus(str, Enum):
    CANDIDATE = "candidate"   # Auto-detected, needs review
    REVIEW = "review"         # Operator is evaluating; never auto-tradable
    CONFIRMED = "confirmed"    # Reviewed and approved
    REJECTED = "rejected"      # Reviewed and rejected
    EXPIRED = "expired"        # Revalidation failed / market resolved


@dataclass
class MarketMapping:
    canonical_id: str
    description: str
    status: MappingStatus
    allow_auto_trade: bool = False
    aliases: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    kalshi_market_id: str = ""
    polymarket_slug: str = ""
    polymarket_question: str = ""
    notes: str = ""
    review_note: str = ""
    mapping_score: float = 0.0
    confidence: float = 0.0
    expires_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    # SAFE-06 (plan 03-06): JSONB-serialized resolution-criteria payload and
    # a denormalized match-status column. Both land idempotently via the
    # ALTER TABLE migration in arbiter/sql/init.sql. Storing the criteria
    # payload as a JSON string (not a dict) keeps the dataclass hashable /
    # immutable-ish and lines up 1:1 with the JSONB column.
    resolution_criteria_json: str = ""
    resolution_match_status: str = "pending_operator_review"

    def to_dict(self) -> dict:
        # T-3-06-F: malformed JSON in resolution_criteria_json must not crash
        # serialization. Fall back to None on parse failure so callers can
        # render a "pending review" state.
        resolution_criteria: Optional[dict] = None
        if self.resolution_criteria_json:
            try:
                resolution_criteria = json.loads(self.resolution_criteria_json)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "MarketMapping %s has malformed resolution_criteria JSON: %s",
                    self.canonical_id, exc,
                )
                resolution_criteria = None
        return {
            "canonical_id": self.canonical_id,
            "description": self.description,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "allow_auto_trade": self.allow_auto_trade,
            "aliases": list(self.aliases),
            "tags": list(self.tags),
            "kalshi": self.kalshi_market_id,
            "polymarket": self.polymarket_slug,
            "polymarket_question": self.polymarket_question,
            "notes": self.notes,
            "review_note": self.review_note,
            "mapping_score": self.mapping_score,
            "confidence": self.confidence,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_validated_at": self.last_validated_at.isoformat() if self.last_validated_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            # SAFE-06: expose resolution-criteria side-by-side with core fields.
            "resolution_criteria": resolution_criteria,
            "resolution_match_status": self.resolution_match_status,
        }

    @classmethod
    def from_record(cls, record: MarketMappingRecord) -> "MarketMapping":
        score = similarity_score(record.description, " ".join(record.aliases)) if record.aliases else 0.0
        criteria_json = json.dumps(record.resolution_criteria) if record.resolution_criteria is not None else ""
        return cls(
            canonical_id=record.canonical_id,
            description=record.description,
            status=_coerce_status(record.status),
            allow_auto_trade=record.allow_auto_trade,
            aliases=record.aliases,
            tags=record.tags,
            kalshi_market_id=record.kalshi,
            polymarket_slug=record.polymarket,
            polymarket_question=record.polymarket_question,
            notes=record.notes,
            mapping_score=score,
            confidence=score,
            resolution_criteria_json=criteria_json,
            resolution_match_status=record.resolution_match_status or "pending_operator_review",
        )

    @classmethod
    def from_dict(cls, canonical_id: str, payload: dict[str, Any]) -> "MarketMapping":
        criteria = payload.get("resolution_criteria")
        criteria_json = json.dumps(criteria) if criteria is not None else ""
        return cls(
            canonical_id=canonical_id,
            description=str(payload.get("description", canonical_id)),
            status=_coerce_status(str(payload.get("status", "candidate"))),
            allow_auto_trade=bool(payload.get("allow_auto_trade", False)),
            aliases=tuple(payload.get("aliases") or ()),
            tags=tuple(payload.get("tags") or ()),
            kalshi_market_id=str(payload.get("kalshi", "") or payload.get("kalshi_market_id", "") or ""),
            polymarket_slug=str(payload.get("polymarket", "") or payload.get("polymarket_slug", "") or ""),
            polymarket_question=str(payload.get("polymarket_question", "") or ""),
            notes=str(payload.get("notes", "") or ""),
            review_note=str(payload.get("review_note", "") or ""),
            mapping_score=float(payload.get("mapping_score", payload.get("confidence", 0.0)) or 0.0),
            confidence=float(payload.get("confidence", payload.get("mapping_score", 0.0)) or 0.0),
            resolution_criteria_json=criteria_json,
            resolution_match_status=str(
                payload.get("resolution_match_status", "pending_operator_review")
                or "pending_operator_review"
            ),
        )


def _coerce_status(value: str | MappingStatus | None) -> MappingStatus:
    if isinstance(value, MappingStatus):
        return value
    try:
        return MappingStatus(str(value or MappingStatus.CANDIDATE.value))
    except ValueError:
        logger.warning("Unknown mapping status %r, falling back to candidate", value)
        return MappingStatus.CANDIDATE


SQL_INIT = """
CREATE TABLE IF NOT EXISTS market_mappings (
    canonical_id         VARCHAR(60) PRIMARY KEY,
    description          TEXT NOT NULL,
    status               VARCHAR(20) NOT NULL DEFAULT 'candidate',
    allow_auto_trade     BOOLEAN DEFAULT FALSE,
    aliases              TEXT[] DEFAULT '{}',
    tags                 TEXT[] DEFAULT '{}',
    kalshi_market_id     VARCHAR(100) DEFAULT '',
    polymarket_slug      VARCHAR(200) DEFAULT '',
    polymarket_question  TEXT DEFAULT '',
    notes                TEXT DEFAULT '',
    review_note          TEXT DEFAULT '',
    mapping_score        DECIMAL(5,4) DEFAULT 0,
    confidence           DECIMAL(5,4) DEFAULT 0,
    expires_at           TIMESTAMPTZ,
    last_validated_at    TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mappings_status ON market_mappings(status);
CREATE INDEX IF NOT EXISTS idx_mappings_kalshi ON market_mappings(kalshi_market_id) WHERE kalshi_market_id != '';
CREATE INDEX IF NOT EXISTS idx_mappings_poly ON market_mappings(polymarket_slug) WHERE polymarket_slug != '';
CREATE INDEX IF NOT EXISTS idx_mappings_expires ON market_mappings(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS mapping_candidates (
    id                  SERIAL PRIMARY KEY,
    canonical_id        VARCHAR(60) NOT NULL,
    platform            VARCHAR(20) NOT NULL,
    platform_market_id  VARCHAR(200) NOT NULL,
    description         TEXT,
    match_score         DECIMAL(5,4) DEFAULT 0,
    status              VARCHAR(20) DEFAULT 'pending',
    reviewed_at         TIMESTAMPTZ,
    reviewer_note       TEXT DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(platform, platform_market_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON mapping_candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_canonical ON mapping_candidates(canonical_id);

-- SAFE-06 (plan 03-06): idempotent migration adding resolution-criteria columns.
-- Kept inside SQL_INIT so existing init_schema() calls pick it up. Postgres
-- supports `ADD COLUMN IF NOT EXISTS` since 9.6.
ALTER TABLE market_mappings
    ADD COLUMN IF NOT EXISTS resolution_criteria JSONB,
    ADD COLUMN IF NOT EXISTS resolution_match_status VARCHAR(40)
        DEFAULT 'pending_operator_review';
"""


class MarketMappingStore:
    """
    Postgres-backed market mapping store.
    Handles all market ID mappings across Kalshi and Polymarket.

    Usage:
        store = MarketMappingStore(database_url)
        await store.connect()
        await store.init_schema()          # Run once
        await store.seed_from_records()    # Run once to migrate from MARKET_SEEDS

        mapping = await store.get("DEM_HOUSE_2026")
        async for cid, m in store.iter_confirmed(require_auto_trade=True):
            ...
    """

    _pool: Optional[asyncpg.Pool] = None

    def __init__(self, database_url: str):
        self.database_url = database_url

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("MarketMappingStore: connected to Postgres")

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("MarketMappingStore: disconnected")

    async def acquire(self) -> asyncpg.Connection:
        if self._pool is None:
            await self.connect()
        return await self._pool.acquire()

    # ─── Schema ──────────────────────────────────────────────────────────────

    async def init_schema(self) -> None:
        """Create tables and indexes. Idempotent — safe to run multiple times."""
        conn = await self.acquire()
        try:
            await conn.execute(SQL_INIT)
            logger.info("MarketMappingStore schema initialized")
        finally:
            await self._pool.release(conn)

    # ─── Seed ────────────────────────────────────────────────────────────────

    async def seed_from_records(
        self,
        records: Tuple[MarketMappingRecord, ...] = MARKET_SEEDS,
        overwrite: bool = False,
    ) -> int:
        """
        Bulk-insert records from MARKET_SEEDS (or custom tuple).
        Does NOT overwrite existing rows unless overwrite=True.
        Returns number of rows inserted.
        """
        conn = await self.acquire()
        inserted = 0
        try:
            for record in records:
                existing = await conn.fetchrow(
                    "SELECT 1 FROM market_mappings WHERE canonical_id = $1",
                    record.canonical_id,
                )
                if existing and not overwrite:
                    continue

                mapping = MarketMapping.from_record(record)
                criteria_value = mapping.resolution_criteria_json or None
                await conn.execute(
                    """
                    INSERT INTO market_mappings (
                        canonical_id, description, status, allow_auto_trade,
                        aliases, tags, kalshi_market_id, polymarket_slug,
                        polymarket_question,
                        notes, mapping_score, confidence, updated_at,
                        resolution_criteria, resolution_match_status
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW(),
                        $13::jsonb, $14
                    ) ON CONFLICT (canonical_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        status = EXCLUDED.status,
                        allow_auto_trade = EXCLUDED.allow_auto_trade,
                        aliases = EXCLUDED.aliases,
                        tags = EXCLUDED.tags,
                        kalshi_market_id = EXCLUDED.kalshi_market_id,
                        polymarket_slug = EXCLUDED.polymarket_slug,
                        polymarket_question = EXCLUDED.polymarket_question,
                        notes = EXCLUDED.notes,
                        mapping_score = EXCLUDED.mapping_score,
                        confidence = EXCLUDED.confidence,
                        resolution_criteria = EXCLUDED.resolution_criteria,
                        resolution_match_status = EXCLUDED.resolution_match_status,
                        updated_at = NOW()
                    """,
                    mapping.canonical_id,
                    mapping.description,
                    mapping.status.value,
                    mapping.allow_auto_trade,
                    list(mapping.aliases),
                    list(mapping.tags),
                    mapping.kalshi_market_id,
                    mapping.polymarket_slug,
                    mapping.polymarket_question,
                    mapping.notes,
                    mapping.mapping_score,
                    mapping.confidence,
                    criteria_value,
                    mapping.resolution_match_status or "pending_operator_review",
                )
                inserted += 1

            logger.info(f"MarketMappingStore: seeded {inserted}/{len(records)} records")
            await self.refresh_runtime_cache()
            return inserted

        finally:
            await self._pool.release(conn)

    # ─── CRUD ───────────────────────────────────────────────────────────────

    async def get(self, canonical_id: str) -> Optional[MarketMapping]:
        """Get a single mapping by canonical ID."""
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM market_mappings WHERE canonical_id = $1",
                canonical_id,
            )
            return self._row_to_mapping(row) if row else None

        finally:
            await self._pool.release(conn)

    async def upsert(self, mapping: MarketMapping) -> MarketMapping:
        """Insert or update a mapping."""
        conn = await self.acquire()
        try:
            now = utc_now()
            # SAFE-06 / T-3-06-E: resolution_criteria flows in as the dataclass
            # JSON string. Cast to JSONB via ::jsonb parameterized binding so
            # asyncpg cannot be confused into string-interpolation.
            criteria_value = (
                mapping.resolution_criteria_json
                if mapping.resolution_criteria_json
                else None
            )
            await conn.execute(
                """
                INSERT INTO market_mappings (
                    canonical_id, description, status, allow_auto_trade,
                    aliases, tags, kalshi_market_id, polymarket_slug,
                    polymarket_question,
                    notes, review_note, mapping_score, confidence,
                    expires_at, last_validated_at, created_at, updated_at,
                    resolution_criteria, resolution_match_status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                    $18::jsonb, $19
                ) ON CONFLICT (canonical_id) DO UPDATE SET
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    allow_auto_trade = EXCLUDED.allow_auto_trade,
                    aliases = EXCLUDED.aliases,
                    tags = EXCLUDED.tags,
                    kalshi_market_id = EXCLUDED.kalshi_market_id,
                    polymarket_slug = EXCLUDED.polymarket_slug,
                    polymarket_question = EXCLUDED.polymarket_question,
                    notes = EXCLUDED.notes,
                    review_note = EXCLUDED.review_note,
                    mapping_score = EXCLUDED.mapping_score,
                    confidence = EXCLUDED.confidence,
                    expires_at = EXCLUDED.expires_at,
                    last_validated_at = EXCLUDED.last_validated_at,
                    updated_at = NOW(),
                    resolution_criteria = EXCLUDED.resolution_criteria,
                    resolution_match_status = EXCLUDED.resolution_match_status
                """,
                mapping.canonical_id,
                mapping.description,
                mapping.status.value if isinstance(mapping.status, Enum) else mapping.status,
                mapping.allow_auto_trade,
                list(mapping.aliases),
                list(mapping.tags),
                mapping.kalshi_market_id,
                mapping.polymarket_slug,
                mapping.polymarket_question,
                mapping.notes,
                mapping.review_note,
                mapping.mapping_score,
                mapping.confidence,
                mapping.expires_at,
                mapping.last_validated_at,
                mapping.created_at,
                now,
                criteria_value,
                mapping.resolution_match_status or "pending_operator_review",
            )
            mapping.updated_at = now
            upsert_runtime_market_mapping(mapping.canonical_id, mapping.to_dict())
            return mapping

        finally:
            await self._pool.release(conn)

    async def update_status(
        self,
        canonical_id: str,
        status: MappingStatus,
        review_note: str = "",
        allow_auto_trade: Optional[bool] = None,
    ) -> Optional[MarketMapping]:
        """Review and update mapping status (approve/reject/expire)."""
        mapping = await self.get(canonical_id)
        if not mapping:
            return None

        mapping.status = status
        mapping.review_note = review_note
        if allow_auto_trade is not None:
            mapping.allow_auto_trade = allow_auto_trade
        mapping.updated_at = utc_now()

        return await self.upsert(mapping)

    async def delete(self, canonical_id: str) -> bool:
        """Remove a mapping."""
        conn = await self.acquire()
        try:
            result = await conn.execute(
                "DELETE FROM market_mappings WHERE canonical_id = $1",
                canonical_id,
            )
            if result == "DELETE 1":
                from ..config.settings import MARKET_MAP

                MARKET_MAP.pop(canonical_id, None)
            return result == "DELETE 1"

        finally:
            await self._pool.release(conn)

    # ─── Queries ────────────────────────────────────────────────────────────

    async def iter_confirmed(
        self,
        require_auto_trade: bool = False,
    ) -> Iterable[Tuple[str, MarketMapping]]:
        """
        Iterate over confirmed mappings — replacement for iter_confirmed_market_mappings().
        Yields (canonical_id, MarketMapping) tuples.
        """
        conn = await self.acquire()
        try:
            query = "SELECT * FROM market_mappings WHERE status = 'confirmed'"
            if require_auto_trade:
                query += " AND allow_auto_trade = TRUE"
            query += " ORDER BY description"

            rows = await conn.fetch(query)
            for row in rows:
                yield row["canonical_id"], self._row_to_mapping(row)

        finally:
            await self._pool.release(conn)

    async def get_by_platform(
        self,
        platform: str,
        platform_market_id: str,
    ) -> Optional[MarketMapping]:
        """Look up mapping by venue-specific market ID."""
        conn = await self.acquire()
        try:
            col = {
                "kalshi": "kalshi_market_id",
                "polymarket": "polymarket_slug",
            }.get(platform.lower())

            if not col:
                return None

            row = await conn.fetchrow(
                f"SELECT * FROM market_mappings WHERE {col} = $1 AND {col} != ''",
                platform_market_id,
            )
            return self._row_to_mapping(row) if row else None

        finally:
            await self._pool.release(conn)

    async def get_by_exact_pair(
        self,
        *,
        kalshi_market_id: str,
        polymarket_slug: str,
    ) -> Optional[MarketMapping]:
        """Return the mapping that already binds this exact venue pair."""
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                SELECT * FROM market_mappings
                WHERE kalshi_market_id = $1 AND polymarket_slug = $2
                """,
                kalshi_market_id,
                polymarket_slug,
            )
            return self._row_to_mapping(row) if row else None
        finally:
            await self._pool.release(conn)

    async def search_candidates(
        self,
        query_text: str,
        platform: Optional[str] = None,
        min_score: float = 0.3,
        limit: int = 20,
    ) -> List[MarketMapping]:
        """
        Text search across description and aliases.
        Used for auto-candidate matching and UI search.
        """
        conn = await self.acquire()
        try:
            q = f"%{query_text.lower()}%"
            if platform:
                col = {"kalshi": "kalshi_market_id", "polymarket": "polymarket_slug"}.get(platform.lower())
                col_filter = f" AND {col} != ''" if col else ""
            else:
                col_filter = ""

            rows = await conn.fetch(
                f"""
                SELECT *, confidence FROM market_mappings
                WHERE (
                    LOWER(description) LIKE $1
                    OR $1 = ANY(SELECT LOWER(unnest(aliases)))
                ){col_filter}
                AND confidence >= $2
                ORDER BY confidence DESC
                LIMIT $3
                """,
                q, min_score, limit,
            )
            return [self._row_to_mapping(r) for r in rows]

        finally:
            await self._pool.release(conn)

    async def get_candidates(
        self,
        status: str = "pending",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get auto-detected mapping candidates awaiting review."""
        conn = await self.acquire()
        try:
            rows = await conn.fetch(
                """
                SELECT * FROM mapping_candidates
                WHERE status = $1
                ORDER BY match_score DESC, created_at DESC
                LIMIT $2
                """,
                status, limit,
            )
            return [dict(r) for r in rows]

        finally:
            await self._pool.release(conn)

    async def add_candidate(
        self,
        canonical_id: str,
        platform: str,
        platform_market_id: str,
        description: str,
        match_score: float,
    ) -> Dict[str, Any]:
        """Record a new auto-detected mapping candidate."""
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO mapping_candidates (
                    canonical_id, platform, platform_market_id,
                    description, match_score, status
                ) VALUES ($1, $2, $3, $4, $5, 'pending')
                ON CONFLICT (platform, platform_market_id) DO UPDATE
                    SET match_score = EXCLUDED.match_score,
                        canonical_id = EXCLUDED.canonical_id,
                        description = EXCLUDED.description
                RETURNING *
                """,
                canonical_id, platform, platform_market_id, description, match_score,
            )
            return dict(row)

        finally:
            await self._pool.release(conn)

    async def count_candidates(self) -> int:
        """Return the number of candidate/review mappings currently pending operator action."""
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM market_mappings
                WHERE status IN ('candidate', 'review')
                """
            )
            return int(row["total"] if row else 0)
        finally:
            await self._pool.release(conn)

    async def write_candidates(self, candidates: List[Dict[str, Any]]) -> int:
        """Upsert discovery candidates into the canonical market_mappings table.

        Discovery is never allowed to auto-enable trading or silently promote a
        confirmed mapping. New rows always land as ``candidate`` with
        ``allow_auto_trade=False``; existing non-confirmed rows are updated in
        place so the dashboard and scanner see the freshest venue IDs + score.
        """
        written = 0
        for candidate in candidates:
            mapping = await self._mapping_from_candidate(candidate)
            if mapping is None:
                continue
            await self.upsert(mapping)
            written += 1
        return written

    async def sync_candidates(self, candidates: List[Dict[str, Any]]) -> int:
        """Upsert the latest discovery pass and expire stale auto candidates.

        Auto-discovery is a rolling snapshot of the current live venue catalogs.
        When heuristics improve or markets rotate, old auto-generated candidate
        pairs should fall out of the queue instead of accumulating forever.
        """
        written = await self.write_candidates(candidates)
        active_pairs = {
            (
                str(candidate.get("kalshi_ticker", "") or "").strip(),
                str(candidate.get("poly_slug", "") or "").strip(),
            )
            for candidate in candidates
            if str(candidate.get("kalshi_ticker", "") or "").strip()
            and str(candidate.get("poly_slug", "") or "").strip()
        }

        existing_candidates = await self.all(status=MappingStatus.CANDIDATE.value, limit=5000)
        for mapping in existing_candidates:
            if not self._is_expirable_auto_candidate(mapping):
                continue
            pair = (
                str(mapping.kalshi_market_id or "").strip(),
                str(mapping.polymarket_slug or "").strip(),
            )
            if not pair[0] or not pair[1] or pair in active_pairs:
                continue
            mapping.status = MappingStatus.EXPIRED
            mapping.allow_auto_trade = False
            mapping.review_note = "Expired by latest discovery sync."
            await self.upsert(mapping)

        return written

    async def review_candidate(
        self,
        candidate_id: int,
        decision: str,  # "approve" | "reject"
        reviewer_note: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Review a candidate — either creates/updates a confirmed mapping (approve)
        or marks the candidate rejected.
        """
        conn = await self.acquire()
        try:
            candidate = await conn.fetchrow(
                "SELECT * FROM mapping_candidates WHERE id = $1",
                candidate_id,
            )
            if not candidate:
                return None

            if decision == "approve":
                # Upsert the confirmed mapping
                mapping = MarketMapping(
                    canonical_id=candidate["canonical_id"],
                    description=candidate["description"],
                    status=MappingStatus.CANDIDATE,
                    **{f"{candidate['platform']}_market_id": candidate["platform_market_id"]}
                    if candidate["platform"] == "kalshi"
                    else {},
                    **{"polymarket_slug": candidate["platform_market_id"]}
                    if candidate["platform"] == "polymarket"
                    else {},
                )
                await self.upsert(mapping)

            await conn.execute(
                """
                UPDATE mapping_candidates
                SET status = $2, reviewed_at = NOW(), reviewer_note = $3
                WHERE id = $1
                """,
                candidate_id, "approved" if decision == "approve" else "rejected", reviewer_note,
            )

            return dict(candidate)

        finally:
            await self._pool.release(conn)

    async def all(
        self,
        status: Optional[str] = None,
        limit: int = 500,
    ) -> List[MarketMapping]:
        """List all mappings, optionally filtered by status."""
        conn = await self.acquire()
        try:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM market_mappings WHERE status = $1 ORDER BY description LIMIT $2",
                    status, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM market_mappings ORDER BY description LIMIT $1",
                    limit,
                )
            return [self._row_to_mapping(r) for r in rows]

        finally:
            await self._pool.release(conn)

    async def refresh_runtime_cache(self) -> int:
        """Mirror all durable mappings into the legacy in-process MARKET_MAP."""
        rows = await self.all(limit=5000)
        replace_runtime_market_map(
            (mapping.canonical_id, mapping.to_dict()) for mapping in rows
        )
        return len(rows)

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_expirable_auto_candidate(mapping: MarketMapping) -> bool:
        if mapping.status != MappingStatus.CANDIDATE:
            return False
        if mapping.allow_auto_trade:
            return False
        if mapping.canonical_id.startswith("AUTO_"):
            return True
        return "Auto-discovered candidate mapping" in str(mapping.notes or "")

    async def _mapping_from_candidate(self, candidate: Dict[str, Any]) -> Optional[MarketMapping]:
        kalshi_ticker = str(candidate.get("kalshi_ticker", "") or "").strip()
        poly_slug = str(candidate.get("poly_slug", "") or "").strip()
        if not kalshi_ticker or not poly_slug:
            return None

        pair_mapping = await self.get_by_exact_pair(
            kalshi_market_id=kalshi_ticker,
            polymarket_slug=poly_slug,
        )
        kalshi_mapping = await self.get_by_platform("kalshi", kalshi_ticker)
        poly_mapping = await self.get_by_platform("polymarket", poly_slug)

        existing = pair_mapping
        if existing is None and kalshi_mapping and poly_mapping:
            if kalshi_mapping.canonical_id == poly_mapping.canonical_id:
                existing = kalshi_mapping
        if existing is None:
            if (
                kalshi_mapping is not None
                and not kalshi_mapping.polymarket_slug
                and kalshi_mapping.status != MappingStatus.CONFIRMED
            ):
                existing = kalshi_mapping
            elif (
                poly_mapping is not None
                and not poly_mapping.kalshi_market_id
                and poly_mapping.status != MappingStatus.CONFIRMED
            ):
                existing = poly_mapping

        if existing is None:
            canonical_id = str(candidate.get("canonical_id", "") or "").strip()
            if not canonical_id:
                canonical_id = self._generated_candidate_id(kalshi_ticker, poly_slug)
            existing = MarketMapping(
                canonical_id=canonical_id,
                description=self._candidate_description(candidate),
                status=MappingStatus.CANDIDATE,
                allow_auto_trade=False,
            )

        score = float(candidate.get("score", 0.0) or 0.0)
        candidate_status = _coerce_status(candidate.get("status", MappingStatus.CANDIDATE.value))
        candidate_allow_auto = bool(candidate.get("allow_auto_trade", False))
        existing.description = self._candidate_description(candidate, fallback=existing.description)
        existing.kalshi_market_id = kalshi_ticker
        existing.polymarket_slug = poly_slug
        existing.polymarket_question = str(
            candidate.get("poly_question", "") or existing.polymarket_question or ""
        )
        existing.mapping_score = score
        existing.confidence = score
        existing.notes = str(candidate.get("notes", "") or existing.notes or "Auto-discovered candidate mapping.")
        criteria = candidate.get("resolution_criteria")
        if criteria is not None:
            existing.resolution_criteria_json = json.dumps(criteria)
        existing.resolution_match_status = str(
            candidate.get("resolution_match_status", existing.resolution_match_status or "pending_operator_review")
            or "pending_operator_review"
        )
        if existing.status == MappingStatus.CONFIRMED:
            existing.allow_auto_trade = existing.allow_auto_trade
        elif candidate_status == MappingStatus.CONFIRMED:
            existing.status = MappingStatus.CONFIRMED
            existing.allow_auto_trade = candidate_allow_auto
        else:
            existing.allow_auto_trade = False
            if existing.status not in {MappingStatus.REJECTED, MappingStatus.EXPIRED, MappingStatus.REVIEW}:
                existing.status = MappingStatus.CANDIDATE
        return existing

    @staticmethod
    def _candidate_description(candidate: Dict[str, Any], fallback: str = "") -> str:
        for field in ("poly_question", "kalshi_title", "description"):
            value = str(candidate.get(field, "") or "").strip()
            if value:
                return value
        return fallback or "Auto-discovered market candidate"

    @staticmethod
    def _generated_candidate_id(kalshi_ticker: str, poly_slug: str) -> str:
        digest = hashlib.sha1(f"{kalshi_ticker}|{poly_slug}".encode("utf-8")).hexdigest()[:12]
        base = normalize_market_text(kalshi_ticker).replace(" ", "_").upper()[:24] or "MARKET"
        return f"AUTO_{base}_{digest}"

    def _row_to_mapping(self, row: asyncpg.Record) -> Optional[MarketMapping]:
        if row is None:
            return None
        # SAFE-06: resolution_criteria may not exist on rows read before the
        # ALTER TABLE migration ran in older deployments. Use .get-style
        # access via dict() so missing columns default to None.
        row_dict = dict(row)
        criteria_raw = row_dict.get("resolution_criteria")
        # asyncpg may deliver JSONB as a dict (when json_codec set) or as a
        # str (default). Normalize to a JSON string for the dataclass field.
        if criteria_raw is None:
            criteria_json = ""
        elif isinstance(criteria_raw, (dict, list)):
            criteria_json = json.dumps(criteria_raw)
        else:
            criteria_json = str(criteria_raw)
        return MarketMapping(
            canonical_id=row["canonical_id"],
            description=row["description"],
            status=_coerce_status(row["status"]),
            allow_auto_trade=row["allow_auto_trade"],
            aliases=tuple(row["aliases"]) if row["aliases"] else (),
            tags=tuple(row["tags"]) if row["tags"] else (),
            kalshi_market_id=row["kalshi_market_id"] or "",
            polymarket_slug=row["polymarket_slug"] or "",
            polymarket_question=row["polymarket_question"] or "",
            notes=row["notes"] or "",
            review_note=row["review_note"] or "",
            mapping_score=float(row["mapping_score"]) if row["mapping_score"] else 0.0,
            confidence=float(row["confidence"]) if row["confidence"] else 0.0,
            expires_at=row["expires_at"],
            last_validated_at=row["last_validated_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolution_criteria_json=criteria_json,
            resolution_match_status=row_dict.get("resolution_match_status")
                or "pending_operator_review",
        )
