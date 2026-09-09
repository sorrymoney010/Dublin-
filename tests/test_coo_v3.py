from __future__ import annotations

import base64

import pytest

from dublin_bot.config import Settings
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.managed_position import ManagedPositionStore


FAKE_SECRET = base64.b64encode(b"dublin-v3-test-secret").decode()


def test_defaults_remain_locked():
    s = Settings(_env_file=None)
    assert s.safety_locked is True
    assert s.live_execution_armed is False
    assert s.live_ready is False


def test_live_ready_requires_every_execution_gate(tmp_path):
    s = Settings(
        _env_file=None,
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_execution_armed=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        kraken_api_key="test-key",
        kraken_api_secret=FAKE_SECRET,
        managed_position_path=tmp_path / "managed.json",
    )
    assert s.live_ready is True


def test_execution_arm_rejects_incomplete_live_config():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            paper_trading=False,
            dry_run=False,
            allow_live_trading=True,
            live_execution_armed=True,
            live_risk_acknowledgement="",
        )


def test_gateway_live_submission_requires_constructor_arm(tmp_path):
    s = Settings(
        _env_file=None,
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_execution_armed=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        kraken_api_key="test-key",
        kraken_api_secret=FAKE_SECRET,
        nonce_state_path=tmp_path / "nonce.json",
    )
    gw = KrakenGateway(s, allow_order_submission=False)
    assert gw.order_submission_enabled is False
    armed = KrakenGateway(s, allow_order_submission=True)
    assert armed.order_submission_enabled is True


def test_managed_position_store_round_trip(tmp_path):
    store = ManagedPositionStore(tmp_path / "managed.json")
    position = ManagedPositionStore.new(
        symbol="BTC/USD",
        quantity=0.001,
        entry_price=50_000.0,
        stop_price=48_500.0,
        order_id="TEST-ORDER",
    )
    store.save(position)
    loaded = store.load()
    assert loaded is not None
    assert loaded.symbol == "BTC/USD"
    assert loaded.quantity == pytest.approx(0.001)
    assert loaded.stop_price == pytest.approx(48_500.0)
    store.clear()
    assert store.load() is None
