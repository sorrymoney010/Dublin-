"""
Trading System Coordinator: ties together strategy + trading agent + monitoring agent.

Usage:
    from dublin_bot.coordinator import TradingSystem
    
    system = TradingSystem(paper_trading=True)  # Start paper trading
    system.start()
    
    # Or for live trading:
    system = TradingSystem(paper_trading=False)
    system.start()
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dublin_bot.config import Settings
from dublin_bot.agents.trading_agent import TradingAgent
from dublin_bot.agents.monitoring_agent import MonitoringAgent


class TradingSystem:
    """
    Main coordinator: runs the trading loop with monitoring.
    
    Paper trading mode (default): simulates trades, tracks PnL.
    Live mode: trades on exchange with real money.
    """

    def __init__(
        self,
        paper_trading: bool = True,
        strategy: str = "rotation",
        check_interval_seconds: int = 30,
        monitor_interval_seconds: int = 60,
    ):
        self.paper_trading = paper_trading
        self.strategy_name = strategy
        self.check_interval = check_interval_seconds
        self.monitor_interval = monitor_interval_seconds
        
        # Build settings
        self.settings = Settings()
        self.settings.paper_trading = paper_trading
        self.trading_agent = TradingAgent(self.settings)
        self.monitoring_agent = MonitoringAgent(
            self.trading_agent,
            check_interval_seconds=monitor_interval_seconds,
            report_callback=self._on_report,
        )
        
        # Control
        self._running = False
        self._report_callbacks: list = []
        
    def add_report_callback(self, callback):
        """Add a callback to receive reports."""
        self._report_callbacks.append(callback)
        self.monitoring_agent.report_callback = self._dispatch_report
    
    def _dispatch_report(self, report: dict):
        """Dispatch report to all callbacks."""
        for cb in self._report_callbacks:
            try:
                cb(report)
            except Exception:
                pass
    
    def _on_report(self, report: dict):
        """Default report handler - prints to console."""
        self._print_report(report)
    
    def _print_report(self, report: dict):
        """Print a human-readable report."""
        timestamp = report.get("timestamp", "unknown")
        mode = "PAPER" if report.get("paper_trading") else "LIVE"
        
        print(f"\n{'='*60}")
        print(f"[{timestamp}] {mode} TRADING REPORT")
        print(f"{'='*60}")
        
        if report.get("last_confirmed_trade"):
            trade = report["last_confirmed_trade"]
            print(f"\n>>> LAST TRADE CONFIRMED:")
            print(f"    Action:     {trade.get('action')}")
            print(f"    Symbol:     {trade.get('symbol')}")
            print(f"    Price:      {trade.get('price')}")
            print(f"    Quantity:   {trade.get('quantity')}")
            print(f"    Mode:       {trade.get('mode')}")
            print(f"    Status:     {trade.get('status')}")
            
            if trade.get("pnl"):
                pnl = trade["pnl"]
                sign = "+" if pnl > 0 else ""
                print(f"    PnL:        {sign}{pnl:.2f} ({sign}{trade.get('return_pct', 0):.2f}%)")
                print(f"    RESULT:     {'WIN' if pnl > 0 else 'LOSS'}")
        
        if report.get("stats"):
            stats = report["stats"]
            print(f"\n--- STATISTICS ---")
            print(f"    Total trades:  {stats.get('total_confirmed_trades')}")
            print(f"    Wins:          {stats.get('wins')}")
            print(f"    Losses:        {stats.get('losses')}")
            print(f"    Win rate:      {stats.get('win_rate', 0)*100:.1f}%")
            print(f"    Total PnL:     {stats.get('total_pnl', 0):.2f}")
            print(f"    Avg win:       {stats.get('average_win', 0):.2f}")
            print(f"    Avg loss:      {stats.get('average_loss', 0):.2f}")
        
        if report.get("anomalies"):
            print(f"\n!!! ANOMALIES !!!")
            for a in report["anomalies"]:
                print(f"    - {a}")
        
        print(f"{'='*60}\n")
    
    def start(self):
        """Start the trading system."""
        self._running = True
        
        # Start monitoring agent
        self.monitoring_agent.start_monitoring()
        
        print(f"\n{'#'*60}")
        print(f"# TRADING SYSTEM STARTED")
        print(f"# Mode: {'PAPER TRADING' if self.paper_trading else 'LIVE TRADING'}")
        print(f"# Strategy: {self.strategy_name}")
        print(f"# Check interval: {self.check_interval}s")
        print(f"# Monitor interval: {self.monitor_interval}s")
        print(f"# Coins: {self.settings.universe_allowlist or self.settings.coin_basket}")
        print(f"# Equity: ${self.settings.strategy_equity_usd}")
        print(f"{'#'*60}\n")
        
        # Handle graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        try:
            self._run_loop()
        finally:
            self.stop()
    
    def _run_loop(self):
        """Main trading loop."""
        while self._running:
            try:
                # Get report from monitoring agent (which checks trading agent)
                report = self.monitoring_agent.force_check()
                
                # Print summary
                self._print_summary(report)
                
            except Exception as e:
                print(f"ERROR in trading loop: {e}")
                import traceback
                traceback.print_exc()
            
            # Wait before next cycle
            time.sleep(self.check_interval)
    
    def _print_summary(self, report: dict):
        """Print a quick summary (not full report every time)."""
        mode = "PAPER" if report.get("paper_trading") else "LIVE"
        position = report.get("last_trade", {}).get("symbol", "NONE")
        action = report.get("last_trade", {}).get("action", "-")
        
        # Only print if something happened
        if report.get("last_confirmed_trade"):
            trade = report["last_confirmed_trade"]
            pnl = trade.get("pnl", 0)
            sign = "+" if pnl > 0 else ""
            result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "EVEN")
            print(f"[{mode}] {trade.get('action')} {trade.get('symbol')} @ {trade.get('price')} | PnL: {sign}{pnl:.2f} | {result}")
        
        # Print periodic stats
        stats = report.get("stats", {})
        if stats.get("total_confirmed_trades", 0) % 5 == 0 and stats.get("total_confirmed_trades", 0) > 0:
            print(f"[{mode}] Stats: {stats.get('wins')}W/{stats.get('losses')}L | Win rate: {stats.get('win_rate', 0)*100:.1f}% | PnL: {stats.get('total_pnl', 0):.2f}")
    
    def stop(self):
        """Stop the trading system."""
        self._running = False
        self.monitoring_agent.stop_monitoring()
        self._save_state()
        print("\nTrading system stopped.")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        print("\nShutdown signal received...")
        self.stop()
        sys.exit(0)
    
    def _save_state(self):
        """Save final state."""
        self.monitoring_agent._save_state()
    
    def get_status(self) -> dict:
        """Get current system status."""
        return {
            "running": self._running,
            "paper_trading": self.paper_trading,
            "strategy": self.strategy_name,
            "current_position": self.trading_agent.strategy.get_position(),
            "entry_price": self.trading_agent.strategy.get_entry_price(),
            "stats": self.monitoring_agent.get_stats(),
            "recent_trades": self.monitoring_agent.get_recent_trades(5),
        }


def run_paper_trading():
    """Run the system in paper trading mode."""
    print("\nStarting PAPER TRADING mode...")
    system = TradingSystem(paper_trading=True)
    system.start()
    return system


def run_live_trading():
    """Run the system in live trading mode."""
    print("\nStarting LIVE TRADING mode...")
    system = TradingSystem(paper_trading=False)
    system.start()
    return system


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Dublin Trading System")
    parser.add_argument("--live", action="store_true", help="Run in live trading mode")
    parser.add_argument("--paper", action="store_true", help="Run in paper trading mode (default)")
    parser.add_argument("--interval", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--monitor", type=int, default=60, help="Monitor check interval in seconds")
    parser.add_argument("--strategy", type=str, default="rotation", help="Strategy to use")
    
    args = parser.parse_args()
    
    paper = not args.live
    
    system = TradingSystem(
        paper_trading=paper,
        strategy=args.strategy,
        check_interval_seconds=args.interval,
        monitor_interval_seconds=args.monitor,
    )
    
    print(f"\nStarting with config:")
    print(f"  Paper trading: {paper}")
    print(f"  Strategy: {args.strategy}")
    print(f"  Check interval: {args.interval}s")
    print(f"  Monitor interval: {args.monitor}s")
    print(f"  Coins: {system.settings.universe_allowlist or system.settings.coin_basket}")
    print(f"  Risk per trade: {system.settings.risk_per_trade*100:.0f}%")
    print(f"  Daily loss limit: {system.settings.max_daily_loss_fraction*100:.0f}%")
    print()
    
    system.start()
