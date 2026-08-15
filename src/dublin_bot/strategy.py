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
            "momentum": s.rsi_min <= float(row["rsi"]) <= s.rsi_max,
            # A breakout is a close near the prior resistance (within 1 ATR). In a
            # ranging/grinding market this is the actionable setup; we do not
            # require a strict print above the 20-bar high.
            "breakout": price >= float(row["prior_resistance"]) - atr,
        }
        # Advisory only — they shape the score and the note, but never block an
        # entry on their own. This is what lets the bot trade in choppy/flat
        # sessions instead of waiting forever for a full uptrend.
        advisory = {
            "regime": price > float(row["ema_regime"]),
            "trend": float(row["ema_fast"]) > float(row["ema_slow"]),
            "volume": float(row["volume_ratio"]) >= s.min_volume_ratio,
        }
        score = (sum(checks.values()) * 20) + (sum(advisory.values()) * 5)
        failed = [name for name, passed in checks.items() if not passed]

        # Fire when momentum and breakout both pass. Risk limits (size, orders/day,
        # daily-loss, drawdown) are still enforced downstream in risk.py.
        if not failed:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            note = "Momentum + breakout confirmed"
            if not advisory["regime"]:
                note += " (counter-trend)"
            if not advisory["volume"]:
                note += "; low volume (advisory)"
            return Signal(Action.BUY, score, note, price, atr, stop_price)

        return Signal(Action.WAIT, score, f"Filters failed: {', '.join(failed)}", price, atr)


