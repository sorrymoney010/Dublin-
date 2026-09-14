"""
Monitoring Agent: watches the trading agent, confirms trades, reports wins/losses.

Runs continuously, checking every N seconds:
- Did a trade execute?
- Confirm it on the exchange (or paper ledger)
- Report: symbol, action, price, quantity, Pnl, status
- Track wins and losses separately
- Alert on problems

This is the "eyes" that make sure the trading agent isn't lying or broken.
"""

from __future__ import annotations

import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from dublin_bot.config import Settings
from dublin_bot.agents.trading_agent import TradingAgent


class MonitoringAgent:
    """
    Watches the trading agent and confirms everything.
    
    Every cycle:
    1. Check if the trading agent executed a trade
    2. Verify it against the exchange/paper ledger
    3. Report confirmed trade details
    4. Track win/loss statistics
    5. Alert on anomalies
    
    Can run as a separate thread or be polled.
    """

    def __init__(
        self,
        trading_agent: TradingAgent,
        report_callback: Optional[Callable[[dict], None]] = None,
        check_interval_seconds: int = 60,
        log_path: str = "logs/monitor_report.json",
    ):
        self.trading_agent = trading_agent
        self.report_callback = report_callback
        self.check_interval = check_interval_seconds
        self.log_path = Path(log_path)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        
        # Tracking state
        self._last_confirmed_trade: Optional[dict] = None
        self._wins: list[dict] = []
        self._losses: list[dict] = []
        self._confirmed_trades: list[dict] = []
        
        # Load previous state
        self._load_state()
        
    def _load_state(self):
        """Load previously confirmed trades and stats."""
        if self.log_path.exists():
            try:
                with open(self.log_path) as f:
                    data = json.load(f)
                    self._confirmed_trades = data.get("confirmed_trades", [])
                    self._wins = data.get("wins", [])
                    self._losses = data.get("losses", [])
            except (json.JSONDecodeError, KeyError):
                pass
    
    def _save_state(self):
        """Persist current state."""
        data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "confirmed_trades": self._confirmed_trades,
            "wins": self._wins,
            "losses": self._losses,
            "total_wins": len(self._wins),
            "total_losses": len(self._losses),
            "win_rate": len(self._wins) / max(len(self._confirmed_trades), 1),
        }
        with open(self.log_path, "w") as f:
            json.dump(data, f, indent=2)
    
    def start_monitoring(self):
        """Start the monitoring loop in a background thread."""
        if self._thread and self._thread.is_alive():
            return  # Already running
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self._thread.start()
    
    def stop_monitoring(self):
        """Stop the monitoring loop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
    
    def _monitoring_loop(self):
        """Main monitoring loop: check every N seconds."""
        while not self._stop_event.is_set():
            self.check_and_report()
            self._stop_event.wait(self.check_interval)
    
    def check_and_report(self) -> dict:
        """
        Check the trading agent's state, confirm trades, report.
        
        Returns a report dict with:
        - last_trade: what happened last
        - wins: list of winning trades
        - losses: list of losing trades
        - stats: win rate, total Pnl, etc.
        - anomalies: anything suspicious
        """
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper_trading": self.trading_agent.settings.paper_trading,
            "last_trade": self._last_confirmed_trade,
            "pending_conflicts": [],
            "anomalies": [],
            "stats": self.get_stats(),
        }
        
        # Get the trading agent's latest report
        try:
            agent_report = self.trading_agent.scan_and_decide()
            report["agent_decision"] = agent_report
        except Exception as e:
            report["anomalies"].append(f"Agent report failed: {e}")
            return report
        
        # Check if a trade was executed and confirm it
        if agent_report.get("executed") and agent_report.get("order_result"):
            result = agent_report["order_result"]
            
            # Verify the trade
            confirmed_trade = self._confirm_trade(result, agent_report)
            
            if confirmed_trade:
                self._last_confirmed_trade = confirmed_trade
                self._confirmed_trades.append(confirmed_trade)
                
                # Classify as win or loss
                if confirmed_trade.get("pnl", 0) > 0:
                    self._wins.append(confirmed_trade)
                elif confirmed_trade.get("pnl", 0) < 0:
                    self._losses.append(confirmed_trade)
                
                # Save state
                self._save_state()
                
                report["last_confirmed_trade"] = confirmed_trade
                
                # Call callback if provided
                if self.report_callback:
                    try:
                        self.report_callback(confirmed_trade)
                    except Exception:
                        pass
        
        # Check for anomalies
        anomalies = self._check_anomalies(agent_report)
        report["anomalies"] = anomalies
        
        # If callback provided, send full report
        if self.report_callback and not report["anomalies"]:
            try:
                self.report_callback(report)
            except Exception:
                pass
        
        return report
    
    def _confirm_trade(self, result: dict, agent_report: dict) -> Optional[dict]:
        """
        Confirm a trade actually happened.
        
        In paper mode: verify against paper_trades.json ledger.
        In live mode: verify against exchange orders.
        
        Returns the confirmed trade dict, or None if unconfirmed.
        """
        if result.get("mode") == "paper" or self.trading_agent.settings.paper_trading:
            # Paper mode: check the paper trades ledger
            paper_trades = self.trading_agent._load_paper_trades()
            
            # Find the most recent trade matching this result
            for trade in reversed(paper_trades):
                if (trade.get("action") == result.get("action") and
                    trade.get("symbol") == result.get("symbol") and
                    abs(trade.get("price", 0) - result.get("price", 0)) < 0.0001):
                    
                    # Calculate PnL if it's a SELL
                    confirmed = {
                        "timestamp": trade.get("timestamp"),
                        "action": trade.get("action"),
                        "symbol": trade.get("symbol"),
                        "price": trade.get("price"),
                        "quantity": trade.get("quantity"),
                        "mode": "paper",
                        "status": trade.get("status"),
                    }
                    
                    if trade.get("action") == "SELL":
                        entry_price = trade.get("entry_price", 0)
                        if entry_price:
                            confirmed["pnl"] = (trade["price"] - entry_price) * trade.get("quantity", 0)
                            confirmed["return_pct"] = (trade["price"] - entry_price) / entry_price * 100
                    
                    return confirmed
            
            # Could not confirm in paper ledger
            return None
        
        else:
            # Live mode: verify with exchange if possible
            # For now, accept the order result as confirmation
            # In production, would query open orders / trade history
            confirmed = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": result.get("action"),
                "symbol": result.get("symbol"),
                "price": result.get("price"),
                "order_id": result.get("order_id"),
                "mode": "live",
                "status": "SUBMITTED",
            }
            
            # Try to get actual fill if order completed
            try:
                order = self.trading_agent.gateway.get_order(result.get("order_id"))
                if order:
                    confirmed["status"] = order.get("status", "UNKNOWN")
                    confirmed["fill_price"] = order.get("fill_price")
                    confirmed["fill_quantity"] = order.get("fill_quantity")
                    
                    # Calculate PnL
                    if order.get("status") == "FILLED" and result.get("action") == "SELL":
                        entry_price = self.trading_agent.strategy.get_entry_price()
                        if entry_price:
                            qty = order.get("fill_quantity", 0)
                            confirmed["pnl"] = (order.get("fill_price", 0) - entry_price) * qty
                            confirmed["return_pct"] = (order.get("fill_price", 0) - entry_price) / entry_price * 100
            except Exception:
                pass
            
            return confirmed
    
    def _check_anomalies(self, agent_report: dict) -> list[str]:
        """Check for suspicious patterns."""
        anomalies = []
        
        # Check if trades are executing too fast (possible bot loop)
        if agent_report.get("executed"):
            current_time = time.time()
            if self._last_confirmed_trade:
                last_time = datetime.fromisoformat(
                    self._last_confirmed_trade.get("timestamp", "2000-01-01T00:00:00Z")
                ).timestamp()
                time_since_last = current_time - last_time
                
                if time_since_last < 2:  # Less than 2 seconds between trades
                    anomalies.append(f"Rapid trading: {time_since_last:.1f}s since last trade")
        
        # Check if paper PnL is swinging wildly
        if self._confirmed_trades:
            recent_pnls = [
                t.get("pnl", 0) for t in self._confirmed_trades[-5:]
                if t.get("pnl")
            ]
            if len(recent_pnls) >= 3:
                avg = sum(recent_pnls) / len(recent_pnls)
                if abs(avg) > 50 and self.trading_agent.settings.strategy_equity_usd < 1000:
                    anomalies.append(f"Large PnL swings relative to equity: avg {avg:.2f}")
        
        return anomalies
    
    def get_stats(self) -> dict:
        """Get current win/loss statistics."""
        return {
            "total_confirmed_trades": len(self._confirmed_trades),
            "wins": len(self._wins),
            "losses": len(self._losses),
            "win_rate": len(self._wins) / max(len(self._confirmed_trades), 1),
            "total_pnl": sum(t.get("pnl", 0) for t in self._confirmed_trades if t.get("pnl")),
            "average_win": (
                sum(t.get("pnl", 0) for t in self._wins if t.get("pnl")) / max(len(self._wins), 1)
            ),
            "average_loss": (
                sum(t.get("pnl", 0) for t in self._losses if t.get("pnl")) / max(len(self._losses), 1)
            ),
            "paper_trading": self.trading_agent.settings.paper_trading,
        }
    
    def get_recent_trades(self, n: int = 10) -> list[dict]:
        """Get the N most recent confirmed trades."""
        return self._confirmed_trades[-n:]
    
    def force_check(self) -> dict:
        """Manually trigger a check (for testing or on-demand)."""
        return self.check_and_report()
