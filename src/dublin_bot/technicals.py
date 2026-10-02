"""The ONE technical/microstructure feature module (live == backtest).

Both the paper strategies (via ``backtest_core.add_indicators``) and the
walk-forward / order-flow study call :func:`add_technicals`, so a feature
value on a given closed bar is computed by identical code everywhere.

Core columns (unchanged formulas from the original ``backtest_core``):
    ema20, ema50, ema200, rsi (14, Wilder), atr (14, Wilder), atr_pct,
    adx (14, Wilder via ``indicators.compute_adx``), atr_rank (ATR% pct-rank
    over the trailing 200 bars).
Volume:
    vol_z      – volume z-score vs the trailing 20 bars (incl. the bar).
Order flow (only when the bar frame carries tick-built ``buy_vol``/``sell_vol``;
NaN otherwise, e.g. REST-only bars):
    ofi        – (buy_vol - sell_vol) / volume of the bar, in [-1, 1]
                 (taker-initiated volume imbalance).
    ofi_z      – ofi z-score vs the trailing 50 bars.
    flow3      – 3-bar net taker flow / 3-bar volume (smoothed ofi).
    cvd        – cumulative (buy_vol - sell_vol) over the frame.
    spread_bps – mean quoted spread in the bar (when quotes were recorded).

Every value at bar i only uses bars <= i (no look-ahead).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import compute_adx

CORE_COLUMNS = ("ema20", "ema50", "ema200", "rsi", "atr", "atr_pct", "adx", "atr_rank")
FLOW_COLUMNS = ("ofi", "ofi_z", "flow3", "cvd")


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def zscore(s: pd.Series, n: int, min_periods: int | None = None) -> pd.Series:
    mp = min_periods or max(5, n // 2)
    m = s.rolling(n, min_periods=mp).mean()
    sd = s.rolling(n, min_periods=mp).std(ddof=0)
    return (s - m) / sd.replace(0, np.nan)


def add_core(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema20"] = ema(d["close"], 20)
    d["ema50"] = ema(d["close"], 50)
    d["ema200"] = ema(d["close"], 200)
    d["rsi"] = rsi(d["close"], 14)
    d["atr"] = atr(d, 14)
    d["atr_pct"] = d["atr"] / d["close"]
    d["adx"] = compute_adx(d["high"], d["low"], d["close"], 14)
    # ATR% percentile vs the trailing 200 bars (volatility-expansion gate).
    d["atr_rank"] = d["atr_pct"].rolling(200, min_periods=50).rank(pct=True)
    return d


def add_volume_flow(d: pd.DataFrame) -> pd.DataFrame:
    """Volume and order-flow features (in place on a copy made by the caller)."""
    if "volume" in d:
        d["vol_z"] = zscore(d["volume"].astype(float), 20)
    if "buy_vol" in d and "sell_vol" in d:
        buy = d["buy_vol"].astype(float)
        sell = d["sell_vol"].astype(float)
        tot = buy + sell
        net = buy - sell
        d["ofi"] = net / tot.replace(0, np.nan)
        d["ofi_z"] = zscore(d["ofi"], 50)
        d["flow3"] = net.rolling(3, min_periods=3).sum() / tot.rolling(3, min_periods=3).sum().replace(0, np.nan)
        d["cvd"] = net.fillna(0.0).cumsum().where(net.notna())
    else:
        for c in FLOW_COLUMNS:
            d[c] = np.nan
    return d


def add_technicals(df: pd.DataFrame) -> pd.DataFrame:
    """Core indicators + volume z-score + order-flow features."""
    return add_volume_flow(add_core(df))


# ── order-flow entry filters (study candidates; off unless configured) ──
# Each maps an indicator frame to a per-bar boolean "entry allowed" mask.
# Missing order-flow data (NaN, e.g. REST-only bars) never passes: a filter
# that is switched on fails CLOSED.
def _gt0(col: str):
    return lambda d: (d[col].to_numpy(float) > 0) if col in d else np.zeros(len(d), dtype=bool)


def _ofi_rising(d: pd.DataFrame) -> np.ndarray:
    if "ofi" not in d:
        return np.zeros(len(d), dtype=bool)
    o = d["ofi"].to_numpy(float)
    prev = np.concatenate([[np.nan], o[:-1]])
    return o > prev


FLOW_FILTERS = {
    "ofi_pos": _gt0("ofi"),        # signal bar had net taker BUY volume
    "ofi_z_pos": _gt0("ofi_z"),    # signal bar's imbalance above its 50-bar norm
    "flow3_pos": _gt0("flow3"),    # last 3 bars net taker buying
    "ofi_rising": _ofi_rising,     # imbalance improved vs the previous bar
}


def flow_filter_mask(d: pd.DataFrame, name: str | None) -> np.ndarray:
    if not name:
        return np.ones(len(d), dtype=bool)
    if name not in FLOW_FILTERS:
        raise ValueError(f"unknown flow filter {name!r}; known: {sorted(FLOW_FILTERS)}")
    with np.errstate(invalid="ignore"):
        return np.asarray(FLOW_FILTERS[name](d), dtype=bool)
