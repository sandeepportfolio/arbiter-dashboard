"""Tests for the settled-mapping auto-expire sweep."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import List, Optional, Tuple

import pytest

from arbiter.mapping.expire_settled import (
    expire_settled_confirmed_mappings,
    parse_settled_date,
)
from arbiter.mapping.market_map import MappingStatus


# ─── parse_settled_date ────────────────────────────────────────────────────

class TestParseSettledDate:
    def test_recognises_mlb_game_pattern(self):
        assert parse_settled_date("GAME_MLB_20260516_ARI_a029e71c") == date(2026, 5, 16)

    def test_recognises_mls_three_outcome_pattern(self):
        assert parse_settled_date("GAME_MLS_20260516_TIE_eee5c5c4") == date(2026, 5, 16)

    def test_returns_none_for_election_pattern_without_full_date(self):
        # GOP_HOUSE_2026 carries only a year — not enough to safely expire.
        assert parse_settled_date("GOP_HOUSE_2026") is None

    def test_returns_none_for_crypto_strike_pattern(self):
        # CRYPT_D_26APR2207_T... uses a YYMMMDDHH-ish token, not YYYYMMDD.
        assert parse_settled_date("CRYPT_D_26APR2207_T85799_99") is None

    def test_returns_none_for_invalid_calendar_date(self):
        # 13th month is not a real date — strptime raises, we return None.
        assert parse_settled_date("GAME_MLB_20261301_FOO_abc") is None

    def test_returns_none_for_empty_string(self):
        assert parse_settled_date("") is None

    def test_returns_none_when_date_token_is_followed_by_non_letter(self):
        # The pattern requires an underscore + uppercase letter after the
        # 8-digit run so we don't false-match ids like "FOO_BAR_12345678_99".
        assert parse_settled_date("FOO_BAR_12345678_99") is None


# ─── expire_settled_confirmed_mappings ────────────────────────────────────

class _FakeStore:
    """Minimal in-memory store that mimics MarketMappingStore.iter_confirmed
    and update_status for unit-test purposes only."""

    def __init__(self, confirmed_ids: List[str]):
        self._confirmed_ids = confirmed_ids
        self.updates: List[Tuple[str, MappingStatus, str, Optional[bool]]] = []

    async def iter_confirmed(self, require_auto_trade: bool = False):
        for canonical_id in self._confirmed_ids:
            yield canonical_id, None  # mapping payload is unused by the sweep

    async def update_status(
        self,
        canonical_id: str,
        status: MappingStatus,
        review_note: str = "",
        allow_auto_trade: Optional[bool] = None,
    ):
        self.updates.append((canonical_id, status, review_note, allow_auto_trade))
        return None


class TestExpireSweep:
    def test_demotes_only_past_dated_mappings(self):
        store = _FakeStore([
            "GAME_MLB_20260516_ARI_a",   # past — should be expired
            "GAME_MLB_20260524_BOS_b",   # future — leave alone
            "GAME_MLB_20260523_LAD_c",   # today — leave alone
            "GOP_HOUSE_2026",            # not date-bearing — leave alone
        ])

        expired = asyncio.run(expire_settled_confirmed_mappings(
            store, today=date(2026, 5, 23),
        ))

        assert expired == ["GAME_MLB_20260516_ARI_a"]
        assert len(store.updates) == 1
        cid, status, note, allow = store.updates[0]
        assert cid == "GAME_MLB_20260516_ARI_a"
        assert status == MappingStatus.EXPIRED
        assert allow is False
        assert "2026-05-16" in note
        assert "2026-05-23" in note

    def test_today_dated_mappings_are_not_expired(self):
        """Games scheduled today are either still playing or just finished
        but not yet settled by the exchange — leave them confirmed."""
        store = _FakeStore(["GAME_MLB_20260523_LAD_c"])
        expired = asyncio.run(expire_settled_confirmed_mappings(
            store, today=date(2026, 5, 23),
        ))
        assert expired == []
        assert store.updates == []

    def test_empty_store_returns_empty_list(self):
        store = _FakeStore([])
        expired = asyncio.run(expire_settled_confirmed_mappings(
            store, today=date(2026, 5, 23),
        ))
        assert expired == []

    def test_continues_after_per_mapping_update_failure(self):
        """A failure on one row must not abort the whole sweep — every
        other settled-date mapping must still get demoted in the same pass."""

        class _FlakeyStore(_FakeStore):
            def __init__(self, ids):
                super().__init__(ids)
                self.calls = 0

            async def update_status(
                self,
                canonical_id,
                status,
                review_note="",
                allow_auto_trade=None,
            ):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("simulated db blip")
                return await super().update_status(
                    canonical_id, status, review_note, allow_auto_trade,
                )

        store = _FlakeyStore([
            "GAME_MLB_20260516_ARI_a",
            "GAME_MLB_20260517_BOS_b",
        ])
        expired = asyncio.run(expire_settled_confirmed_mappings(
            store, today=date(2026, 5, 23),
        ))
        # First update raised; second succeeded — only the second is reported.
        assert expired == ["GAME_MLB_20260517_BOS_b"]
        assert [u[0] for u in store.updates] == ["GAME_MLB_20260517_BOS_b"]
