"""Offline engine safety regressions using Kraken-shaped fake responses."""
from decimal import Decimal

import pytest

from dublin_bot.engine import TradingEngine, build_gateway
from dublin_bot.errors import BrokerError
from dublin_bot.models import Action, Signal
from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier
from tests.conftest import balance_payload, error_payload, ohlc_payload, ticker_payload


@pytest.fixture
def live_engine(settings_factory, fake_session):
    settings = settings_factory(
        paper_trading=False, dry_run=False, allow_live_trading=True,
        live_execution_armed=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    limiter = KrakenRateLimiter(
        RateLimitTier.pro(), time_fn=lambda: clock[0], sleep_fn=sleep,
    )
    gateway = build_gateway(settings, session=fake_session, rate_limiter=limiter)
    fake_session.routes["Balance"] = balance_payload(XXBT="1.5", ZUSD="1000")
    fake_session.routes["AddOrder"] = {"error": [], "result": {"txid": ["FAKE-EXIT"]}}
    return TradingEngine(settings, gateway=gateway)


def seed_position(engine):
    position = engine.position_store.new(
        symbol="BTC/USD", quantity=0.01, entry_price=50000,
        stop_price=48000, order_id="FAKE-ENTRY",
    )
    engine.position_store.save(position)
    return position


@pytest.mark.parametrize("price", [47000, 48000])
@pytest.mark.parametrize("ohlc", [error_payload("EQuery:Unavailable"), ohlc_payload(end_ts=1)])
def test_stop_exits_before_candle_and_entry_gates(
    live_engine, fake_session, monkeypatch, price, ohlc,
):
    seed_position(live_engine)
    fake_session.routes["Ticker"] = ticker_payload(last=price, bid=1, ask=100000)
    fake_session.routes["OHLC"] = ohlc
    def forbidden(*args, **kwargs):
        raise AssertionError("Triggered stop must bypass strategy and entry market checks")
    monkeypatch.setattr(live_engine.strategy, "evaluate", forbidden)
    state = live_engine.state_store.load(25)
    state.orders_today = 100
    live_engine.state_store.save(state)
    result = live_engine.run_cycle()
    assert result.executed
    assert result.record.signal.action is Action.SELL
    assert result.gates["protective_stop"]["triggered"]
    orders = fake_session.endpoint_calls("AddOrder")
    assert len(orders) == 1
    assert Decimal(orders[0]["data"]["volume"]) == Decimal("0.01")
    assert orders[0]["data"]["type"] == "sell"
    assert not fake_session.endpoint_calls("OHLC")


def test_stop_above_threshold_keeps_position(live_engine, fake_session, monkeypatch):
    seed_position(live_engine)
    monkeypatch.setattr(live_engine.strategy, "evaluate", lambda *a, **kw: Signal(
        Action.WAIT, 0, "hold", 50000,
    ))
    result = live_engine.run_cycle()
    assert not result.gates["protective_stop"]["triggered"]
    assert not result.executed
    assert live_engine.position_store.load() is not None
    assert not fake_session.endpoint_calls("AddOrder")


@pytest.mark.parametrize("balance", ["0", "0.009", "0.0099999999999", "nan", "inf", "-1", "bad", None])
def test_bad_balance_blocks_all_orders(live_engine, fake_session, balance):
    seed_position(live_engine)
    fake_session.routes["Ticker"] = ticker_payload(last=47000)
    fake_session.routes["Balance"] = balance_payload(XXBT=balance)
    result = live_engine.run_cycle()
    assert result.blocked_at == "reconciliation"
    assert not fake_session.endpoint_calls("AddOrder")
    assert live_engine.position_store.load() is not None


@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("armed", [True, False])
def test_private_balance_failure_fails_closed(live_engine, fake_session, managed, armed):
    if managed:
        seed_position(live_engine)
    live_engine.settings.live_execution_armed = armed
    fake_session.routes["Balance"] = error_payload("EGeneral:Permission denied")
    result = live_engine.run_cycle()
    assert result.blocked_at == "reconciliation"
    assert not fake_session.endpoint_calls("AddOrder")


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "bad", None])
@pytest.mark.parametrize("managed", [True, False])
def test_invalid_equity_blocks_entry_and_stop(live_engine, fake_session, monkeypatch, value, managed):
    if managed:
        seed_position(live_engine)
        fake_session.routes["Ticker"] = ticker_payload(last=47000)
    else:
        monkeypatch.setattr(live_engine.strategy, "evaluate", lambda *a, **kw: Signal(
            Action.BUY, 100, "buy", 50000, stop_price=48000,
        ))
    fake_session.routes["TradeBalance"] = {"error": [], "result": {"eb": value}}
    result = live_engine.run_cycle()
    assert result.blocked_at == "account_data"
    assert not fake_session.endpoint_calls("AddOrder")


@pytest.mark.parametrize("armed", [True, False])
def test_equity_api_failure_never_uses_budget_in_live_mode(live_engine, fake_session, armed):
    live_engine.settings.live_execution_armed = armed
    fake_session.routes["TradeBalance"] = error_payload("EGeneral:Permission denied")
    with pytest.raises(BrokerError):
        live_engine.gateway.account_equity()


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "bad", None])
def test_invalid_stop_ticker_blocks(live_engine, fake_session, value):
    seed_position(live_engine)
    fake_session.routes["Ticker"] = ticker_payload(last=value)
    result = live_engine.run_cycle()
    assert result.blocked_at == "protective_stop"
    assert not fake_session.endpoint_calls("AddOrder")
    assert live_engine.position_store.load() is not None


@pytest.mark.parametrize("raw", ['{', '{}', 'null', '{"quantity": 0}'])
def test_corrupt_managed_ledger_never_becomes_flat(live_engine, fake_session, raw):
    live_engine.position_store.path.write_text(raw)
    result = live_engine.run_cycle()
    assert result.blocked_at == "reconciliation"
    assert not fake_session.endpoint_calls("AddOrder")


def test_symbol_mismatch_blocks(live_engine, fake_session):
    position = seed_position(live_engine)
    position.symbol = "ETH/USD"
    live_engine.position_store.save(position)
    assert live_engine.run_cycle().blocked_at == "reconciliation"
    assert not fake_session.endpoint_calls("AddOrder")


def test_balance_rechecked_before_sell(live_engine, fake_session):
    seed_position(live_engine)
    fake_session.routes["Ticker"] = ticker_payload(last=47000)
    fake_session.routes["Balance"] = [balance_payload(XXBT="1.5"), balance_payload(XXBT="0.005")]
    result = live_engine.run_cycle()
    assert not result.executed
    assert "exceeds available" in result.record.risk.reason
    assert live_engine.position_store.load() is not None
    assert not fake_session.endpoint_calls("AddOrder")


def test_unrelated_wallet_holding_is_not_a_managed_exit(live_engine, fake_session, monkeypatch):
    monkeypatch.setattr(live_engine.strategy, "evaluate", lambda *a, **kw: Signal(
        Action.SELL, 100, "sell", 50000,
    ))
    result = live_engine.run_cycle()
    assert result.gates["position_reconciliation"]["orphaned_exchange_holding"]
    assert not result.executed
    assert not fake_session.endpoint_calls("AddOrder")


def test_paper_stop_requires_no_wallet_balance(live_engine, fake_session):
    seed_position(live_engine)
    live_engine.settings.live_execution_armed = False
    live_engine.settings.allow_live_trading = False
    live_engine.settings.paper_trading = True
    live_engine.settings.dry_run = True
    fake_session.routes["Ticker"] = ticker_payload(last=47000)
    fake_session.routes["Balance"] = error_payload("EGeneral:Permission denied")
    result = live_engine.run_cycle()
    assert result.executed
    assert result.record.order_id.startswith("kraken-dry-managed-sell-")
    assert not fake_session.endpoint_calls("Balance")
    assert not fake_session.endpoint_calls("AddOrder")


def test_stop_preserves_safety_locks(live_engine, fake_session):
    seed_position(live_engine)
    live_engine.settings.live_risk_acknowledgement = ""
    fake_session.routes["Ticker"] = ticker_payload(last=47000)
    assert live_engine.run_cycle().blocked_at == "safety"
    assert not fake_session.endpoint_calls("AddOrder")


@pytest.mark.parametrize("payload", [error_payload("EGeneral:Permission denied"),
                                    {"error": [], "result": {}}])
def test_stop_ticker_failure_blocks(live_engine, fake_session, payload):
    seed_position(live_engine)
    fake_session.routes["Ticker"] = payload
    assert live_engine.run_cycle().blocked_at == "protective_stop"
    assert not fake_session.endpoint_calls("AddOrder")


def test_missing_equity_fails_closed(live_engine, fake_session):
    fake_session.routes["TradeBalance"] = {"error": [], "result": {}}
    assert live_engine.run_cycle().blocked_at == "account_data"
    assert not fake_session.endpoint_calls("AddOrder")


def test_missing_base_asset_is_zero_and_blocks_managed_shortage(live_engine, fake_session):
    seed_position(live_engine)
    fake_session.routes["Balance"] = balance_payload(ZUSD="1000")
    result = live_engine.run_cycle()
    assert result.blocked_at == "reconciliation"
    assert result.gates["position_reconciliation"]["exchange_quantity"] == 0
    assert not fake_session.endpoint_calls("AddOrder")
