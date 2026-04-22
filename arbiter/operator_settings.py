from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

log = logging.getLogger("arbiter.operator_settings")


def default_operator_settings_path() -> Path:
    raw = os.getenv("ARBITER_OPERATOR_SETTINGS_PATH", "~/.arbiter/operator-settings.json")
    return Path(raw).expanduser()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_float(value: Any, default: float, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(number, minimum)


def _coerce_int(value: Any, default: int, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(number, minimum)


def default_market_discovery_settings() -> dict[str, Any]:
    return {
        "auto_discovery_enabled": _env_bool("AUTO_DISCOVERY_ENABLED", True),
        "auto_discovery_interval_seconds": _coerce_float(os.getenv("AUTO_DISCOVERY_INTERVAL_S", "300"), 300.0, 15.0),
        "auto_discovery_budget_rps": _coerce_float(os.getenv("AUTO_DISCOVERY_BUDGET_RPS", "2.0"), 2.0, 0.1),
        "auto_discovery_min_score": _coerce_float(os.getenv("AUTO_DISCOVERY_MIN_SCORE", "0.25"), 0.25, 0.0),
        "auto_discovery_max_candidates": _coerce_int(os.getenv("AUTO_DISCOVERY_MAX_CANDIDATES", "500"), 500, 1),
        "auto_promote_enabled": _env_bool("AUTO_PROMOTE_ENABLED", False),
        "auto_promote_min_score": _coerce_float(os.getenv("AUTO_PROMOTE_MIN_SCORE", "0.78"), 0.78, 0.0),
        "auto_promote_daily_cap": _coerce_int(os.getenv("AUTO_PROMOTE_DAILY_CAP", "250"), 250, 1),
        "auto_promote_advisory_scans": _coerce_int(os.getenv("AUTO_PROMOTE_ADVISORY_SCANS", "0"), 0, 0),
        "auto_promote_max_days": _coerce_int(os.getenv("AUTO_PROMOTE_MAX_DAYS", "400"), 400, 1),
    }


def load_market_discovery_settings(store: "OperatorSettingsStore | None" = None) -> dict[str, Any]:
    settings = default_market_discovery_settings()
    payload = store.load() if store is not None else {}
    raw_settings = payload.get("settings") if isinstance(payload, dict) else None
    mapping = raw_settings.get("mapping") if isinstance(raw_settings, dict) else None
    if not isinstance(mapping, dict):
        return settings

    settings["auto_discovery_enabled"] = _coerce_bool(
        mapping.get("auto_discovery_enabled"), settings["auto_discovery_enabled"]
    )
    settings["auto_discovery_interval_seconds"] = _coerce_float(
        mapping.get("auto_discovery_interval_seconds"), settings["auto_discovery_interval_seconds"], 15.0
    )
    settings["auto_discovery_budget_rps"] = _coerce_float(
        mapping.get("auto_discovery_budget_rps"), settings["auto_discovery_budget_rps"], 0.1
    )
    settings["auto_discovery_min_score"] = _coerce_float(
        mapping.get("auto_discovery_min_score"), settings["auto_discovery_min_score"], 0.0
    )
    settings["auto_discovery_max_candidates"] = _coerce_int(
        mapping.get("auto_discovery_max_candidates"), settings["auto_discovery_max_candidates"], 1
    )
    settings["auto_promote_enabled"] = _coerce_bool(
        mapping.get("auto_promote_enabled"), settings["auto_promote_enabled"]
    )
    settings["auto_promote_min_score"] = _coerce_float(
        mapping.get("auto_promote_min_score"), settings["auto_promote_min_score"], 0.0
    )
    settings["auto_promote_daily_cap"] = _coerce_int(
        mapping.get("auto_promote_daily_cap"), settings["auto_promote_daily_cap"], 1
    )
    settings["auto_promote_advisory_scans"] = _coerce_int(
        mapping.get("auto_promote_advisory_scans"), settings["auto_promote_advisory_scans"], 0
    )
    settings["auto_promote_max_days"] = _coerce_int(
        mapping.get("auto_promote_max_days"), settings["auto_promote_max_days"], 1
    )
    return settings


class OperatorSettingsStore:
    """Small JSON-backed store for operator-editable runtime settings.

    The store persists only non-secret runtime knobs that are safe to edit from
    the dashboard. Secrets and environment bootstrap remain outside this file.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_operator_settings_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - corrupted settings should not crash the app
            log.warning("operator_settings.load_failed path=%s err=%s", self.path, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, settings: dict[str, Any], *, updated_by: str | None = None) -> dict[str, Any]:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "updated_by": (updated_by or "system").strip() or "system",
            "settings": deepcopy(settings),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)
        return payload
