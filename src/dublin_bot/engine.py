from __future__ import annotations

from datetime import datetime, timezone

from .alpaca_gateway import AlpacaGateway
from .config import Settings
from .journal import Journal
from .models import Action, DecisionRecord, RiskDecision
from .risk import RiskManager, SessionState
from .strategy import TrendBreakoutStrategy


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gateway = AlpacaGateway(settings)
        self.strategy = TrendBreakoutStrategy(settings)
        self.risk = RiskManager(settings)
        self.journal = Journal(settings.journal_path)

    def run_once(self) -> DecisionRecord:
        bars = self.gateway.get_bars()
        in_position = self.gateway.has_position()
        signal = self.strategy.evaluate(bars, in_position=in_position)
        equity = min(self.gateway.account_equity(), self.settings.strategy_equity_usd)
        state = SessionState(start_equity=equity, peak_equity=equity, current_equity=equity)
        risk = self.risk.evaluate(signal, state)
        order_id: str | None = None

        if signal.action is Action.BUY and risk.approved:
            order_id = self.gateway.buy_notional(risk.notional_usd)
        elif signal.action is Action.SELL and in_position:
            order_id = self.gateway.close_position()
            risk = RiskDecision(True, "Exit signal approved")

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
