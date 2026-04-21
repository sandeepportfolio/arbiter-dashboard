"""onboard_polymarket_us.py — Playwright-driven onboarding for the Polymarket US dev portal.

Behavior:
    1. Open headful Chromium to https://polymarket.us/developer.
    2. Poll the URL for a dev-portal-internal path, waiting up to 5 minutes
       for the operator to complete login manually.
    3. Attempt the "Create API Key" flow using best-effort selectors.
       Because the portal is login-gated and may drift, the script always keeps
       a manual fallback: if the automatic selector fails, it pauses and prints
       "Operator: please click Create API Key in the browser, then press Enter."
    4. Capture the secret from the on-screen field using Playwright's
       locator.input_value() directly into a Python variable — never via screenshot.
    5. Edit .env.production to set POLYMARKET_US_API_KEY_ID and
       POLYMARKET_US_API_SECRET using a line-by-line rewrite (preserve other lines).
    6. Close the browser page showing the secret.
    7. Delete any intermediate screenshots.
    8. Print "Credentials captured and written to .env.production. Run: ./scripts/setup/go_live.sh"
       (NEVER print the secret).

Safety:
    - The whole run is wrapped in try/finally so the browser closes on any error.
    - On failure, .env.production is scrubbed of any partial write.
    - Never prints or logs the secret value at any point.

Usage:
    pip install playwright && playwright install chromium
    python scripts/setup/onboard_polymarket_us.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers — unit-testable, no browser dependency
# ---------------------------------------------------------------------------

def rewrite_env_file(env_text: str, key_id: str, secret: str) -> str:
    """Rewrite an .env file buffer, setting the two Polymarket US credentials.

    - Lines matching POLYMARKET_US_API_KEY_ID=... or POLYMARKET_US_API_SECRET=...
      are replaced in-place.
    - If either key is absent from the file, it is appended at the end.
    - All other lines are preserved verbatim (comments, blank lines, other vars).
    - The secret value is NEVER written to stdout; only the file buffer is mutated.

    Parameters
    ----------
    env_text : str
        Current contents of the .env file.
    key_id : str
        The API key ID value to write.
    secret : str
        The API secret (base64 Ed25519 seed) to write. NEVER printed.

    Returns
    -------
    str
        The rewritten .env file contents.
    """
    lines = env_text.splitlines(keepends=True)
    found_key_id = False
    found_secret = False

    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("POLYMARKET_US_API_KEY_ID="):
            new_lines.append(f"POLYMARKET_US_API_KEY_ID={key_id}\n")
            found_key_id = True
        elif stripped.startswith("POLYMARKET_US_API_SECRET="):
            new_lines.append(f"POLYMARKET_US_API_SECRET={secret}\n")
            found_secret = True
        else:
            new_lines.append(line)

    if not found_key_id:
        new_lines.append(f"POLYMARKET_US_API_KEY_ID={key_id}\n")
    if not found_secret:
        new_lines.append(f"POLYMARKET_US_API_SECRET={secret}\n")

    return "".join(new_lines)


def _scrub_env_file(env_path: Path) -> None:
    """Remove partial POLYMARKET_US credential lines from .env.production on failure."""
    if not env_path.exists():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        clean_lines = [
            line for line in lines
            if not line.lstrip().startswith("POLYMARKET_US_API_KEY_ID=")
            and not line.lstrip().startswith("POLYMARKET_US_API_SECRET=")
        ]
        env_path.write_text("".join(clean_lines), encoding="utf-8")
    except OSError:
        pass  # best effort — don't mask the original error


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------

_PORTAL_URL = "https://polymarket.us/developer"
_PORTAL_INTERNAL_PATHS = ("/developer/keys", "/developer/api", "/api-keys", "/developer")
_LOGIN_TIMEOUT_S = 300  # 5 minutes
_POLL_INTERVAL_S = 2

# Best-effort selectors for the login-gated Polymarket US portal. These are not
# treated as a hard dependency because the flow explicitly falls back to manual
# operator click/paste when the UI drifts.
_CREATE_KEY_SELECTOR = 'button:has-text("Create API Key")'
_KEY_ID_SELECTOR = '[data-testid="api-key-id"], input[name="keyId"], .api-key-id'
_SECRET_SELECTOR = '[data-testid="api-secret"], input[name="secret"], .api-secret'


def _wait_for_login(page) -> bool:
    """Poll page URL until it looks like a logged-in dev-portal path (up to 5 min).

    Returns True if login was detected, False on timeout.
    """
    print("Waiting for operator login... (navigate to and complete login at the browser window)")
    deadline = time.time() + _LOGIN_TIMEOUT_S
    while time.time() < deadline:
        current_url = page.url
        if any(path in current_url for path in _PORTAL_INTERNAL_PATHS):
            print(f"Login detected at: {current_url}")
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False


def run_onboarding(env_path: Path | None = None) -> None:
    """Full onboarding flow. Requires a real browser environment."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        print(
            "playwright not installed. Run: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(2)

    if env_path is None:
        # Default: .env.production at repo root (two levels up from scripts/setup/)
        env_path = Path(__file__).resolve().parent.parent.parent / ".env.production"

    captured_key_id: str | None = None
    captured_secret: str | None = None
    screenshots: list[Path] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Navigate to dev portal
            page.goto(_PORTAL_URL)
            print(f"Opened browser at {_PORTAL_URL}")

            # 2. Wait for operator login
            if not _wait_for_login(page):
                raise RuntimeError(
                    f"Timed out waiting for login after {_LOGIN_TIMEOUT_S}s. "
                    "Please re-run after completing login."
                )

            # 3. Navigate to Create API Key flow
            try:
                page.wait_for_selector(_CREATE_KEY_SELECTOR, timeout=10_000)
                page.click(_CREATE_KEY_SELECTOR)
                print("Clicked 'Create API Key' button.")
            except PWTimeoutError:
                # Fallback: let the operator navigate manually
                print(
                    f"\nCould not find '{_CREATE_KEY_SELECTOR}' automatically.\n"
                    "Operator: please click 'Create API Key' in the browser, then press Enter here."
                )
                input("Press Enter after clicking Create API Key...")

            # 4. Capture the secret from the DOM field — never via screenshot
            # Wait for the key_id and secret fields to appear after creation
            try:
                page.wait_for_selector(_KEY_ID_SELECTOR, timeout=30_000)
                captured_key_id = page.locator(_KEY_ID_SELECTOR).first.input_value()
            except PWTimeoutError:
                print(
                    f"\nCould not find key ID field (selector: {_KEY_ID_SELECTOR}).\n"
                    "Operator: paste the Key ID here (not the secret):"
                )
                captured_key_id = input("Key ID: ").strip()

            try:
                page.wait_for_selector(_SECRET_SELECTOR, timeout=30_000)
                # Read directly into variable — NEVER print this value
                captured_secret = page.locator(_SECRET_SELECTOR).first.input_value()
            except PWTimeoutError:
                print(
                    f"\nCould not find secret field (selector: {_SECRET_SELECTOR}).\n"
                    "Operator: paste the secret here (it will NOT be echoed):"
                )
                import getpass
                captured_secret = getpass.getpass("Secret (hidden): ").strip()

            if not captured_key_id or not captured_secret:
                raise RuntimeError("Failed to capture key_id or secret — aborting.")

            # 5. Write to .env.production using line-by-line rewrite
            if env_path.exists():
                existing = env_path.read_text(encoding="utf-8")
            else:
                existing = ""

            new_content = rewrite_env_file(existing, captured_key_id, captured_secret)
            env_path.write_text(new_content, encoding="utf-8")
            # Clear in-memory copies immediately after write
            captured_secret = None

            # 6. Close the browser page showing the secret
            page.close()

        except Exception:
            # On any error: close page, scrub partial writes, re-raise
            try:
                page.close()
            except Exception:
                pass
            if captured_secret is not None:
                captured_secret = None  # scrub from memory
            _scrub_env_file(env_path)
            raise

        finally:
            # 7. Delete any intermediate screenshots
            for shot_path in screenshots:
                try:
                    shot_path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    # 8. Print success message — NEVER print the secret
    print("Credentials captured and written to .env.production. Run: ./scripts/setup/go_live.sh")


if __name__ == "__main__":
    run_onboarding()
