from __future__ import annotations

from decimal import Decimal

from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.kraken_gateway import SymbolMeta
from dublin_bot.managed_gateway import ManagedKrakenGateway


def test_default_engine_gateway_remains_unarmed(tmp_path):
    settings = Settings(
        _env_file=None,
        nonce_state_path=tmp_path / "nonce.json",
        managed_position_path=tmp_path / "managed.json",
    )
    gateway = build_gateway(settings)
    assert isinstance(gateway, ManagedKrakenGateway)
    assert gateway.order_submission_enabled is False


def test_live_engine_gateway_requires_explicit_arm_and_credentials(tmp_path):
    settings = Settings(
        _env_file=None,
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_execution_armed=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        kraken_api_key="fake-key",
        kraken_api_secret="c2VjcmV0",
        nonce_state_path=tmp_path / "nonce.json",
        managed_position_path=tmp_path / "managed.json",
    )
    gateway = build_gateway(settings)
    assert gateway.order_submission_enabled is True


def test_managed_exit_never_uses_entire_wallet_balance(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        nonce_state_path=tmp_path / "nonce.json",
        managed_position_path=tmp_path / "managed.json",
    )
    gateway = ManagedKrakenGateway(settings)
    meta = SymbolMeta(
        key="XXBTZUSD",
        altname="XBTUSD",
        wsname="XBT/USD",
        base="XXBT",
        quote="ZUSD",
        lot_decimals=8,
        pair_decimals=1,
        order_min=Decimal("0.00001"),
        cost_min=Decimal("1"),
        status="online",
    )
    monkeypatch.setattr(gateway, "resolve_symbol", lambda symbol=None: meta)
    monkeypatch.setattr(gateway, "positions", lambda: [{"quantity": 1.5}])
    events = []
    monkeypatch.setattr(
        gateway,
        "_log",
        lambda event, payload, severity="info": events.append(payload),
    )

    order_id = gateway.sell_quantity(0.01, userref=123)

    assert order_id.startswith("kraken-dry-managed-sell-")
    assert events[-1]["managed_exit"] is True
    assert Decimal(events[-1]["volume"]) == Decimal("0.01000000")
    assert Decimal(events[-1]["volume"]) < Decimal("1.5")


def test_coo_threshold_relationship_is_validated():
    try:
        Settings(_env_file=None, coo_entry_score=60, coo_exit_score=70)
    except ValueError as exc:
        assert "COO_ENTRY_SCORE" in str(exc)
    else:
        raise AssertionError("invalid COO threshold relationship was accepted")
