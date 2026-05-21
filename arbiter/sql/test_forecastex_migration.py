"""
Static checks on the ForecastEx schema migration.

We can't run real Postgres in CI, but we can verify the migration text is
idempotent (uses IF NOT EXISTS), defaults the column safely so existing
two-platform rows keep working, and is present in BOTH the SQL bootstrap
file and the Python-embedded SCHEMA_SQL that runs at startup.
"""
from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = PROJECT_ROOT / "arbiter" / "sql" / "init.sql"
MARKET_MAP_PY = PROJECT_ROOT / "arbiter" / "mapping" / "market_map.py"


def test_init_sql_has_idempotent_forecastex_alter():
    text = INIT_SQL.read_text()
    assert "ADD COLUMN IF NOT EXISTS forecastex_contract_id" in text, (
        "init.sql must use IF NOT EXISTS for the ForecastEx column so re-running "
        "against an upgraded DB is a no-op."
    )
    # Default '' keeps two-platform rows valid (NOT NULL safe).
    assert "forecastex_contract_id VARCHAR(100) DEFAULT ''" in text


def test_init_sql_creates_forecastex_index_when_set():
    text = INIT_SQL.read_text()
    assert "idx_mappings_forecastex" in text
    # Partial index — only over non-blank rows so two-platform mappings
    # don't bloat it.
    assert "WHERE forecastex_contract_id != ''" in text


def test_python_schema_includes_forecastex_column():
    """The Python SCHEMA_SQL string embedded in market_map.py runs as part
    of the startup migration. Must match init.sql so a fresh DB and an
    upgraded DB end up with the same schema."""
    text = MARKET_MAP_PY.read_text()
    assert "ADD COLUMN IF NOT EXISTS forecastex_contract_id" in text
    assert "idx_mappings_forecastex" in text


def test_create_table_includes_forecastex_column():
    """Fresh installs must also see the column."""
    text = MARKET_MAP_PY.read_text()
    # The CREATE TABLE statement also lists it for fresh deployments.
    assert "forecastex_contract_id VARCHAR(100) DEFAULT ''" in text
