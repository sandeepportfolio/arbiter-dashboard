#!/usr/bin/env python3
"""Generate the cryptographic material for IBKR **Web API OAuth 1.0a**
(first-party / self-service) — the genuinely zero-touch "API keys" path that
needs no gateway process and no recurring login.

This script ONLY generates local key files and prints what to paste into the
IBKR self-service registration portal. It never transmits anything, never
touches your account, and never places an order. Run it once; then complete
the one-time registration described in deploy/ib-gateway/RUNBOOK.md (§ Path B).

Outputs (all written to a GITIGNORED dir, default deploy/ib-gateway/oauth/):
  private_signature.pem   RSA-2048 — signs OAuth requests (RSA-SHA256). SECRET.
  public_signature.pem    register this in the IBKR portal.
  private_encryption.pem  RSA-2048 — decrypts the access-token secret. SECRET.
  public_encryption.pem   register this in the IBKR portal.
  dhparam.pem             Diffie-Hellman params for the live-session-token (LST)
                          handshake. register this in the IBKR portal.

After IBKR processes the registration (allow ~1-2 weeks; activation happens on
a weekend) you receive a ~9-char **consumer key**. Put it + the private key
paths into .env.production (see RUNBOOK). The bot then derives a ~24h live
session token machine-to-machine — no browser, no 2FA, no weekly tap.

Usage:
    python scripts/ibkr_oauth_setup.py [--out deploy/ib-gateway/oauth] [--force]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Files we generate and whether each is a secret (private) or shareable (public
# / params that get registered in the portal).
_FILES = ("private_signature.pem", "public_signature.pem",
          "private_encryption.pem", "public_encryption.pem", "dhparam.pem")


def _run(cmd: list[str]) -> None:
    """Run an openssl command, surfacing a clear error on failure."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("error: `openssl` not found on PATH — install OpenSSL first.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"error: {' '.join(cmd)}\n{exc.stderr}")


def generate(out_dir: Path, force: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = [f for f in _FILES if (out_dir / f).exists()]
    if existing and not force:
        sys.exit(
            f"refusing to overwrite existing key material in {out_dir} "
            f"({', '.join(existing)}). Re-run with --force to regenerate "
            "(this INVALIDATES any registration already tied to these keys)."
        )

    sig_priv = out_dir / "private_signature.pem"
    sig_pub = out_dir / "public_signature.pem"
    enc_priv = out_dir / "private_encryption.pem"
    enc_pub = out_dir / "public_encryption.pem"
    dh = out_dir / "dhparam.pem"

    # RSA-2048 signing + encryption keypairs (IBKR OAuth 1.0a uses RSA-SHA256
    # request signing and an RSA-encrypted access-token secret).
    _run(["openssl", "genrsa", "-out", str(sig_priv), "2048"])
    _run(["openssl", "rsa", "-in", str(sig_priv), "-pubout", "-out", str(sig_pub)])
    _run(["openssl", "genrsa", "-out", str(enc_priv), "2048"])
    _run(["openssl", "rsa", "-in", str(enc_priv), "-pubout", "-out", str(enc_pub)])
    # Diffie-Hellman params for the live-session-token handshake.
    _run(["openssl", "dhparam", "-out", str(dh), "2048"])

    # Lock down the private keys (best effort; harmless on filesystems that
    # ignore the mode).
    for secret in (sig_priv, enc_priv):
        try:
            os.chmod(secret, 0o600)
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="deploy/ib-gateway/oauth",
                    help="output dir (gitignored)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing keys (invalidates prior registration)")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    generate(out_dir, args.force)

    print(f"\n✓ OAuth key material written to {out_dir}\n")
    print("REGISTER THESE THREE in the IBKR self-service OAuth portal")
    print("(US: Settings → User Settings → API / OAuth self-service):")
    print(f"  • public_signature.pem   {out_dir / 'public_signature.pem'}")
    print(f"  • public_encryption.pem  {out_dir / 'public_encryption.pem'}")
    print(f"  • dhparam.pem            {out_dir / 'dhparam.pem'}")
    print("\nKEEP SECRET (never commit, never share):")
    print(f"  • private_signature.pem  {out_dir / 'private_signature.pem'}")
    print(f"  • private_encryption.pem {out_dir / 'private_encryption.pem'}")
    print("\nNext: follow deploy/ib-gateway/RUNBOOK.md § Path B. After IBKR")
    print("issues your consumer key (~1-2 weeks), set in .env.production:")
    print("  IBKR_OAUTH_CONSUMER_KEY=<the 9-char key>")
    print(f"  IBKR_OAUTH_SIGNATURE_KEY={out_dir / 'private_signature.pem'}")
    print(f"  IBKR_OAUTH_ENCRYPTION_KEY={out_dir / 'private_encryption.pem'}")
    print(f"  IBKR_OAUTH_DH_PARAM={out_dir / 'dhparam.pem'}\n")


if __name__ == "__main__":
    main()
