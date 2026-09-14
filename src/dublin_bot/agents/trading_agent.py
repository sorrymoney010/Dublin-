"""
Trading Agent: applies strategy rules, executes trades, tracks positions.

This is the "brain" that:
1. Scans all coins using the strategy
2. Decides: BUY, SELL, or WAIT
3. Executes the trade (paper or live)
4. Tracks what we hold
5. Reports what it's doing

Paper trading mode: simulates trades without real money, tracks PnL in a ledger.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dublin_bot.config import Settings
from dublin_bot.gateway import BrokerGateway
from dublin_bot.strategies.rotation_strategy import RotationStrategy
from dublin_bot.models import Action, Signal, RiskDecision
from dublin_bot.paper import PaperPortfolio
from dublin_bot.risk import RiskManager
from dublin_bot.state import StateStore
from dublin_bot.engine import build_gateway
from dublin_bot.market_data import MultiSymbolGateway


class TradingAgent:
    """
    The trading "brain": scans, decides, executes, tracks.
    
    Paper trading mode (default): simulates everything, tracks PnL in paper_portfolio.json.
    Live mode: actually trades on the exchange.
    """

    def __init__(
        self,
        settings: Settings,
        gateway: Optional[BrokerGateway] = None,
        strategy: Optional[RotationStrategy] = None,
        paper_portfolio: Optional[PaperPortfolio] = None,
        risk_manager: Optional[RiskManager] = None,
    ):
        self.settings = settings
        
        # Build base gateway
        base_gateway = gateway or build_gateway(self.settings)
        
        # Wrap in multi-symbol adapter for rotation strategy
        self.gateway = MultiSymbolGateway(base_gateway, self.settings)
        self._base_gateway = base_gateway  # Keep reference for direct calls
        
        self.strategy = strategy or RotationStrategy(settings)
        self.risk = risk_manager or RiskManager(settings)
        self.paper_portfolio = paper_portfolio or PaperPortfolio(
            Path(getattr(settings, "paper_portfolio_path", "logs/paper_portfolio.json"))
        )
        self.state_store = StateStore(
            Path(getattr(settings, "session_state_path", "logs/session_state.json"))
        )
        
        # Paper trading ledger
        self._paper_trades: list[dict] = self._load_paper_trades()
        
        # Trading state
        self._last_cycle_time: Optional[float] = None
        self._cooldown_until: float = 0.0
        
    def _load_paper_trades(self) -> list[dict]:
        """Load paper trade history."""
        path = Path("logs/paper_trades.json")
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return []

    def _save_paper_trades(self):
        """Persist paper trade history."""
        path = Path("logs/paper_trades.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self._paper_trades, f, indent=2)

    def get_position_info(self) -> dict:
        """Get current position info from gateway."""
        # Use base gateway for direct calls
        gw = self._base_gateway
        has_pos = gw.has_position()
        positions = gw.positions()
        return {
            "has_position": has_pos,
            "positions": positions,
            "equity": gw.account_equity(),
            "open_orders": gw.orders(),
        }

    def scan_and_decide(self) -> dict:
        """
        Main decision loop: scan all coins, decide action, execute if needed.
        
        Returns a report dict with what happened this cycle.
        """
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper_trading": self.settings.paper_trading,
            "decisions": [],
            "executed": False,
            "current_position": self.strategy.get_position(),
        }

        # 1. Scan all allowed coins using multi-symbol gateway
        allowed_coins = self.settings.universe_allowlist or self.settings.coin_basket
        coin_signals = {}

        for symbol in allowed_coins:
            try:
                # Use multi-symbol gateway to fetch bars for this specific symbol
                bars = self.gateway.get_bars_for(symbol)
                if bars is not None and len(bars) >= 20:
                    # Strategy evaluate needs bars + whether we're in position on this coin
                    in_position = (symbol == self.strategy.get_position())
                    signal = self.strategy.evaluate(bars, in_position=in_position)
                    coin_signals[symbol] = signal
                    report["decisions"].append({
                        "symbol": symbol,
                        "signal": signal.action.value,
                        "score": signal.score,
                        "reason": signal.reason,
                        "price": signal.price,
                    })
                else:
                    report["decisions"].append({
                        "symbol": symbol,
                        "signal": "NO_DATA",
                        "reason": "Insufficient bars",
                        "price": 0.0,
                    })
            except Exception as e:
                report["decisions"].append({
                    "symbol": symbol,
                    "signal": "ERROR",
                    "error": str(e),
                })

        # 2. Use rotation strategy to pick the best action
        current_holdings = self._get_current_holdings()
        rotation_signal = self.strategy.evaluate_all_coins(coin_signals, current_holdings)

        report["best_action"] = rotation_signal.action.value if rotation_signal else "WAIT"
        report["best_symbol"] = self.strategy.get_position()
        report["best_reason"] = rotation_signal.reason if rotation_signal else "No signal"

        # 3. Execute if needed
        if rotation_signal and rotation_signal.action == Action.BUY:
            result = self._execute_buy(rotation_signal, current_holdings)
            report["executed"] = result.get("executed", False)
            report["order_result"] = result
            report["current_position"] = self.strategy.get_position()

        elif rotation_signal and rotation_signal.action == Action.SELL:
            result = self._execute_sell(rotation_signal, current_holdings)
            report["executed"] = result.get("executed", False)
            report["order_result"] = result
            report["current_position"] = self.strategy.get_position()

        # 4. Update cooldown
        self._last_cycle_time = time.time()
        if self.settings.cooldown_minutes > 0:
            self._cooldown_until = time.time() + (self.settings.cooldown_minutes * 60)

        return report

    def _get_current_holdings(self) -> dict[str, float]:
        """Get current holdings (paper or live)."""
        if self.settings.paper_trading or self.settings.dry_run:
            # Use paper portfolio
            return self.paper_portfolio.holdings()
        else:
            # Live: query exchange
            gw = self._base_gateway
            positions = gw.positions()
            holdings = {}
            for pos in positions:
                asset = pos.get("asset", "")
                amount = float(pos.get("quantity", 0))
                if amount > 0:
                    holdings[asset] = amount
            return holdings

    def _execute_buy(self, signal: Signal, current_holdings: dict) -> dict:
        """Execute a BUY order (paper or live)."""
        symbol = self.strategy.get_position() or "UNKNOWN"
        result = {
            "action": "BUY",
            "symbol": symbol,
            "price": signal.price,
            "executed": False,
            "mode": "paper" if (self.settings.paper_trading or self.settings.dry_run) else "live",
        }

        if self.settings.paper_trading or self.settings.dry_run:
            # Paper trading: simulate the trade
            equity = self.settings.strategy_equity_usd
            notional = equity * self.settings.risk_per_trade  # Risk % of equity
            if notional < self.settings.min_order_notional_usd:
                notional = self.settings.min_order_notional_usd
            
            quantity = notional / signal.price
            
            # Record paper trade
            trade = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "BUY",
                "symbol": symbol,
                "price": signal.price,
                "quantity": quantity,
                "notional": notional,
                "paper_cost": notional,
                "status": "SIMULATED",
            }
            self._paper_trades.append(trade)
            self._save_paper_trades()
            
            # Update paper portfolio
            self.paper_portfolio.record_trade(
                symbol=symbol,
                action="BUY",
                price=signal.price,
                quantity=quantity,
            )
            
            result["executed"] = True
            result["simulated_quantity"] = quantity
            result["simulated_notional"] = notional

        else:
            # Live trading: actually place order
            try:
                # Check cooldown
                if time.time() < self._cooldown_until:
                    result["error"] = "Cooldown active"
                    return result
                
                # Check risk
                gw = self._base_gateway
                equity = gw.account_equity()
                state = self.state_store.load(equity)
                risk_decision = self.risk.evaluate(signal, state)
                
                if not risk_decision.approved:
                    result["error"] = risk_decision.reason
                    return result
                
                # Place order using gateway's buy_notional
                notional = risk_decision.notional_usd
                order_id = gw.buy_notional(
                    notional_usd=notional,
                    signal_price=signal.price,
                )
                
                result["executed"] = True
                result["order_id"] = order_id
                result["notional"] = notional
                
                # Update paper portfolio (even live trades get paper tracking)
                self.paper_portfolio.record_trade(
                    symbol=symbol,
                    action="BUY",
                    price=signal.price,
                    quantity=notional / signal.price,
                )
                
            except Exception as e:
                result["error"] = str(e)
                result["executed"] = False

        return result

    def _execute_sell(self, signal: Signal, current_holdings: dict) -> dict:
        """Execute a SELL order (paper or live)."""
        symbol = self.strategy.get_position() or "UNKNOWN"
        result = {
            "action": "SELL",
            "symbol": symbol,
            "price": signal.price,
            "executed": False,
            "mode": "paper" if (self.settings.paper_trading or self.settings.dry_run) else "live",
        }

        position = self.strategy.get_position()
        if not position:
            result["error"] = "No position to sell"
            return result

        if self.settings.paper_trading or self.settings.dry_run:
            # Paper: calculate PnL
            entry_price = self.strategy.get_entry_price()
            current_qty = current_holdings.get(symbol, 0)
            
            if entry_price and current_qty > 0:
                pnl = (signal.price - entry_price) * current_qty
                paper_pnl = pnl  # No fees in simple paper
                
                # Record paper trade
                trade = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "SELL",
                    "symbol": symbol,
                    "price": signal.price,
                    "quantity": current_qty,
                    "paper_pnl": paper_pnl,
                    "entry_price": entry_price,
                    "status": "SIMULATED",
                }
                self._paper_trades.append(trade)
                self._save_paper_trades()
                
                # Update paper portfolio
                self.paper_portfolio.record_trade(
                    symbol=symbol,
                    action="SELL",
                    price=signal.price,
                    quantity=current_qty,
                    pnl=paper_pnl,
                )
                
                result["executed"] = True
                result["paper_pnl"] = paper_pnl
                result["return_pct"] = (signal.price - entry_price) / entry_price * 100
                
                # Clear strategy state after successful sell
                self.strategy._position = None
                self.strategy._entry_price = None
                self.strategy._entry_time = None

        else:
            # Live: sell on exchange
            try:
                gw = self._base_gateway
                order_id = gw.close_position()
                
                result["executed"] = True
                result["order_id"] = order_id
                
            except Exception as e:
                result["error"] = str(e)
                result["executed"] = False

        return result

    def get_report(self) -> dict:
        """Generate a summary report of recent trading activity."""
        report = {
            "paper_trading": self.settings.paper_trading,
            "strategy": "rotation",
            "current_position": self.strategy.get_position(),
            "entry_price": self.strategy.get_entry_price(),
            "total_paper_trades": len(self._paper_trades),
            "recent_trades": self._paper_trades[-10:] if self._paper_trades else [],
        }
        
        if self._paper_trades:
            buys = [t for t in self._paper_trades if t["action"] == "BUY"]
            sells = [t for t in self._paper_trades if t["action"] == "SELL"]
            report["total_buys"] = len(buys)
            report["total_sells"] = len(sells)
            
            # Calculate paper PnL
            total_pnl = sum(t.get("paper_pnl", 0) for t in self._paper_trades if t.get("paper_pnl"))
            report["total_paper_pnl"] = total_pnl
            
            # Average return
            returns = [t.get("return_pct", 0) for t in self._paper_trades if t.get("return_pct")]
            report["average_return_pct"] = sum(returns) / max(len(returns), 1)
            
            # Win rate
            winning_sells = [t for t in self._paper_trades if t.get("action") == "SELL" and t.get("paper_pnl", 0) > 0]
            report["win_rate"] = len(winning_sells) / max(len(sells), 1)
            
        return report
