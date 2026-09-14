"""
Rotation Strategy for fast, multi-coin momentum trading.

Rules:
- Scan ALL allowed coins every cycle
- Buy when momentum turns up (RSI rising + price starting to move)
- Exit on: profit target reached, stop loss hit, or momentum dying
- After any exit, immediately scan for next opportunity
- Designed for "boom boom boom" action, not waiting around

Not magic. Just rules. The bot follows the rules, doesn't guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal


@dataclass
class RotationSetup:
    """A detected entry opportunity across one coin."""
    symbol: str
    score: float
    price: float
    rsi: float
    reason: str
    stop_price: float
    atr: float


class RotationStrategy:
    """
    Momentum rotation: find the best momentum setup across all allowed coins,
    buy it, track it, exit on profit/loss/reversal, then find the next.
    
    Key differences from mean_reversion:
    - Scans ALL coins, picks the best setup (not just current symbol)
    - Buys when momentum TURNS up (faster than waiting for oversold)
    - Exits on profit target (not waiting for perfect reversal)
    - Designed for rotation between coins
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._position: Optional[str] = None  # current coin we hold
        self._entry_price: Optional[float] = None
        self._entry_time: Optional[float] = None

    def reset(self):
        """Clear position state (for paper trading reset, new session, etc.)"""
        self._position = None
        self._entry_price = None
        self._entry_time = None

    def get_position(self) -> Optional[str]:
        """Return the symbol we currently hold, or None."""
        return self._position

    def get_entry_price(self) -> Optional[float]:
        return self._entry_price

    def evaluate(self, bars: object, in_position: bool = False) -> Signal:
        """
        Evaluate a single coin's bars and return a trading signal.
        
        Args:
            bars: pandas DataFrame with OHLCV data (must have 'close', 'volume' columns)
            in_position: whether we currently hold this coin
        
        Returns:
            Signal with BUY/SELL/WAIT action
        """
        import pandas as pd
        from dublin_bot.models import Action
        
        if bars is None or len(bars) < 20:
            return Signal(Action.WAIT, 0, "Insufficient data", price=0.0)
        
        # Get current price
        current_price = float(bars["close"].iloc[-1])
        
        # Calculate RSI
        delta = bars["close"].diff()
        gain = delta.clip(lower=0).rolling(window=self.settings.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(window=self.settings.rsi_period).mean()
        rs = gain / loss.replace(0, 1)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        # Calculate momentum (price change over lookback)
        lookback = min(self.settings.lookback_bars, len(bars))
        if lookback < 5:
            return Signal(Action.WAIT, 0, "Not enough bars for momentum", price=current_price)
        
        price_change_pct = (bars["close"].iloc[-1] - bars["close"].iloc[-lookback]) / bars["close"].iloc[-lookback]
        
        # Calculate volume trend
        avg_volume = bars["volume"].iloc[-lookback:].mean()
        current_volume = bars["volume"].iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
        
        # Determine action
        if in_position:
            # We hold this coin - check for exit signals
            # Use entry_price for actual PnL calculation
            if self._entry_price and self._entry_price > 0:
                entry_gain = (current_price - self._entry_price) / self._entry_price
            else:
                entry_gain = 0.0
            
            # Exit on profit target (3%)
            if entry_gain >= 0.03:
                return Signal(Action.SELL, 90, f"Profit target hit: +{entry_gain*100:.1f}%", price=current_price)
            # Exit on stop loss (2%)
            if entry_gain <= -0.02:
                return Signal(Action.SELL, 95, f"Stop loss hit: {entry_gain*100:.1f}%", price=current_price)
            # Exit on momentum reversal (RSI dropping below 40 after being high)
            if current_rsi < 40 and rsi.iloc[-2] > 50:
                return Signal(Action.SELL, 70, f"Momentum reversing (RSI {current_rsi:.1f})", price=current_price)
            # Exit on volume drying up
            if volume_ratio < 0.5:
                return Signal(Action.SELL, 60, f"Volume drying up (ratio {volume_ratio:.2f})", price=current_price)
            # Hold
            return Signal(Action.WAIT, 50, f"Holding, gain={entry_gain*100:.1f}%, RSI={current_rsi:.1f}", price=current_price)
        else:
            # We don't hold - check for entry signals
            # Entry: momentum-based rotation entry
            # Buy when momentum is positive (price moving up), regardless of RSI level
            # This is for "boom boom boom" - chase the momentum
            if price_change_pct > 0.005:  # At least 0.5% up over lookback
                volume_boost = min(20, (volume_ratio - 1.0) * 10)  # Volume surge bonus
                momentum_score = min(50, price_change_pct * 100 * 2)  # Up to 50 points for momentum
                rsi_score = max(0, 30 - abs(current_rsi - 55))  # Best around RSI 55
                score = int(momentum_score + volume_boost + rsi_score + 30)  # Base 30
                score = min(95, score)  # Cap at 95
                
                reason_parts = [f"Momentum: +{price_change_pct*100:.2f}%"]
                if volume_ratio > 1.5:
                    reason_parts.append(f"vol {volume_ratio:.2f}x")
                if current_rsi < 70:
                    reason_parts.append(f"RSI {current_rsi:.1f}")
                
                return Signal(Action.BUY, score, 
                             ", ".join(reason_parts),
                             price=current_price)
            
            # If momentum is slightly negative but RSI is low, might be a dip buy
            if current_rsi < 40 and price_change_pct > -0.02:
                return Signal(Action.BUY, 35, 
                             f"Oversold RSI {current_rsi:.1f}, slight dip",
                             price=current_price)
            
            # No setup
            return Signal(Action.WAIT, 5, 
                         f"Waiting: RSI={current_rsi:.1f}, mom={price_change_pct*100:.2f}%, vol={volume_ratio:.2f}x",
                         price=current_price)

    def evaluate_all_coins(
        self,
        symbol_signals: dict[str, Signal],
        current_holdings: dict[str, float],
    ) -> Signal:
        """
        Evaluate all coins and pick the best action.
        
        Args:
            symbol_signals: {symbol: Signal} from evaluating each coin's bars
            current_holdings: {symbol: quantity} what we actually hold
        
        Returns:
            Signal for the best action across ALL coins
        """
        # 1. First priority: if we HOLD a coin, decide whether to SELL it
        if self._position and self._position in symbol_signals:
            held_signal = symbol_signals[self._position]
            if held_signal.action == Action.SELL:
                # Return SELL signal WITHOUT clearing state here.
                # The trading agent will clear after executing the sell.
                # This avoids a race where get_position() returns None before _execute_sell runs.
                return held_signal

        # 2. Second priority: if we have cash, find the best BUY
        if self._position is None:
            # Filter to coins with BUY signals
            buy_signals = [
                (sym, sig) for sym, sig in symbol_signals.items()
                if sig.action == Action.BUY
            ]

            if not buy_signals:
                # No good buys anywhere - wait
                return Signal(
                    Action.WAIT, 5,
                    "No momentum setups found across any coin",
                    price=0.0
                )

            # Pick the best one by score
            best_sym, best_sig = max(buy_signals, key=lambda x: x[1].score)

            # Track this position
            self._position = best_sym
            self._entry_price = best_sig.price
            self._entry_time = 0  # would be time.time() in real execution

            return best_sig

        # 3. We're holding a position and it's not telling us to sell yet
        # Check if that position's signal changed
        held_signal = symbol_signals.get(self._position)
        if held_signal and held_signal.action == Action.WAIT:
            return held_signal

        # Holding, no sell signal yet
        return Signal(
            Action.WAIT, 50,
            f"Holding {self._position}, watching for exit",
            price=self._entry_price or 0.0
        )

    def should_exit_for_profit(self, current_price: float, profit_pct: float = 0.03) -> bool:
        """Check if current position has hit profit target."""
        if not self._position or not self._entry_price:
            return False
        gain = (current_price - self._entry_price) / self._entry_price
        return gain >= profit_pct

    def should_exit_for_loss(self, current_price: float, loss_pct: float = 0.02) -> bool:
        """Check if current position has hit stop loss."""
        if not self._position or not self._entry_price:
            return False
        loss = (self._entry_price - current_price) / self._entry_price
        return loss >= loss_pct

    def get_holdings(self) -> dict[str, float]:
        """Return what we hold. For paper trading, this is tracked internally."""
        if self._position and self._entry_price:
            # Estimate quantity based on config (paper trading uses strategy_equity_usd)
            equity = getattr(self.settings, 'strategy_equity_usd', 100.0)
            qty = (equity * 0.25) / self._entry_price  # 25% of equity per position
            return {self._position: qty}
        return {}
