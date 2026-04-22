from pathlib import Path

import pytest

from arbiter.config import settings


def test_project_dotenv_prefers_repo_root(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    repo_env = repo_root / ".env"
    repo_env.write_text("KALSHI_API_KEY_ID=test-key\n", encoding="utf-8")
    package_env = repo_root / "arbiter" / ".env"
    package_env.write_text("KALSHI_API_KEY_ID=wrong-key\n", encoding="utf-8")

    loaded = []

    monkeypatch.setattr(settings, "load_dotenv", lambda path, override=True: loaded.append(Path(path)))

    result = settings._load_project_dotenv(package_dir / "settings.py")

    assert result == repo_env
    assert loaded == [repo_env]


def test_project_dotenv_falls_back_to_package_root(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    package_env = repo_root / "arbiter" / ".env"
    package_env.write_text("KALSHI_API_KEY_ID=test-key\n", encoding="utf-8")

    loaded = []

    monkeypatch.setattr(settings, "load_dotenv", lambda path, override=True: loaded.append(Path(path)))

    result = settings._load_project_dotenv(package_dir / "settings.py")

    assert result == package_env
    assert loaded == [package_env]


def test_project_dotenv_explicit_env_file_overrides_defaults(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    repo_prod_env = repo_root / ".env.production"
    repo_prod_env.write_text("DATABASE_URL=postgresql://arbiter:secret@localhost:5432/arbiter_live\n", encoding="utf-8")

    loaded = []

    monkeypatch.setattr(settings, "load_dotenv", lambda path, override=True: loaded.append(Path(path)))
    monkeypatch.setenv("ARBITER_ENV_FILE", str(repo_prod_env))

    result = settings._load_project_dotenv(package_dir / "settings.py")

    assert result == repo_prod_env
    assert loaded == [repo_prod_env]


def test_load_config_resolves_kalshi_key_relative_to_loaded_dotenv(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    monkeypatch.setattr(settings, "_DOTENV_PATH", repo_root / ".env")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private.pem")

    cfg = settings.load_config()

    assert cfg.kalshi.private_key_path == str((repo_root / "keys" / "kalshi_private.pem").resolve())


# ─── SAFE-06: resolution_criteria schema on MarketMappingRecord + update helper ──


def test_resolution_criteria_optional_when_missing():
    """MARKET_MAP entries built without resolution_criteria serialize to
    dicts where the key is either absent or None — consumers must not raise
    KeyError when reading it back (Pitfall 6).
    """
    from arbiter.config.settings import MarketMappingRecord

    record = MarketMappingRecord(
        canonical_id="X_TEST",
        description="Optional-field smoke test",
        status="candidate",
        kalshi="K1",
        polymarket="P1",
    )
    payload = record.to_dict()
    # Backward-compatible read — default-to-None is expected.
    assert payload.get("resolution_criteria") is None
    # resolution_match_status defaults to pending_operator_review.
    assert payload.get("resolution_match_status") == "pending_operator_review"
    # Record attributes expose the raw fields too.
    assert record.resolution_criteria is None
    assert record.resolution_match_status == "pending_operator_review"


def test_resolution_criteria_accepted_when_present():
    """MarketMappingRecord stores the structured criteria payload and
    round-trips it through .to_dict() unchanged.
    """
    from arbiter.config.settings import MarketMappingRecord

    criteria = {
        "kalshi": {
            "source": "https://kalshi.com/markets/KXTEST",
            "rule": "Resolves YES on the certified outcome",
            "settlement_date": "2029-01-06",
        },
        "polymarket": {
            "source": "https://polymarket.com/event/test",
            "rule": "Resolves YES on the inauguration outcome",
            "settlement_date": "2029-01-20",
        },
        "criteria_match": "pending_operator_review",
        "operator_note": "",
    }
    record = MarketMappingRecord(
        canonical_id="X_TEST",
        description="Resolution criteria smoke test",
        status="candidate",
        kalshi="K1",
        polymarket="P1",
        resolution_criteria=criteria,
    )
    payload = record.to_dict()
    assert payload["resolution_criteria"] == criteria
    assert payload["resolution_criteria"]["criteria_match"] == "pending_operator_review"


def test_update_market_mapping_accepts_resolution_criteria():
    """update_market_mapping accepts resolution_criteria kwarg; returns
    mapping dict exposing both 'resolution_criteria' and
    'resolution_match_status' top-level keys.
    """
    from arbiter.config.settings import MARKET_MAP, update_market_mapping

    if not MARKET_MAP:
        pytest.skip("No seed mappings")
    canonical_id = next(iter(MARKET_MAP.keys()))

    criteria = {
        "kalshi": {"rule": "A"},
        "polymarket": {"rule": "B"},
        "criteria_match": "divergent",
        "operator_note": "rules differ by 14 days",
    }
    result = update_market_mapping(canonical_id, resolution_criteria=criteria)
    assert result is not None
    assert result["resolution_criteria"] == criteria
    assert result["resolution_criteria"]["criteria_match"] == "divergent"
    # Top-level status mirror (plan truth: resolution_match_status is exposed).
    assert result.get("resolution_match_status") == "divergent"


def test_update_market_mapping_rejects_invalid_criteria_match():
    """Task 1 behavior: API handler validates criteria_match enum; the
    settings helper accepts whatever the caller sends (validation is a trust
    boundary at the API). Keep this test minimal — confirm the helper does
    not crash on exotic values; the API test enforces 400-rejection.
    """
    from arbiter.config.settings import MARKET_MAP, update_market_mapping

    if not MARKET_MAP:
        pytest.skip("No seed mappings")
    canonical_id = next(iter(MARKET_MAP.keys()))
    # Helper does not validate — it is purely a persistence hook. The API
    # layer is what rejects bad enums.
    result = update_market_mapping(
        canonical_id,
        resolution_criteria={"criteria_match": "pending_operator_review"},
    )
    assert result is not None
    assert result["resolution_match_status"] == "pending_operator_review"


def test_update_market_mapping_explicit_resolution_match_status():
    """Explicit resolution_match_status kwarg wins over criteria_match mirror."""
    from arbiter.config.settings import MARKET_MAP, update_market_mapping

    if not MARKET_MAP:
        pytest.skip("No seed mappings")
    canonical_id = next(iter(MARKET_MAP.keys()))
    result = update_market_mapping(
        canonical_id,
        resolution_criteria={"criteria_match": "similar"},
        resolution_match_status="identical",
    )
    assert result is not None
    assert result["resolution_match_status"] == "identical"
