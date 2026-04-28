"""
Validate mapping integrity — ensures no mismatched markets can reach trading.

The #1 risk in prediction market arbitrage is pairing different events together
(e.g., soccer + NBA). These tests verify every active mapping is correct.
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.config.settings import MARKET_MAP, MARKET_SEEDS


class TestMappingIntegrity:
    """Every mapping in MARKET_MAP must have consistent fields."""

    def test_all_confirmed_have_resolution_status(self):
        """Confirmed mappings with allow_auto_trade must have resolution_match_status=identical."""
        for cid, mapping in MARKET_MAP.items():
            if mapping.get("status") == "confirmed" and mapping.get("allow_auto_trade"):
                res = mapping.get("resolution_match_status", "missing")
                assert res == "identical", (
                    f"Mapping {cid} is confirmed+auto_trade but "
                    f"resolution_match_status={res} (must be 'identical')"
                )

    def test_no_disabled_seeds_in_active_map(self):
        """Seeds marked disabled should not be in the active trading map."""
        for cid, mapping in MARKET_MAP.items():
            if mapping.get("status") == "disabled":
                assert not mapping.get("allow_auto_trade", False), (
                    f"Disabled mapping {cid} has allow_auto_trade=True!"
                )

    def test_candidate_mappings_not_auto_tradable(self):
        """Candidate (unverified) mappings must not be auto-tradable."""
        for cid, mapping in MARKET_MAP.items():
            if mapping.get("status") == "candidate":
                assert not mapping.get("allow_auto_trade", False), (
                    f"Candidate mapping {cid} has allow_auto_trade=True! "
                    f"Only confirmed mappings should be tradable."
                )

    def test_hand_curated_seeds_have_both_platforms(self):
        """Hand-curated MARKET_SEEDS must specify both kalshi and polymarket tickers."""
        for seed in MARKET_SEEDS:
            cid = seed.canonical_id if hasattr(seed, 'canonical_id') else seed.get("canonical_id", "unknown")
            kalshi = seed.kalshi if hasattr(seed, 'kalshi') else seed.get("kalshi")
            poly = seed.polymarket if hasattr(seed, 'polymarket') else seed.get("polymarket")
            assert kalshi, f"Seed {cid} missing kalshi ticker"
            assert poly, f"Seed {cid} missing polymarket ticker"

    def test_no_duplicate_canonical_ids(self):
        """No duplicate canonical_id values in MARKET_SEEDS."""
        ids = [s.canonical_id if hasattr(s, 'canonical_id') else s.get("canonical_id") for s in MARKET_SEEDS]
        assert len(ids) == len(set(ids)), f"Duplicate canonical_ids: {[x for x in ids if ids.count(x) > 1]}"


class TestAutoSeedSafety:
    """Verify auto-loaded seeds can't bypass safety gates."""

    def test_auto_seeds_default_candidate(self):
        """All auto-loaded seeds should default to status=candidate."""
        for cid, mapping in MARKET_MAP.items():
            if cid.startswith("AUTO_"):
                assert mapping.get("status") in ("candidate", "disabled"), (
                    f"Auto-seed {cid} has status={mapping.get('status')}, "
                    f"expected 'candidate' or 'disabled'"
                )

    def test_auto_seeds_no_auto_trade(self):
        """Auto-loaded seeds must not have allow_auto_trade=True by default."""
        for cid, mapping in MARKET_MAP.items():
            if cid.startswith("AUTO_") and mapping.get("status") == "candidate":
                assert not mapping.get("allow_auto_trade", False), (
                    f"Auto-seed {cid} has allow_auto_trade=True without being promoted!"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
