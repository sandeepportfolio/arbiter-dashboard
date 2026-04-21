import os
from arbiter.config.settings import load_config, PolymarketConfig, PolymarketUSConfig


def test_variant_defaults_to_us(monkeypatch):
    monkeypatch.delenv("POLYMARKET_VARIANT", raising=False)
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketUSConfig)


def test_variant_legacy_returns_legacy_class(monkeypatch):
    monkeypatch.setenv("POLYMARKET_VARIANT", "legacy")
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "0" * 64)
    monkeypatch.setenv("POLY_FUNDER", "0x" + "1" * 40)
    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketConfig)


def test_variant_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")
    cfg = load_config()
    assert cfg.polymarket is None


def test_variant_us_picks_up_key_id(monkeypatch):
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "my-key-id-123")
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketUSConfig)
    assert cfg.polymarket.api_key_id == "my-key-id-123"
