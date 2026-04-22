# Portable Secrets

This folder exists to make Arbiter portable across machines without committing raw secrets.

## Recommended workflow

On the source machine:

```bash
export PORTABLE_SECRETS_PASSPHRASE='choose-a-strong-passphrase'
./scripts/setup/export_portable_secrets.sh
```

That creates:

```text
portable-secrets/arbiter-portable-secrets.tgz.enc
```

On the destination machine after `git clone`:

```bash
export PORTABLE_SECRETS_PASSPHRASE='the-same-passphrase'
./scripts/setup/import_portable_secrets.sh
```

Then verify:

```bash
./scripts/setup/bootstrap_python.sh
./.venv/bin/python scripts/setup/validate_env.py
./.venv/bin/python scripts/setup/check_kalshi_auth.py
./.venv/bin/python scripts/setup/check_polymarket_us.py
./.venv/bin/python scripts/setup/check_telegram.py
```

## What is inside the encrypted bundle

If present locally, the exporter includes:
- `.env.production`
- `.env`
- `keys/kalshi_private.pem`

## What not to do

- Do not commit raw secret files.
- Do not store the passphrase in git.
- Do not paste the passphrase into docs, issues, or chat logs.

## Optional repo-native portability

If you deliberately want clone-only portability from Git alone, you may commit the encrypted bundle file itself:

```text
portable-secrets/arbiter-portable-secrets.tgz.enc
```

That is much safer than committing raw keys, but it is still a policy choice:
- only do it if you are comfortable with the repo holding encrypted secrets
- keep the passphrase separate, ideally in a password manager or secure note
