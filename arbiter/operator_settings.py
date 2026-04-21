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
