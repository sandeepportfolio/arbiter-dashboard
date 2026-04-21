"""One-shot helper: read a concatenated single-line PEM from stdin, validate via
cryptography library, and write properly-formatted PKCS#1/8 RSA PEM to
keys/kalshi_private.pem. No key material touches stdout/log."""
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization

raw = sys.stdin.read().strip()
# Reconstruct a minimally-valid PEM the cryptography lib can parse: ensure newlines
# exist after BEGIN header and before END footer so base64 body is isolated.
if "-----BEGIN RSA PRIVATE KEY-----" in raw and "\n" not in raw:
    body = raw.split("-----BEGIN RSA PRIVATE KEY-----", 1)[1]
    body = body.split("-----END RSA PRIVATE KEY-----", 1)[0].strip()
    pem_in = "-----BEGIN RSA PRIVATE KEY-----\n" + body + "\n-----END RSA PRIVATE KEY-----\n"
else:
    pem_in = raw

key = serialization.load_pem_private_key(pem_in.encode(), password=None)
# Re-serialize in standard format — writes 64-char lines, PKCS#1.
pem_out = key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)
out = Path("keys/kalshi_private.pem")
out.write_bytes(pem_out)
out.chmod(0o600)
print(f"wrote {out} ({out.stat().st_size} bytes)")
