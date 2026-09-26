"""Walk-forward optimizer for Dublin- strategies.

Maximizes the *stitched out-of-sample equity curve*, not in-sample luck.

This module does not place orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.models import Action
from dublin_bot.sizing import EdgeEstimate, PositionSizer, SizingConfig
from dublin_bot.strategy import build_strategy


@dataclass(frozen=True)
class SignalParams:
    strategy: str
    rsi_min: float
    rsi_max: float
    rsi_oversold: float
    rsi_exit: float
    atr_stop_multiplier: float
    breakout_lookback: int

    def apply(self, settings: Settings) -> Settings:
        settings.strategy = self.strategy
        settings.rsi_min = self.rsi_min
        settings.rsi_max = self.rsi_max
        settings.rsi_oversold = self.rsi_oversold
        settings.rsi_exit = self.rsi_exit
        settings.atr_stop_multiplier = self.atr_stop_multiplier
        settings.breakout_lookback = self.breakout_lookback
        return settings


@dataclass
class WFSplit:
    fold: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int


@dataclass
class ReplayMetrics:
    n_trades: int
    win_rate: float
    total_return: float
    sharpe: float
    max_dd: float
    turnover: float
    expectancy: float
    equity: list[float]
    score: float


@dataclass
class FoldResult:
    fold: int
    params: dict[str, Any]
    is_score: float
    oos: dict[str, float]


DEFAULT_GRID: list[SignalParams] = [
    SignalParams("mean_reversion", 35, 75, 32, 52, 1.5, 20),
    SignalParams("mean_reversion", 35, 75, 35, 55, 1.5, 20),
    SignalParams("mean_reversion", 35, 75, 38, 55, 1.8, 20),
    SignalParams("mean_reversion", 35, 75, 38, 58, 1.5, 14),
    SignalParams("momentum", 35, 70, 38, 55, 1.5, 20),
    SignalParams("momentum", 38, 72, 38, 55, 1.8, 20),
    SignalParams("momentum", 32, 75, 38, 55, 1.5, 14),
    SignalParams("momentum", 35, 75, 38, 55, 2.0, 24),
]


def rolling_splits(n_bars: int, is_bars: int, oos_bars: int, step_bars: int, embargo_bars: int = 4) -> list[WFSplit]:
    splits: list[WFSplit] = []
    start = 0
    fold = 0
    while start + is_bars + embargo_bars + oos_bars <= n_bars:
        is_end = start + is_bars
        oos_start = is_end + embargo_bars
        oos_end = oos_start + oos_bars
        splits.append(WFSplit(fold, start, is_end, oos_start, oos_end))
        start += step_bars
        fold += 1
    return splits


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0:
            max_dd = max(max_dd, (peak - x) / peak)
    return max_dd


def _sharpe(returns: list[float], periods_per_year: int) -> float:
    if len(returns) < 3:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = float(arr.std())
    if sd <= 0:
        return 0.0
    return float(arr.mean() / sd * np.sqrt(periods_per_year))


def score_metrics(m: ReplayMetrics, lam: float = 1.5, gamma: float = 0.15) -> float:
    if m.n_trades < 3:
        return -10.0
    return m.sharpe - lam * m.max_dd - gamma * m.turnover


def replay(
    bars: pd.DataFrame,
    settings: Settings,
    *,
    start_equity: float = 1000.0,
    fee_rate: float | None = None,
    slippage_bps: float | None = None,
    periods_per_year: int = 365,
) -> ReplayMetrics:
    if fee_rate is None:
        fee_rate = settings.paper_taker_fee_bps / 10_000.0
    if slippage_bps is None:
        slippage_bps = settings.paper_slippage_bps
    slip = slippage_bps / 10_000.0

    strat = build_strategy(settings)
    sizer = PositionSizer(
        SizingConfig(
            target_vol=settings.target_vol,
            kelly_fraction=settings.kelly_fraction,
            max_risk_per_trade=settings.risk_per_trade,
            max_leverage=settings.max_leverage,
        )
    )

    equity = start_equity
    curve = [equity]
    trade_rets: list[float] = []
    wins = 0
    n_trades = 0
    in_pos = False
    entry_px = entry_qty = entry_notional = 0.0
    returns = bars["close"].pct_change().fillna(0.0)

    for i in range(len(bars)):
        window = bars.iloc[: i + 1]
        raw_px = float(window.iloc[-1]["close"])
        if not in_pos:
            px = raw_px * (1.0 + slip)
            sig = strat.evaluate(window, in_position=False)
            if sig.action is not Action.BUY:
                continue
            stop = sig.stop_price or (px - max(float(getattr(sig, "atr", 0.0) or 0.0), px * 0.01) * settings.atr_stop_multiplier)
            stop_dist = max(px - float(stop), px * 0.005)
            sized = sizer.size(
                equity=equity,
                current_price=px,
                returns=returns.iloc[max(0, i - 60) : i + 1],
                stop_distance=stop_dist,
                edge=EdgeEstimate(win_prob=0.52, avg_win=1.2, avg_loss=1.0, confidence=0.6),
            )
            notional = min(sized.notional, equity * settings.max_position_fraction)
            if notional < settings.min_order_notional_usd:
                continue
            entry_px = px
            entry_notional = notional
            entry_qty = notional / px
            in_pos = True
        else:
            px = raw_px * (1.0 - slip)
            sig = strat.evaluate(window, in_position=True)
            stop_hit = px <= entry_px * (1.0 - settings.stop_loss_pct)
            take_hit = px >= entry_px * (1.0 + settings.take_profit_pct)
            if sig.action is Action.SELL or stop_hit or take_hit:
                gross = entry_qty * px
                fees = (entry_notional + gross) * fee_rate
                pnl = gross - entry_notional - fees
                ret = pnl / entry_notional if entry_notional else 0.0
                equity += pnl
                curve.append(equity)
                trade_rets.append(ret)
                n_trades += 1
                if pnl > 0:
                    wins += 1
                in_pos = False

    if in_pos:
        px = float(bars.iloc[-1]["close"]) * (1.0 - slip)
        gross = entry_qty * px
        fees = (entry_notional + gross) * fee_rate
        pnl = gross - entry_notional - fees
        equity += pnl
        curve.append(equity)
        trade_rets.append(pnl / entry_notional if entry_notional else 0.0)
        n_trades += 1
        if pnl > 0:
            wins += 1

    m = ReplayMetrics(
        n_trades=n_trades,
        win_rate=(wins / n_trades) if n_trades else 0.0,
        total_return=(equity / start_equity) - 1.0,
        sharpe=_sharpe(trade_rets, min(periods_per_year, 365)),
        max_dd=_max_drawdown(curve),
        turnover=n_trades / max(len(bars), 1),
        expectancy=float(np.mean(trade_rets)) if trade_rets else 0.0,
        equity=curve,
        score=0.0,
    )
    m.score = score_metrics(m)
    return m


def plateau_pick(scored: list[tuple[float, SignalParams]], top_k: int = 3) -> SignalParams:
    scored = sorted(scored, key=lambda x: x[0], reverse=True)
    top = scored[: max(1, min(top_k, len(scored)))]
    names = [p.strategy for _, p in top]
    strat = max(set(names), key=names.count)
    cohort = [p for _, p in top if p.strategy == strat]

    def avg(attr: str, cast=float):
        return cast(np.median([getattr(p, attr) for p in cohort]))

    return SignalParams(
        strategy=strat,
        rsi_min=avg("rsi_min"),
        rsi_max=avg("rsi_max"),
        rsi_oversold=avg("rsi_oversold"),
        rsi_exit=avg("rsi_exit"),
        atr_stop_multiplier=round(avg("atr_stop_multiplier"), 2),
        breakout_lookback=int(avg("breakout_lookback", int)),
    )


def run_walk_forward(
    bars: pd.DataFrame,
    base: Settings,
    *,
    grid: Iterable[SignalParams] | None = None,
    is_bars: int = 24 * 45,
    oos_bars: int = 24 * 14,
    step_bars: int = 24 * 14,
    embargo_bars: int = 6,
    start_equity: float = 1000.0,
    holdout_frac: float = 0.15,
) -> dict[str, Any]:
    grid = list(grid or DEFAULT_GRID)
    n = len(bars)
    if n < is_bars + oos_bars + 50:
        raise ValueError(f"Need more bars: have {n}, need > {is_bars + oos_bars + 50}")

    holdout_n = max(int(n * holdout_frac), oos_bars)
    research = bars.iloc[:-holdout_n].copy()
    holdout = bars.iloc[-holdout_n:].copy()
    splits = rolling_splits(len(research), is_bars, oos_bars, step_bars, embargo_bars)
    if not splits:
        raise ValueError("No WFO splits — shorten is_bars / oos_bars")

    folds: list[FoldResult] = []
    last_params: SignalParams | None = None
    bars_per_year = int(365 * 24 * 60 / max(base.timeframe_minutes, 1))

    for sp in splits:
        is_px = research.iloc[sp.is_start : sp.is_end]
        oos_px = research.iloc[sp.oos_start : sp.oos_end]
        scored: list[tuple[float, SignalParams]] = []
        for params in grid:
            s = params.apply(base.model_copy(deep=True))
            is_m = replay(is_px, s, start_equity=start_equity, periods_per_year=min(365, bars_per_year))
            scored.append((is_m.score, params))
        chosen = plateau_pick(scored)
        last_params = chosen
        s = chosen.apply(base.model_copy(deep=True))
        oos_m = replay(oos_px, s, start_equity=start_equity, periods_per_year=min(365, bars_per_year))
        folds.append(
            FoldResult(
                fold=sp.fold,
                params=asdict(chosen),
                is_score=max(v for v, _ in scored),
                oos={
                    "score": oos_m.score,
                    "sharpe": oos_m.sharpe,
                    "max_dd": oos_m.max_dd,
                    "n_trades": oos_m.n_trades,
                    "win_rate": oos_m.win_rate,
                    "total_return": oos_m.total_return,
                    "expectancy": oos_m.expectancy,
                },
            )
        )

    oos_sharpes = [f.oos["sharpe"] for f in folds]
    oos_scores = [f.oos["score"] for f in folds]
    oos_rets = [f.oos["total_return"] for f in folds]
    deploy = last_params or grid[0]
    hold_m = replay(
        holdout,
        deploy.apply(base.model_copy(deep=True)),
        start_equity=start_equity,
        periods_per_year=min(365, bars_per_year),
    )

    return {
        "n_bars": n,
        "n_folds": len(folds),
        "is_bars": is_bars,
        "oos_bars": oos_bars,
        "embargo_bars": embargo_bars,
        "fee_bps": base.paper_taker_fee_bps,
        "slippage_bps": base.paper_slippage_bps,
        "folds": [asdict(f) for f in folds],
        "oos_median_sharpe": float(np.median(oos_sharpes)) if oos_sharpes else 0.0,
        "oos_p5_sharpe": float(np.percentile(oos_sharpes, 5)) if oos_sharpes else 0.0,
        "oos_median_score": float(np.median(oos_scores)) if oos_scores else 0.0,
        "oos_win_fold_frac": float(np.mean([1.0 if r > 0 else 0.0 for r in oos_rets])) if oos_rets else 0.0,
        "deploy_params": asdict(deploy),
        "holdout": {
            "bars": len(holdout),
            "score": hold_m.score,
            "sharpe": hold_m.sharpe,
            "max_dd": hold_m.max_dd,
            "n_trades": hold_m.n_trades,
            "win_rate": hold_m.win_rate,
            "total_return": hold_m.total_return,
            "final_equity": hold_m.equity[-1] if hold_m.equity else start_equity,
        },
        "verdict": _verdict(oos_sharpes, oos_rets, hold_m),
    }


def _verdict(sharpes: list[float], rets: list[float], hold: ReplayMetrics) -> str:
    if not sharpes:
        return "INSUFFICIENT_FOLDS"
    med = float(np.median(sharpes))
    p5 = float(np.percentile(sharpes, 5))
    win_frac = float(np.mean([1.0 if r > 0 else 0.0 for r in rets]))
    if hold.n_trades < 3:
        return "HOLD_OUT_TOO_THIN"
    if med > 0.4 and p5 > -0.3 and win_frac >= 0.5 and hold.score > -0.5:
        return "PAPER_DEPLOY"
    if med > 0:
        return "WATCH_ONLY"
    return "DO_NOT_DEPLOY"


def write_deploy(report: dict[str, Any], path: Path = Path("logs/wfo_deploy.json")) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "verdict": report["verdict"],
        "params": report["deploy_params"],
        "oos_median_sharpe": report["oos_median_sharpe"],
        "holdout": report["holdout"],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path
