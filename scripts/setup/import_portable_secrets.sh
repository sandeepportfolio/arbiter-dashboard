#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
DEFAULT_INPUT="$REPO_ROOT/portable-secrets/arbiter-portable-secrets.tgz.enc"
INPUT_PATH=${1:-$DEFAULT_INPUT}
BACKUP_ROOT="$REPO_ROOT/portable-secrets/backups/$(date '+%Y%m%d-%H%M%S')"

usage() {
  cat <<'EOF'
Usage: ./scripts/setup/import_portable_secrets.sh [bundle-path]

Restores an encrypted Arbiter portability bundle into this repo checkout.
Existing local secret files are backed up first under portable-secrets/backups/.

Passphrase handling:
- preferred: export PORTABLE_SECRETS_PASSPHRASE='your-passphrase'
- fallback: the script will prompt interactively

Default input:
  portable-secrets/arbiter-portable-secrets.tgz.enc
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "$INPUT_PATH" ]]; then
  echo "Bundle not found: $INPUT_PATH" >&2
  exit 1
fi

if [[ -z "${PORTABLE_SECRETS_PASSPHRASE:-}" ]]; then
  read -r -s -p "Bundle passphrase: " pass1
  echo
  export PORTABLE_SECRETS_PASSPHRASE="$pass1"
  unset pass1
fi

staging_dir=$(mktemp -d)
archive_path="$staging_dir/arbiter-portable-secrets.tgz"
extract_dir="$staging_dir/extracted"
trap 'rm -rf "$staging_dir"' EXIT
mkdir -p "$extract_dir"

openssl enc -d -aes-256-cbc -pbkdf2 -iter 250000 \
  -in "$INPUT_PATH" \
  -out "$archive_path" \
  -pass env:PORTABLE_SECRETS_PASSPHRASE

(
  cd "$extract_dir"
  tar -xzf "$archive_path"
)

mapfile -t restored_files < <(cd "$extract_dir" && find . -type f ! -name manifest.txt | sed 's#^./##' | sort)

if [[ ${#restored_files[@]} -eq 0 ]]; then
  echo "Bundle decrypted, but no files were found inside." >&2
  exit 1
fi

made_backup=0
for rel in "${restored_files[@]}"; do
  target="$REPO_ROOT/$rel"
  if [[ -f "$target" ]]; then
    mkdir -p "$BACKUP_ROOT/$(dirname -- "$rel")"
    cp "$target" "$BACKUP_ROOT/$rel"
    made_backup=1
  fi
  mkdir -p "$(dirname -- "$target")"
  cp "$extract_dir/$rel" "$target"
  chmod 600 "$target"
done

if [[ $made_backup -eq 1 ]]; then
  echo "Backed up replaced files under: $BACKUP_ROOT"
else
  echo "No existing local secret files needed backup."
fi

echo "Restored files:"
printf ' - %s\n' "${restored_files[@]}"

echo "Next recommended checks:"
echo "  ./.venv/bin/python scripts/setup/validate_env.py"
echo "  ./.venv/bin/python scripts/setup/check_kalshi_auth.py"
echo "  ./.venv/bin/python scripts/setup/check_polymarket_us.py"
echo "  ./.venv/bin/python scripts/setup/check_telegram.py"
