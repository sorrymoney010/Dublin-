from __future__ import annotations

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.walk_forward import (
    DEFAULT_GRID,
    SignalParams,
    plateau_pick,
    replay,
    rolling_splits,
    run_walk_forward,
    score_metrics,
)


def _bars(n: int = 4000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0001, 0.012, n)
    for i in range(80, n, 180):
        rets[i : i + 12] = -abs(rng.normal(0.01, 0.004, 12))
        rets[i + 12 : i + 24] = abs(rng.normal(0.008, 0.003, 12))
    close = 100 * np.exp(np.cumsum(rets))
    high = close * (1 + rng.uniform(0.001, 0.006, n))
    low = close * (1 - rng.uniform(0.001, 0.006, n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = rng.uniform(1e5, 3e5, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_splits_do_not_overlap_after_embargo():
    splits = rolling_splits(1000, is_bars=200, oos_bars=50, step_bars=50, embargo_bars=5)
    assert splits
    for sp in splits:
        assert sp.is_end + 5 <= sp.oos_start
        assert sp.oos_end <= 1000


def test_plateau_prefers_cluster_not_outlier():
    grid = [
        (1.0, SignalParams("mean_reversion", 35, 75, 32, 52, 1.5, 20)),
        (0.95, SignalParams("mean_reversion", 35, 75, 35, 55, 1.5, 20)),
        (0.94, SignalParams("mean_reversion", 35, 75, 38, 55, 1.5, 20)),
        (3.5, SignalParams("momentum", 10, 90, 10, 90, 4.0, 80)),
    ]
    picked = plateau_pick(grid, top_k=3)
    assert picked.strategy == "mean_reversion"
    assert picked.atr_stop_multiplier < 3.0


def test_replay_and_wfo_run_on_synthetic():
    bars = _bars()
    settings = Settings(_env_file=None)
    settings.paper_trading = True
    settings.dry_run = True
    settings.allow_live_trading = False
    m = replay(bars.iloc[:800], settings, start_equity=1000.0)
    assert m.equity[0] == 1000.0
    assert m.max_dd >= 0.0
    report = run_walk_forward(
        bars,
        settings,
        grid=DEFAULT_GRID[:4],
        is_bars=24 * 30,
        oos_bars=24 * 10,
        step_bars=24 * 10,
        embargo_bars=4,
        holdout_frac=0.12,
    )
    assert report["n_folds"] >= 2
    assert report["verdict"] in {"PAPER_DEPLOY", "WATCH_ONLY", "DO_NOT_DEPLOY", "HOLD_OUT_TOO_THIN"}
    assert "deploy_params" in report
    _ = score_metrics(m)
