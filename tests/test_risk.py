from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal
from dublin_bot.risk import RiskManager, SessionState


def test_risk_manager_caps_order_to_quarter_of_budget():
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    signal = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(start_equity=25.0, peak_equity=25.0, current_equity=25.0)
    decision = manager.evaluate(signal, state)
    assert decision.approved is True
    assert decision.notional_usd <= 6.25
    assert decision.planned_loss_usd <= 0.10


def test_daily_loss_breaker_blocks_entry():
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    signal = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(
        start_equity=25.0,
        peak_equity=25.0,
        current_equity=24.0,
        realized_pnl_today=-0.50,
    )
    decision = manager.evaluate(signal, state)
    assert decision.approved is False
    assert "Daily loss" in decision.reason


def test_consecutive_loss_breaker_blocks_entry():
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    signal = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(
        start_equity=25.0,
        peak_equity=25.0,
        current_equity=25.0,
        consecutive_losses=2,
    )
    decision = manager.evaluate(signal, state)
    assert decision.approved is False
    assert "Consecutive-loss" in decision.reason
