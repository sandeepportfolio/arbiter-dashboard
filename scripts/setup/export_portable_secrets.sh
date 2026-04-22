#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
DEFAULT_OUTPUT="$REPO_ROOT/portable-secrets/arbiter-portable-secrets.tgz.enc"
OUTPUT_PATH=${1:-$DEFAULT_OUTPUT}

FILES=(
  ".env.production"
  ".env"
  "keys/kalshi_private.pem"
)

usage() {
  cat <<'EOF'
Usage: ./scripts/setup/export_portable_secrets.sh [output-path]

Creates an encrypted portability bundle containing the local secret files
needed to bring Arbiter up on another machine.

Passphrase handling:
- preferred: export PORTABLE_SECRETS_PASSPHRASE='your-passphrase'
- fallback: the script will prompt interactively

Default output:
  portable-secrets/arbiter-portable-secrets.tgz.enc
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

mkdir -p "$(dirname -- "$OUTPUT_PATH")"

if [[ -z "${PORTABLE_SECRETS_PASSPHRASE:-}" ]]; then
  read -r -s -p "Bundle passphrase: " pass1
  echo
  read -r -s -p "Confirm passphrase: " pass2
  echo
  if [[ "$pass1" != "$pass2" ]]; then
    echo "Passphrases did not match" >&2
    exit 1
  fi
  export PORTABLE_SECRETS_PASSPHRASE="$pass1"
  unset pass1 pass2
fi

staging_dir=$(mktemp -d)
archive_path="$staging_dir/arbiter-portable-secrets.tgz"
manifest_path="$staging_dir/manifest.txt"
trap 'rm -rf "$staging_dir"' EXIT

included=()
for rel in "${FILES[@]}"; do
  src="$REPO_ROOT/$rel"
  if [[ -f "$src" ]]; then
    dest_dir="$staging_dir/$(dirname -- "$rel")"
    mkdir -p "$dest_dir"
    cp "$src" "$staging_dir/$rel"
    included+=("$rel")
  fi
done

if [[ ${#included[@]} -eq 0 ]]; then
  echo "No supported secret files were found to export." >&2
  exit 1
fi

{
  echo "Arbiter portable secrets bundle"
  echo "Created: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "Repo root: $REPO_ROOT"
  echo "Included files:"
  for rel in "${included[@]}"; do
    echo "- $rel"
  done
  echo
  echo "This archive is encrypted with OpenSSL AES-256-CBC + PBKDF2."
} > "$manifest_path"

(
  cd "$staging_dir"
  tar -czf "$archive_path" manifest.txt "${included[@]}"
)

openssl enc -aes-256-cbc -pbkdf2 -iter 250000 -salt \
  -in "$archive_path" \
  -out "$OUTPUT_PATH" \
  -pass env:PORTABLE_SECRETS_PASSPHRASE

chmod 600 "$OUTPUT_PATH"

echo "Encrypted bundle written to: $OUTPUT_PATH"
echo "Included files:"
printf ' - %s\n' "${included[@]}"
echo "Keep the passphrase out of git and out of the bundle."
