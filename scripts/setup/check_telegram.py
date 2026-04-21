"""check_telegram.py — thin wrapper around `python -m arbiter.notifiers.telegram`.

Exists so go_live.sh can call every validator with the same convention
(`python scripts/setup/check_*.py`).

Exit 0 on successful dry-test, 1 on disabled/failed, 2 on exception.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from arbiter.notifiers.telegram import main as telegram_main  # type: ignore

if __name__ == "__main__":
    sys.exit(telegram_main())
