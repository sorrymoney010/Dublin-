from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .alpaca_gateway import AlpacaGateway
from .config import Settings
from .journal import Journal
from .models import Action, DecisionRecord, RiskDecision
from .risk import RiskManager
from .state import StateStore
from .strategy import TrendBreakoutStrategy


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gateway = AlpacaGateway(settings)
        self.strategy = TrendBreakoutStrategy(settings)
        self.risk = RiskManager(settings)
        self.journal = Journal(settings.journal_path)
        self.state_store = StateStore(Path("logs/session_state.json"))

    def run_once(self) -> DecisionRecord:
        bars = self.gateway.get_bars()
        in_position = self.gateway.has_position()
        signal = self.strategy.evaluate(bars, in_position=in_position)
        equity = min(self.gateway.account_equity(), self.settings.strategy_equity_usd)
        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        state.realized_pnl_today = equity - state.start_equity
        risk = self.risk.evaluate(signal, state)
        order_id: str | None = None

        if signal.action is Action.BUY and risk.approved:
            order_id = self.gateway.buy_notional(risk.notional_usd)
            state.orders_today += 1
            state.last_order_at = datetime.now(timezone.utc)
        elif signal.action is Action.SELL and in_position:
            order_id = self.gateway.close_position()
            state.orders_today += 1
            state.last_order_at = datetime.now(timezone.utc)
            risk = RiskDecision(True, "Exit signal approved")

        self.state_store.save(state)
        record = DecisionRecord(
            symbol=self.settings.symbol,
            signal=signal,
            risk=risk,
            dry_run=self.settings.dry_run,
            order_id=order_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return record
