from __future__ import annotations

import math
import pandas as pd

from .config import Settings
from .indicators import enrich
from .models import Action, Signal


class TrendBreakoutStrategy:
    """COO-style weighted decision layer.

    Instead of requiring every entry filter to be perfect, the strategy scores
    independent evidence. Risk and execution gates remain separate and can still
    veto the trade.
    """

    WEIGHTS = {
        "regime": 22,
        "trend": 20,
        "momentum": 16,
        "breakout": 18,
        "volume": 12,
        "volatility": 12,
    }

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
        if not math.isfinite(atr) or atr <= 0 or price <= 0:
            return Signal(Action.WAIT, 0, "ATR/price unavailable", max(price, 0.0))

        atr_fraction = atr / price
        regime_ok = price > float(row["ema_regime"])
        trend_ok = float(row["ema_fast"]) > float(row["ema_slow"])
        rsi = float(row["rsi"])
        momentum_ok = s.rsi_min <= rsi <= s.rsi_max
        breakout_ok = price > float(row["prior_resistance"])
        volume_ok = float(row["volume_ratio"]) >= s.min_volume_ratio
        volatility_ok = s.coo_min_atr_fraction <= atr_fraction <= s.coo_max_atr_fraction

        checks = {
            "regime": regime_ok,
            "trend": trend_ok,
            "momentum": momentum_ok,
            "breakout": breakout_ok,
            "volume": volume_ok,
            "volatility": volatility_ok,
        }
        score = sum(self.WEIGHTS[name] for name, passed in checks.items() if passed)

        if in_position:
            # Exit logic deliberately reacts faster than entry logic. A broken
            # regime or trend is enough to materially raise exit conviction.
            exit_score = 0
            reasons: list[str] = []
            if price < float(row["ema_slow"]):
                exit_score += 45
                reasons.append("below slow EMA")
            if not regime_ok:
                exit_score += 30
                reasons.append("below regime EMA")
            if rsi < max(35.0, s.rsi_min - 10):
                exit_score += 15
                reasons.append("momentum deterioration")
            if not volatility_ok:
                exit_score += 10
                reasons.append("volatility outside COO band")
            if exit_score >= s.coo_exit_score:
                return Signal(Action.SELL, min(exit_score, 100),
                              "COO exit: " + ", ".join(reasons), price, atr)
            return Signal(Action.WAIT, max(0, 100 - exit_score),
                          "COO hold: trend structure still acceptable", price, atr)

        confidence = score / 100.0
        if score < s.coo_entry_score or confidence < s.coo_confidence_floor:
            failed = [name for name, passed in checks.items() if not passed]
            return Signal(
                Action.WAIT,
                score,
                f"COO score {score}/100 below entry threshold; weak: {', '.join(failed) or 'none'}",
                price,
                atr,
            )

        # Wider stops during higher volatility, but always bounded by the
        # configured multiplier so position sizing remains deterministic.
        stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
        passed = [name for name, ok in checks.items() if ok]
        return Signal(
            Action.BUY,
            score,
            f"COO entry score {score}/100: {', '.join(passed)}",
            price,
            atr,
            stop_price,
        )
