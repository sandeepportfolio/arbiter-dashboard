#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_DIR="${ARBITER_VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_VERSION="${ARBITER_PYTHON_VERSION:-3.12}"
REQ_FILE="$ROOT_DIR/requirements-dev.txt"

if [[ ! -f "$REQ_FILE" ]]; then
  echo "requirements file missing: $REQ_FILE" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  echo "[bootstrap-python] creating virtualenv with uv (Python $PYTHON_VERSION)"
  uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
  echo "[bootstrap-python] installing Python dependencies from $REQ_FILE"
  uv pip install --python "$VENV_DIR/bin/python" -r "$REQ_FILE"
elif command -v python3.12 >/dev/null 2>&1; then
  echo "[bootstrap-python] creating virtualenv with python3.12"
  python3.12 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r "$REQ_FILE"
else
  cat >&2 <<'EOF'
[bootstrap-python] need either:
  - uv (recommended), or
  - python3.12 on PATH
EOF
  exit 1
fi

"$VENV_DIR/bin/python" --version
echo "[bootstrap-python] ready: $VENV_DIR/bin/python -m pytest -q arbiter"
