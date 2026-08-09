from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Settings
from .models import Action, RiskDecision, Signal


@dataclass
class SessionState:
    start_equity: float
    peak_equity: float
    current_equity: float
    realized_pnl_today: float = 0.0
    orders_today: int = 0
    last_order_at: datetime | None = None


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, signal: Signal, state: SessionState) -> RiskDecision:
        s = self.settings
        if signal.action is not Action.BUY:
            return RiskDecision(False, "No entry order requested")
        if state.realized_pnl_today <= -(s.strategy_equity_usd * s.max_daily_loss_fraction):
            return RiskDecision(False, "Daily loss circuit breaker is active")
        if state.current_equity < s.strategy_equity_usd:
            return RiskDecision(False, "Available strategy equity is below configured budget")
        drawdown = 1 - (state.current_equity / max(state.peak_equity, 0.01))
        if drawdown >= s.max_drawdown_fraction:
            return RiskDecision(False, "Maximum drawdown circuit breaker is active")
        if state.orders_today >= s.max_orders_per_day:
            return RiskDecision(False, "Daily order limit reached")
        if state.last_order_at is not None:
            ready_at = state.last_order_at + timedelta(minutes=s.cooldown_minutes)
            if datetime.now(timezone.utc) < ready_at:
                return RiskDecision(False, "Trade cooldown is active")
        if signal.stop_price is None or signal.price <= signal.stop_price:
            return RiskDecision(False, "Invalid stop distance")

        risk_budget = s.strategy_equity_usd * s.risk_per_trade
        stop_fraction = (signal.price - signal.stop_price) / signal.price
        risk_sized_notional = risk_budget / stop_fraction
        allocation_cap = s.strategy_equity_usd * s.max_position_fraction
        notional = min(risk_sized_notional, allocation_cap, state.current_equity)
        if notional < s.min_order_notional_usd:
            return RiskDecision(False, "Calculated order is below minimum notional")
        planned_loss = notional * stop_fraction
        return RiskDecision(True, "Risk checks passed", round(notional, 2), round(planned_loss, 2))

