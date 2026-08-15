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
    # Adaptive-risk multiplier, persisted across cycles in the same session.
    risk_scale: float = 1.0
    win_streak: int = 0
    loss_streak: int = 0


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def effective_risk_per_trade(self) -> float:
        """Base risk scaled by the session's win/loss adaptation factor."""
        s = self.settings
        if not s.adaptive_risk:
            return s.risk_per_trade
        return min(
            s.max_risk_scale,
            max(s.min_risk_scale, s.risk_per_trade * self._last_scale),
        )

    _last_scale = 1.0

    def update_scale(self, state: SessionState) -> None:
        """Adjust the adaptation factor from the most recent closed trade P&L.

        A win nudges the scale up by ``risk_step``; a loss nudges it down. The
        scale is clamped to [min_risk_scale, max_risk_scale] so a hot streak
        compounds allocation while a cold streak tightens it — never below the
        floor, never past the ceiling.
        """
        s = self.settings
        if state.realized_pnl_today > 0 and state.win_streak >= 1:
            self._last_scale = min(s.max_risk_scale, self._last_scale + s.risk_step)
        elif state.realized_pnl_today < 0 and state.loss_streak >= 1:
            self._last_scale = max(s.min_risk_scale, self._last_scale - s.risk_step)
        state.risk_scale = self._last_scale

    def evaluate(self, signal: Signal, state: SessionState, open_exposure_usd: float = 0.0) -> RiskDecision:
        s = self.settings
        equity = max(state.current_equity, 0.0) or s.strategy_equity_usd
        if signal.action is not Action.BUY:
            return RiskDecision(False, "No entry order requested")
        if state.realized_pnl_today <= -(s.strategy_equity_usd * s.max_daily_loss_fraction):
            return RiskDecision(False, "Daily loss circuit breaker is active")
        if equity < s.min_order_notional_usd:
            return RiskDecision(False, f"Account equity {equity:.2f} below minimum order notional")
        drawdown = 1 - (equity / max(state.peak_equity, 0.01))
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

        exposure_cap = equity * (s.margin_exposure_fraction if s.margin_enabled else s.max_exposure_fraction)
        if open_exposure_usd >= exposure_cap:
            return RiskDecision(False, f"Exposure cap reached: {open_exposure_usd:.2f} >= {exposure_cap:.2f}")

        risk_budget = equity * self.effective_risk_per_trade()
        stop_fraction = (signal.price - signal.stop_price) / signal.price
        if stop_fraction <= 0:
            return RiskDecision(False, "Invalid stop distance")
        risk_sized_notional = risk_budget / stop_fraction
        allocation_cap = equity * s.max_position_fraction
        notional = min(risk_sized_notional, allocation_cap, equity)
        if open_exposure_usd + notional > exposure_cap:
            notional = max(0.0, exposure_cap - open_exposure_usd)
        if notional < s.min_order_notional_usd:
            return RiskDecision(False, "Calculated order is below minimum notional")
        planned_loss = notional * stop_fraction
        return RiskDecision(True, "Risk checks passed", round(notional, 2), round(planned_loss, 2))

