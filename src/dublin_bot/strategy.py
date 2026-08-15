from __future__ import annotations

import math
import pandas as pd

from .config import Settings
from .indicators import enrich
from .models import Action, Signal


class TrendBreakoutStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        s = self.settings
        frame = enrich(
            bars,
            fast_ema=s.fast_ema,
            slow_ema=s.slow_ema,
            regime_ema=s.regime_ema,
            rsi_period=s.rsi_period,
            atr_period=s.atr_period,
            breakout_lookback=s.breakout_lookback,
            volume_lookback=s.volume_lookback,
        ).dropna()
        if frame.empty:
            return Signal(Action.WAIT, 0, "Not enough completed bars", 0.0)

        row = frame.iloc[-1]
        price = float(row["close"])
        atr = float(row["atr"])
        if not math.isfinite(atr) or atr <= 0:
            return Signal(Action.WAIT, 0, "ATR unavailable", price)

        if in_position:
            if price < float(row["ema_slow"]):
                return Signal(Action.SELL, 90, "Price closed below slow trend EMA", price, atr)
            return Signal(Action.WAIT, 60, "Position remains above slow trend EMA", price, atr)

        checks = {
            "regime": price > float(row["ema_regime"]),
            "trend": float(row["ema_fast"]) > float(row["ema_slow"]),
            "momentum": s.rsi_min <= float(row["rsi"]) <= s.rsi_max,
            # A breakout is a close near the prior resistance. We allow a full-ATR
            # tolerance so a bar within striking distance of the level (not just a
            # strict print above it) can trigger — these are the setups that
            # actually occur in ranging/grinding markets, rather than the rare
            # clean breakout that strictly exceeds the 20-bar high.
            "breakout": price >= float(row["prior_resistance"]) - atr,
            # Volume is advisory: a low-volume breakout still trades but is
            # flagged, so we don't get permanently blocked in quiet sessions.
            "volume": float(row["volume_ratio"]) >= s.min_volume_ratio,
        }
        score = sum(checks.values()) * 20
        failed = [name for name, passed in checks.items() if not passed]

        # Path A — full trend-following breakout (all filters pass).
        if not failed:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            return Signal(Action.BUY, 100, "Trend, momentum, breakout and volume confirmed", price, atr, stop_price)

        # Path B — range / reversal entry. Fire when price is hugging the prior
        # resistance (breakout tolerance met) and momentum is not overbought, even
        # if the market is below its long-term trend. This lets the bot trade in
        # ranging/weak-downtrend sessions instead of waiting forever for a full
        # uptrend. Risk limits in risk.py still cap size, orders/day and drawdown.
        if "breakout" not in failed and "momentum" not in failed and "volume" in failed:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            return Signal(Action.BUY, 75, "Range/reversal entry: at resistance, momentum ok (low volume advisory)", price, atr, stop_price)
        if "breakout" not in failed and "momentum" not in failed and "regime" in failed and "trend" in failed:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            return Signal(Action.BUY, 70, "Range entry below trend: at resistance, momentum ok (counter-trend)", price, atr, stop_price)

        if failed == ["volume"]:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            return Signal(Action.BUY, 80, "Trend/breakout confirmed; low volume (advisory)", price, atr, stop_price)
        return Signal(Action.WAIT, score, f"Filters failed: {', '.join(failed)}", price, atr)


