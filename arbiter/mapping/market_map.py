"""
Durable Postgres-backed market mapping store.
Replaces the in-memory MARKET_MAP dict from settings.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

import asyncpg
from ..config.settings import MARKET_SEEDS, MarketMappingRecord, normalize_market_text, similarity_score

logger = logging.getLogger("arbiter.mapping")


class MappingStatus(str, Enum):
    CANDIDATE = "candidate"   # Auto-detected, needs review
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
    predictit_id: str = ""
    predictit_contract_keywords: Tuple[str, ...] = ()
    notes: str = ""
    review_note: str = ""
    mapping_score: float = 0.0
    confidence: float = 0.0
    expires_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
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
            "predictit": self.predictit_id,
            "predictit_contract_keywords": list(self.predictit_contract_keywords),
            "notes": self.notes,
            "review_note": self.review_note,
            "mapping_score": self.mapping_score,
            "confidence": self.confidence,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_validated_at": self.last_validated_at.isoformat() if self.last_validated_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_record(cls, record: MarketMappingRecord) -> "MarketMapping":
        score = similarity_score(record.description, " ".join(record.aliases)) if record.aliases else 0.0
        return cls(
            canonical_id=record.canonical_id,
            description=record.description,
            status=MappingStatus.CONFIRMED if record.status == "confirmed" else MappingStatus(record.status),
            allow_auto_trade=record.allow_auto_trade,
            aliases=record.aliases,
            tags=record.tags,
            kalshi_market_id=record.kalshi,
            polymarket_slug=record.polymarket,
            polymarket_question=record.polymarket_question,
            predictit_id=record.predictit,
            predictit_contract_keywords=record.predictit_contract_keywords,
            notes=record.notes,
            mapping_score=score,
            confidence=score,
        )


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
    predictit_id         VARCHAR(100) DEFAULT '',
    predictit_contract_keywords TEXT[] DEFAULT '{}',
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
CREATE INDEX IF NOT EXISTS idx_mappings_predictit ON market_mappings(predictit_id) WHERE predictit_id != '';
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
"""


class MarketMappingStore:
    """
    Postgres-backed market mapping store.
    Handles all market ID mappings across Kalshi, Polymarket, and PredictIt.

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
            self._pool = await asyncpg.create_pool(
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
                await conn.execute(
                    """
                    INSERT INTO market_mappings (
                        canonical_id, description, status, allow_auto_trade,
                        aliases, tags, kalshi_market_id, polymarket_slug,
                        polymarket_question, predictit_id, predictit_contract_keywords,
                        notes, mapping_score, confidence, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, NOW()
                    ) ON CONFLICT (canonical_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        status = EXCLUDED.status,
                        allow_auto_trade = EXCLUDED.allow_auto_trade,
                        aliases = EXCLUDED.aliases,
                        tags = EXCLUDED.tags,
                        kalshi_market_id = EXCLUDED.kalshi_market_id,
                        polymarket_slug = EXCLUDED.polymarket_slug,
                        polymarket_question = EXCLUDED.polymarket_question,
                        predictit_id = EXCLUDED.predictit_id,
                        predictit_contract_keywords = EXCLUDED.predictit_contract_keywords,
                        notes = EXCLUDED.notes,
                        mapping_score = EXCLUDED.mapping_score,
                        confidence = EXCLUDED.confidence,
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
                    mapping.predictit_id,
                    list(mapping.predictit_contract_keywords),
                    mapping.notes,
                    mapping.mapping_score,
                    mapping.confidence,
                )
                inserted += 1

            logger.info(f"MarketMappingStore: seeded {inserted}/{len(records)} records")
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
            now = datetime.utcnow()
            await conn.execute(
                """
                INSERT INTO market_mappings (
                    canonical_id, description, status, allow_auto_trade,
                    aliases, tags, kalshi_market_id, polymarket_slug,
                    polymarket_question, predictit_id, predictit_contract_keywords,
                    notes, review_note, mapping_score, confidence,
                    expires_at, last_validated_at, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19
                ) ON CONFLICT (canonical_id) DO UPDATE SET
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    allow_auto_trade = EXCLUDED.allow_auto_trade,
                    aliases = EXCLUDED.aliases,
                    tags = EXCLUDED.tags,
                    kalshi_market_id = EXCLUDED.kalshi_market_id,
                    polymarket_slug = EXCLUDED.polymarket_slug,
                    polymarket_question = EXCLUDED.polymarket_question,
                    predictit_id = EXCLUDED.predictit_id,
                    predictit_contract_keywords = EXCLUDED.predictit_contract_keywords,
                    notes = EXCLUDED.notes,
                    review_note = EXCLUDED.review_note,
                    mapping_score = EXCLUDED.mapping_score,
                    confidence = EXCLUDED.confidence,
                    expires_at = EXCLUDED.expires_at,
                    last_validated_at = EXCLUDED.last_validated_at,
                    updated_at = NOW()
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
                mapping.predictit_id,
                list(mapping.predictit_contract_keywords),
                mapping.notes,
                mapping.review_note,
                mapping.mapping_score,
                mapping.confidence,
                mapping.expires_at,
                mapping.last_validated_at,
                mapping.created_at,
                now,
            )
            mapping.updated_at = now
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
        mapping.updated_at = datetime.utcnow()

        return await self.upsert(mapping)

    async def delete(self, canonical_id: str) -> bool:
        """Remove a mapping."""
        conn = await self.acquire()
        try:
            result = await conn.execute(
                "DELETE FROM market_mappings WHERE canonical_id = $1",
                canonical_id,
            )
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
                "predictit": "predictit_id",
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
                col = {"kalshi": "kalshi_market_id", "polymarket": "polymarket_slug", "predictit": "predictit_id"}.get(platform.lower())
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
                    **{f"{candidate['platform']}_slug" if candidate["platform"] == "polymarket" else candidate["platform"]: candidate["platform_market_id"]}
                    if candidate["platform"] in ("polymarket", "predictit")
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

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _row_to_mapping(self, row: asyncpg.Record) -> Optional[MarketMapping]:
        if row is None:
            return None
        return MarketMapping(
            canonical_id=row["canonical_id"],
            description=row["description"],
            status=MappingStatus(row["status"]) if row["status"] else MappingStatus.CANDIDATE,
            allow_auto_trade=row["allow_auto_trade"],
            aliases=tuple(row["aliases"]) if row["aliases"] else (),
            tags=tuple(row["tags"]) if row["tags"] else (),
            kalshi_market_id=row["kalshi_market_id"] or "",
            polymarket_slug=row["polymarket_slug"] or "",
            polymarket_question=row["polymarket_question"] or "",
            predictit_id=row["predictit_id"] or "",
            predictit_contract_keywords=tuple(row["predictit_contract_keywords"]) if row["predictit_contract_keywords"] else (),
            notes=row["notes"] or "",
            review_note=row["review_note"] or "",
            mapping_score=float(row["mapping_score"]) if row["mapping_score"] else 0.0,
            confidence=float(row["confidence"]) if row["confidence"] else 0.0,
            expires_at=row["expires_at"],
            last_validated_at=row["last_validated_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
