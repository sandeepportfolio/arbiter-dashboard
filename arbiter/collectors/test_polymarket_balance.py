"""Tests for the legacy Polymarket on-chain balance collector.

Bug being defended: ``fetch_balance()`` used to read USDC at the EOA
(``Account.from_key(private_key).address``). For accounts where trading
collateral lives at the Polymarket proxy wallet (``signature_type=2``,
``POLY_FUNDER`` set), the EOA balance is irrelevant — it's often a few cents
held for gas. The displayed dashboard balance then under-reports the trading
balance by hundreds of dollars and operators try to size trades against the
wrong number.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from arbiter.collectors.polymarket import PolymarketCollector
from arbiter.config.settings import PolymarketConfig
from arbiter.utils.price_store import PriceStore


EOA_ADDRESS = "0xeeee000000000000000000000000000000000000"
FUNDER_ADDRESS = "0xFfff000000000000000000000000000000000000"


class _ContractStub:
    """Stub for w3.eth.contract().functions.balanceOf(addr).call() chain."""

    def __init__(self, balances_by_holder: dict[str, int], decimals: int = 6):
        self._balances = {k.lower(): v for k, v in balances_by_holder.items()}
        self._decimals = decimals
        self.functions = self
        self.last_query_addr: str | None = None

    def balanceOf(self, address):
        self.last_query_addr = address
        bal = self._balances.get(str(address).lower(), 0)
        return MagicMock(call=MagicMock(return_value=bal))

    def decimals(self):
        return MagicMock(call=MagicMock(return_value=self._decimals))


def _install_web3_mock(monkeypatch, balances_by_holder, *, return_eoa=EOA_ADDRESS):
    """Install fake eth_account + web3 modules so PolymarketCollector.fetch_balance
    can run without real RPC / signing. Returns the contract stub so tests can
    inspect which address was queried.
    """
    contract = _ContractStub(balances_by_holder)

    class _FakeAccount:
        @staticmethod
        def from_key(key):
            return types.SimpleNamespace(address=return_eoa)

    fake_eth_account = types.ModuleType("eth_account")
    fake_eth_account.Account = _FakeAccount
    monkeypatch.setitem(sys.modules, "eth_account", fake_eth_account)

    class _FakeW3:
        def __init__(self, provider):
            self._provider = provider
            self.eth = types.SimpleNamespace(contract=lambda address, abi: contract)

        def is_connected(self):
            return True

        @staticmethod
        def HTTPProvider(url):
            return url

        @staticmethod
        def to_checksum_address(addr):
            return addr

    fake_web3 = types.ModuleType("web3")
    fake_web3.Web3 = _FakeW3
    monkeypatch.setitem(sys.modules, "web3", fake_web3)
    return contract


@pytest.mark.asyncio
async def test_fetch_balance_uses_funder_when_set(monkeypatch):
    """When POLY_FUNDER is configured, the balance must come from the proxy
    wallet — the EOA usually only holds gas dust on a Polymarket proxy setup.
    """
    contract = _install_web3_mock(
        monkeypatch,
        balances_by_holder={
            EOA_ADDRESS: 1_000,            # $0.001 EOA dust
            FUNDER_ADDRESS: 500_000_000,    # $500 trading balance (6 decimals)
        },
    )

    config = PolymarketConfig()
    config.private_key = "0x" + "1" * 64
    config.funder = FUNDER_ADDRESS

    store = PriceStore()
    collector = PolymarketCollector(config, store)
    balance = await collector.fetch_balance()

    # Both USDC contracts get queried; balances accumulate to the same number
    # because the stub returns the same value for either contract address.
    # The critical assertion: the holder address we queried was the FUNDER,
    # not the EOA.
    assert contract.last_query_addr.lower() == FUNDER_ADDRESS.lower()
    assert balance == pytest.approx(1000.0)  # 2 contracts × $500


@pytest.mark.asyncio
async def test_fetch_balance_falls_back_to_eoa_without_funder(monkeypatch):
    """Without POLY_FUNDER, the EOA is the only address we can query. Legacy
    behaviour must keep working when no proxy is configured.
    """
    contract = _install_web3_mock(
        monkeypatch,
        balances_by_holder={
            EOA_ADDRESS: 250_000_000,  # $250 EOA balance
        },
    )

    config = PolymarketConfig()
    config.private_key = "0x" + "1" * 64
    config.funder = ""

    store = PriceStore()
    collector = PolymarketCollector(config, store)
    balance = await collector.fetch_balance()

    assert contract.last_query_addr.lower() == EOA_ADDRESS.lower()
    assert balance == pytest.approx(500.0)  # 2 contracts × $250


@pytest.mark.asyncio
async def test_fetch_balance_falls_back_to_eoa_on_invalid_funder(monkeypatch):
    """A malformed POLY_FUNDER should not break balance lookup — log a warning
    and fall back to the EOA so the system stays operational instead of
    returning None and freezing the dashboard.
    """
    contract = _install_web3_mock(
        monkeypatch,
        balances_by_holder={EOA_ADDRESS: 100_000_000},  # $100
    )

    config = PolymarketConfig()
    config.private_key = "0x" + "1" * 64
    config.funder = "not-an-address"  # invalid

    # Override to_checksum_address to raise on invalid input
    import web3 as fake_web3_mod
    original_cs = fake_web3_mod.Web3.to_checksum_address

    @staticmethod
    def strict_cs(addr):
        if not str(addr).startswith("0x") or len(str(addr)) != 42:
            raise ValueError(f"invalid address: {addr}")
        return addr

    fake_web3_mod.Web3.to_checksum_address = strict_cs
    try:
        store = PriceStore()
        collector = PolymarketCollector(config, store)
        balance = await collector.fetch_balance()
    finally:
        fake_web3_mod.Web3.to_checksum_address = original_cs

    assert contract.last_query_addr.lower() == EOA_ADDRESS.lower()
    assert balance == pytest.approx(200.0)


def test_balance_source_reflects_funder_configuration():
    """The collector reports which address-source the balance came from so the
    dashboard can show e.g. 'polymarket:proxy-erc20(0x1234…)' vs the EOA.
    """
    store = PriceStore()

    cfg_with_funder = PolymarketConfig()
    cfg_with_funder.funder = FUNDER_ADDRESS
    cfg_with_funder.private_key = "0x" + "1" * 64
    collector = PolymarketCollector(cfg_with_funder, store)
    assert "proxy" in collector.balance_source.lower()
    assert FUNDER_ADDRESS[:10] in collector.balance_source

    cfg_no_funder = PolymarketConfig()
    cfg_no_funder.funder = ""
    cfg_no_funder.private_key = "0x" + "1" * 64
    collector2 = PolymarketCollector(cfg_no_funder, store)
    assert collector2.balance_source == "polymarket:eoa-erc20"
