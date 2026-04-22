"""
Tests for MarketMappingStore.
"""
import asyncio
import sys
import os
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.mapping.market_map import (
    MappingStatus,
    MarketMapping,
    MarketMappingStore,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MockRecord:
    def __init__(self, data: dict):
        self._data = {k: v for k, v in data.items() if v is not None}

    def __getitem__(self, key):
        return self._data.get(key)

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()


class MockConn:
    def __init__(self):
        self._mappings = {}
        self._candidates = {}  # id -> dict
        self._next_cid = 1

    async def execute(self, query: str, *args):
        if "CREATE TABLE" in query:
            return None
        if "INSERT INTO market_mappings" in query and "ON CONFLICT" in query:
            self._mappings[args[0]] = self._build_mapping_dict(args)
            return "INSERT 0 1"
        if "INSERT INTO mapping_candidates" in query and "RETURNING" in query.upper():
            c = {
                "id": self._next_cid,
                "canonical_id": args[0],
                "platform": args[1],
                "platform_market_id": args[2],
                "description": args[3],
                "match_score": float(args[4]) if args[4] else 0.0,
                "status": "pending",
                "reviewed_at": None,
                "reviewer_note": "",
                "created_at": utc_now(),
            }
            self._candidates[self._next_cid] = c
            self._next_cid += 1
            return "INSERT 0 1"
        if "UPDATE mapping_candidates" in query and "reviewed_at" in query:
            cid = args[0]
            if cid in self._candidates:
                self._candidates[cid]["status"] = args[1]
                self._candidates[cid]["reviewed_at"] = utc_now()
                self._candidates[cid]["reviewer_note"] = args[2] if len(args) > 2 else ""
            return "UPDATE 1"
        if "DELETE FROM market_mappings" in query:
            cid = args[0]
            self._mappings.pop(cid, None)
            return "DELETE 1"
        return None

    async def fetchrow(self, query: str, *args):
        if "SELECT COUNT(*) AS total" in query:
            total = sum(
                1
                for mapping in self._mappings.values()
                if mapping.get("status") in {"candidate", "review"}
            )
            return MockRecord({"total": total})
        if "SELECT * FROM market_mappings WHERE canonical_id" in query:
            cid = args[0]
            return MockRecord(self._mappings[cid]) if cid in self._mappings else None
        if "SELECT * FROM market_mappings" in query and "kalshi_market_id = $1 AND polymarket_slug = $2" in query:
            kalshi_id, poly_slug = args
            for mapping in self._mappings.values():
                if (
                    mapping.get("kalshi_market_id") == kalshi_id
                    and mapping.get("polymarket_slug") == poly_slug
                ):
                    return MockRecord(mapping)
            return None
        if "SELECT 1 FROM market_mappings WHERE canonical_id" in query:
            cid = args[0]
            return MockRecord({"exists": True}) if cid in self._mappings else None
        if "SELECT * FROM market_mappings WHERE kalshi_market_id" in query:
            kid = args[0]
            for m in self._mappings.values():
                if m.get("kalshi_market_id") == kid and kid:
                    return MockRecord(m)
            return None
        if "SELECT * FROM market_mappings WHERE polymarket_slug" in query:
            sid = args[0]
            for m in self._mappings.values():
                if m.get("polymarket_slug") == sid and sid:
                    return MockRecord(m)
            return None
        if "SELECT * FROM mapping_candidates WHERE id" in query:
            cid = args[0]
            return MockRecord(self._candidates[cid]) if cid in self._candidates else None
        # Handle INSERT...RETURNING for candidates — upsert directly in fetchrow
        if "INSERT INTO mapping_candidates" in query and "RETURNING" in query:
            c = {
                "id": self._next_cid,
                "canonical_id": args[0],
                "platform": args[1],
                "platform_market_id": args[2],
                "description": args[3],
                "match_score": float(args[4]) if args[4] else 0.0,
                "status": "pending",
                "reviewed_at": None,
                "reviewer_note": "",
                "created_at": utc_now(),
            }
            self._candidates[self._next_cid] = c
            self._next_cid += 1
            return MockRecord(c)
        return None

    async def fetch(self, query: str, *args):
        if "SELECT * FROM market_mappings WHERE status = $1 ORDER BY description LIMIT $2" in query:
            status_filter = args[0]
            limit = args[1] if len(args) > 1 else len(self._mappings)
            return [
                MockRecord(m)
                for m in sorted(self._mappings.values(), key=lambda item: item.get("description", ""))
                if m.get("status") == status_filter
            ][:limit]
        if "SELECT * FROM market_mappings ORDER BY description LIMIT" in query:
            limit = args[0] if args else len(self._mappings)
            return [
                MockRecord(m)
                for m in sorted(self._mappings.values(), key=lambda item: item.get("description", ""))
            ][:limit]
        if "SELECT * FROM market_mappings WHERE status = 'confirmed'" in query:
            require_auto = "allow_auto_trade = TRUE" in query
            results = [
                MockRecord(m) for m in self._mappings.values()
                if m.get("status") == "confirmed"
                and (not require_auto or m.get("allow_auto_trade"))
            ]
            return results
        if "SELECT * FROM mapping_candidates" in query and "WHERE status" in query.replace('\n', ' '):
            status_filter = args[0]
            limit = args[1] if len(args) > 1 else 50
            return [
                MockRecord(c) for c in self._candidates.values()
                if c["status"] == status_filter
            ][:limit]
        if "SELECT * FROM market_mappings WHERE LOWER(description)" in query:
            q = args[0].replace("%", "").lower()
            min_conf = float(args[1]) if len(args) > 1 else 0.3
            limit = int(args[2]) if len(args) > 2 else 20
            return [
                MockRecord(m) for m in self._mappings.values()
                if q in m.get("description", "").lower()
                and m.get("confidence", 0) >= min_conf
            ][:limit]
        if "SELECT * FROM market_mappings WHERE kalshi_market_id" in query:
            kid = args[0]
            return [
                MockRecord(m) for m in self._mappings.values()
                if m.get("kalshi_market_id") == kid and kid
            ]
        if "SELECT * FROM market_mappings WHERE polymarket_slug" in query:
            sid = args[0]
            return [
                MockRecord(m) for m in self._mappings.values()
                if m.get("polymarket_slug") == sid and sid
            ]
        return []

    def _build_mapping_dict(self, args) -> dict:
        payload = {
            "canonical_id": args[0],
            "description": args[1],
            "status": args[2],
            "allow_auto_trade": args[3],
            "aliases": list(args[4]) if args[4] else [],
            "tags": list(args[5]) if args[5] else [],
            "kalshi_market_id": args[6] or "",
            "polymarket_slug": args[7] or "",
            "polymarket_question": args[8] or "",
            "notes": args[9] or "",
            "review_note": args[10] or "",
            "mapping_score": float(args[11]) if args[11] else 0.0,
            "confidence": float(args[12]) if args[12] else 0.0,
            "expires_at": args[13],
            "last_validated_at": args[14],
            "created_at": args[15] if len(args) > 15 else utc_now(),
            "updated_at": utc_now(),
        }
        if len(args) > 17:
            payload["resolution_criteria"] = args[17]
        if len(args) > 18:
            payload["resolution_match_status"] = args[18]
        return payload


class MockPool:
    def __init__(self):
        self._conn = MockConn()

    async def acquire(self):
        return self._conn

    async def release(self, conn):
        pass

    async def close(self):
        pass


@pytest.fixture
def mock_pool(monkeypatch):
    pool = MockPool()

    async def fake_connect(self):
        self._pool = pool

    monkeypatch.setattr(MarketMappingStore, "connect", fake_connect)
    return pool


@pytest.mark.asyncio
async def test_upsert_and_get(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    mapping = MarketMapping(
        canonical_id="TEST-EVENT-001",
        description="Test market",
        status=MappingStatus.CONFIRMED,
        allow_auto_trade=True,
        kalshi_market_id="kalshi-123",
        polymarket_slug="poly-test-abc",
        mapping_score=0.85,
        confidence=0.80,
    )

    await store.upsert(mapping)
    retrieved = await store.get("TEST-EVENT-001")

    assert retrieved is not None
    assert retrieved.canonical_id == "TEST-EVENT-001"
    assert retrieved.status == MappingStatus.CONFIRMED
    assert retrieved.allow_auto_trade is True
    assert retrieved.kalshi_market_id == "kalshi-123"
    assert retrieved.polymarket_slug == "poly-test-abc"
    assert retrieved.mapping_score == 0.85

    await store.disconnect()


@pytest.mark.asyncio
async def test_iter_confirmed_filter(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    # Insert confirmed and candidate mappings
    for i in range(3):
        m = MarketMapping(
            canonical_id=f"CONFIRMED-{i}",
            description=f"Confirmed market {i}",
            status=MappingStatus.CONFIRMED,
            allow_auto_trade=(i == 0),
            kalshi_market_id=f"k-{i}",
        )
        await store.upsert(m)

    m_candidate = MarketMapping(
        canonical_id="CANDIDATE-1",
        description="Candidate market",
        status=MappingStatus.CANDIDATE,
        kalshi_market_id="k-cand",
    )
    await store.upsert(m_candidate)

    confirmed = [m async for _, m in store.iter_confirmed()]
    assert len(confirmed) == 3
    assert all(mm.status == MappingStatus.CONFIRMED for mm in confirmed)

    auto_trade = [m async for _, m in store.iter_confirmed(require_auto_trade=True)]
    assert len(auto_trade) == 1
    assert auto_trade[0].allow_auto_trade is True

    await store.disconnect()


@pytest.mark.asyncio
async def test_get_by_platform(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    m = MarketMapping(
        canonical_id="PLATFORM-TEST",
        description="Platform lookup test",
        status=MappingStatus.CONFIRMED,
        kalshi_market_id="kalshi-xyz",
        polymarket_slug="poly-xyz",
    )
    await store.upsert(m)

    by_kalshi = await store.get_by_platform("kalshi", "kalshi-xyz")
    assert by_kalshi is not None
    assert by_kalshi.canonical_id == "PLATFORM-TEST"

    by_poly = await store.get_by_platform("polymarket", "poly-xyz")
    assert by_poly is not None
    assert by_poly.canonical_id == "PLATFORM-TEST"

    missing = await store.get_by_platform("kalshi", "nonexistent")
    assert missing is None

    await store.disconnect()


@pytest.mark.asyncio
async def test_add_and_review_candidate(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    await store.add_candidate(
        canonical_id="NEW-CANON-001",
        platform="polymarket",
        platform_market_id="poly-candidate-abc",
        description="Detected candidate",
        match_score=0.72,
    )

    candidates = await store.get_candidates(status="pending")
    assert len(candidates) == 1
    assert candidates[0]["canonical_id"] == "NEW-CANON-001"
    assert candidates[0]["match_score"] == 0.72

    # Approve candidate
    await store.review_candidate(
        candidate_id=1,
        decision="approve",
        reviewer_note="Good match",
    )

    candidates_after = await store.get_candidates(status="pending")
    assert len(candidates_after) == 0

    # Verify mapping was created
    mapping = await store.get("NEW-CANON-001")
    assert mapping is not None

    await store.disconnect()


@pytest.mark.asyncio
async def test_delete_mapping(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    m = MarketMapping(
        canonical_id="DELETE-ME",
        description="Will be deleted",
        status=MappingStatus.CONFIRMED,
    )
    await store.upsert(m)

    assert await store.get("DELETE-ME") is not None
    await store.delete("DELETE-ME")
    assert await store.get("DELETE-ME") is None

    await store.disconnect()


@pytest.mark.asyncio
async def test_write_candidates_creates_candidate_mapping_without_auto_trade(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    written = await store.write_candidates([
        {
            "canonical_id": "AUTO-CAN-001",
            "kalshi_ticker": "KALSHI-123",
            "kalshi_title": "Will rates fall in June?",
            "poly_slug": "rates-fall-june",
            "poly_question": "Will rates fall in June?",
            "score": 0.91,
        }
    ])

    assert written == 1
    mapping = await store.get("AUTO-CAN-001")
    assert mapping is not None
    assert mapping.status == MappingStatus.CANDIDATE
    assert mapping.allow_auto_trade is False
    assert mapping.kalshi_market_id == "KALSHI-123"
    assert mapping.polymarket_slug == "rates-fall-june"

    await store.disconnect()


@pytest.mark.asyncio
async def test_write_candidates_keeps_confirmed_mapping_confirmed(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    confirmed = MarketMapping(
        canonical_id="CONF-001",
        description="Confirmed market",
        status=MappingStatus.CONFIRMED,
        allow_auto_trade=True,
        kalshi_market_id="KALSHI-123",
        polymarket_slug="rates-fall-june",
    )
    await store.upsert(confirmed)

    await store.write_candidates([
        {
            "kalshi_ticker": "KALSHI-123",
            "kalshi_title": "Will rates fall in June?",
            "poly_slug": "rates-fall-june",
            "poly_question": "Will rates fall in June?",
            "score": 0.97,
        }
    ])

    mapping = await store.get("CONF-001")
    assert mapping is not None
    assert mapping.status == MappingStatus.CONFIRMED
    assert mapping.allow_auto_trade is True
    assert mapping.mapping_score == 0.97

    await store.disconnect()


@pytest.mark.asyncio
async def test_write_candidates_preserves_auto_promoted_mapping_state(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    written = await store.write_candidates([
        {
            "canonical_id": "AUTO-PROMOTE-001",
            "kalshi_ticker": "CONTROLH-2026-D",
            "kalshi_title": "Will Democrats win the House in 2026?",
            "poly_slug": "paccc-usho-midterms-2026-11-03-dem",
            "poly_question": "Will Democrats win the House in 2026?",
            "score": 0.93,
            "status": "confirmed",
            "allow_auto_trade": True,
            "resolution_match_status": "identical",
            "resolution_criteria": {
                "kalshi": {"source": "AP", "rule": None, "settlement_date": "2027-02-01"},
                "polymarket": {"source": "AP", "rule": None, "settlement_date": "2027-02-01"},
                "criteria_match": "identical",
                "operator_note": "Auto-promoted in test.",
            },
            "notes": "Auto-promoted in test.",
        }
    ])

    assert written == 1
    mapping = await store.get("AUTO-PROMOTE-001")
    assert mapping is not None
    assert mapping.status == MappingStatus.CONFIRMED
    assert mapping.allow_auto_trade is True
    assert mapping.resolution_match_status == "identical"
    assert "Auto-promoted" in mapping.notes

    await store.disconnect()


@pytest.mark.asyncio
async def test_sync_candidates_expires_stale_auto_discovered_pairs(mock_pool, monkeypatch):
    store = MarketMappingStore("postgres://mock/mock")
    await store.connect()

    await store.write_candidates([
        {
            "canonical_id": "AUTO-CAN-OLD",
            "kalshi_ticker": "KALSHI-OLD",
            "kalshi_title": "Old candidate",
            "poly_slug": "poly-old",
            "poly_question": "Old candidate",
            "score": 0.71,
        }
    ])

    written = await store.sync_candidates([
        {
            "canonical_id": "AUTO-CAN-NEW",
            "kalshi_ticker": "KALSHI-NEW",
            "kalshi_title": "New candidate",
            "poly_slug": "poly-new",
            "poly_question": "New candidate",
            "score": 0.92,
        }
    ])

    assert written == 1
    old_mapping = await store.get("AUTO-CAN-OLD")
    new_mapping = await store.get("AUTO-CAN-NEW")
    assert old_mapping is not None
    assert old_mapping.status == MappingStatus.EXPIRED
    assert new_mapping is not None
    assert new_mapping.status == MappingStatus.CANDIDATE

    await store.disconnect()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
