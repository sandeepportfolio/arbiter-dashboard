"""
Smoke test: load .env.production.template with placeholder values replaced by
dummy values, then call load_config() and assert it does not raise.
"""
import os
import re
from pathlib import Path

import pytest

from arbiter.config.settings import load_config, PolymarketUSConfig


def _load_template_as_env(monkeypatch):
    """Parse .env.production.template and inject all key=value pairs into the
    environment, replacing <...> placeholder values with safe dummy values."""
    template_path = Path(__file__).resolve().parent.parent.parent / ".env.production.template"
    assert template_path.exists(), f"Template not found: {template_path}"

    for line in template_path.read_text().splitlines():
        # Skip comments and blank lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        # Replace <placeholder> tokens with safe dummy values
        value = re.sub(r"<[^>]+>", "dummy_value", value)
        monkeypatch.setenv(key, value)


def test_template_loads_without_raising(monkeypatch):
    """Parsing the template and calling load_config() must not raise any exception."""
    _load_template_as_env(monkeypatch)
    # Force US variant (the new default in the template)
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    cfg = load_config()
    assert cfg is not None
    assert isinstance(cfg.polymarket, PolymarketUSConfig)


def test_template_legacy_variant_loads(monkeypatch):
    """Switching to legacy variant via env must also not raise."""
    _load_template_as_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_VARIANT", "legacy")
    cfg = load_config()
    assert cfg is not None


def test_template_disabled_variant_loads(monkeypatch):
    """Disabled variant must also not raise and yields None polymarket config."""
    _load_template_as_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")
    cfg = load_config()
    assert cfg is not None
    assert cfg.polymarket is None
