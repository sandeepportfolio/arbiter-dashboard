"""check_polymarket.py — wallet round-trip against Polymarket prod.

Validates:
    1. POLY_PRIVATE_KEY is a valid 32-byte hex key
    2. The derived address matches POLY_FUNDER (if set)
    3. USDC.e balance on Polygon (contract 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
    4. Polymarket CLOB /auth endpoint accepts the wallet signature

Exit 0 on all-pass, 1 on any failure.

NEVER prints the private key. Prints only the public address and USDC balance.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# USDC.e on Polygon (the stablecoin Polymarket accepts)
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_BALANCEOF_ABI = [
    {
        "name": "balanceOf",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    }
]


def main() -> int:
    pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder_env = os.getenv("POLY_FUNDER", "").strip()
    clob_url = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com").rstrip("/")

    if not pk:
        print("FAIL — POLY_PRIVATE_KEY is empty.")
        print("  Fix: paste your Polygon wallet private key into .env.production.")
        print("  Must be 64 hex chars (no 0x prefix), from a FRESH throwaway wallet.")
        return 1

    # Normalize
    if pk.startswith("0x"):
        pk = pk[2:]

    try:
        from eth_account import Account
    except ImportError:
        print("FAIL — eth_account not installed (pip install eth-account web3)")
        return 1

    try:
        acct = Account.from_key(pk)
    except Exception as e:
        print(f"FAIL — POLY_PRIVATE_KEY is not a valid Ethereum private key: {e}")
        return 1

    derived = acct.address
    print(f"derived address:  {derived}")

    # Sanity-check POLY_FUNDER matches derived when set
    if funder_env:
        if funder_env.lower() != derived.lower():
            print(f"WARN — POLY_FUNDER ({funder_env}) does not match derived address ({derived}).")
            print("  This is OK if you're using a proxy wallet (signature type 2). Otherwise, correct POLY_FUNDER.")
        else:
            print("POLY_FUNDER:      matches derived ✓")
    else:
        print("WARN — POLY_FUNDER not set. Defaulting to derived address.")

    # Check USDC.e balance on Polygon
    try:
        from web3 import Web3
    except ImportError:
        print("FAIL — web3 not installed (pip install web3)")
        return 1

    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"FAIL — cannot connect to Polygon RPC at {rpc_url}")
        return 1

    check_addr = Web3.to_checksum_address(funder_env or derived)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=ERC20_BALANCEOF_ABI)
    try:
        bal = usdc.functions.balanceOf(check_addr).call()
    except Exception as e:
        print(f"FAIL — USDC.e balanceOf call failed: {e}")
        return 1
    usdc_float = bal / 1e6
    print(f"USDC.e balance:   {usdc_float:.4f} USDC.e (wallet {check_addr})")

    pol_bal = w3.eth.get_balance(check_addr)
    pol_float = pol_bal / 1e18
    print(f"POL balance:      {pol_float:.4f} POL (for gas)")

    if usdc_float < 5:
        print("FAIL — USDC.e balance < $5. Polymarket won't let you trade with less than the minimum order notional.")
        print("  Fix: bridge at least $20 USDC to this wallet on Polygon network, then deposit via polymarket.com.")
        return 1
    if pol_float < 0.01:
        print("WARN — POL balance very low (<$0.01 worth). You need a tiny amount of POL to pay gas.")
        print("  Fix: bridge ~$1 worth of POL to this wallet (or use Polygon faucet).")

    # Polymarket CLOB auth — attempt to get API creds. py-clob-client handles the signing for us.
    try:
        from py_clob_client.client import ClobClient  # type: ignore
        from py_clob_client.constants import POLYGON  # type: ignore
    except ImportError:
        print("WARN — py-clob-client not installed; skipping CLOB auth check.")
        print("  install: pip install py-clob-client")
        return 0 if usdc_float >= 5 else 1

    try:
        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
        client = ClobClient(
            clob_url,
            key=pk,
            chain_id=POLYGON,
            signature_type=sig_type,
            funder=(funder_env or derived),
        )
        creds = client.create_or_derive_api_creds()
        if creds and getattr(creds, "api_key", None):
            print("Polymarket CLOB auth: PASS (API creds derivable)")
            return 0
        print("FAIL — Polymarket create_or_derive_api_creds returned empty")
        return 1
    except Exception as e:
        print(f"FAIL — Polymarket CLOB auth error: {e}")
        print("  Common causes:")
        print("  - Wallet hasn't connected to polymarket.com yet (do the UI deposit flow first)")
        print("  - Wrong POLY_SIGNATURE_TYPE (EOAs use 0; proxy wallets use 2)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
