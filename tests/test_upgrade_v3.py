from __future__ import annotations

from pathlib import Path

from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.managed_gateway import ManagedKrakenGateway
from dublin_bot.managed_position import ManagedPositionStore
from dublin_bot.strategy import TrendBreakoutStrategy


def test_v3_defaults_remain_locked():
    s = Settings(_env_file=None)
    assert s.safety_locked is True
    assert s.live_execution_armed is False
    assert s.live_ready is False


def test_live_engine_gateway_requires_explicit_arm(tmp_path: Path):
    s = Settings(
        _env_file=None,
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_execution_armed=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        kraken_api_key="test-key",
        kraken_api_secret="dGVzdC1zZWNyZXQ=",
        nonce_state_path=tmp_path / "nonce.json",
    )
    gw = build_gateway(s)
    assert isinstance(gw, ManagedKrakenGateway)
    assert gw.order_submission_enabled is True


def test_managed_position_store_round_trip(tmp_path: Path):
    store = ManagedPositionStore(tmp_path / "managed.json")
    position = store.new(
        symbol="BTC/USD",
        quantity=0.001,
        entry_price=50000.0,
        stop_price=48000.0,
        order_id="ORDER-1",
    )
    store.save(position)
    loaded = store.load()
    assert loaded is not None
    assert loaded.quantity == 0.001
    assert loaded.order_id == "ORDER-1"
    store.clear()
    assert store.load() is None


def test_coo_weights_are_bounded_and_complete():
    assert sum(TrendBreakoutStrategy.WEIGHTS.values()) == 100
    assert set(TrendBreakoutStrategy.WEIGHTS) == {
        "regime", "trend", "momentum", "breakout", "volume", "volatility"
    }
