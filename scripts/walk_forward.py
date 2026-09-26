"""CLI: walk-forward optimize Dublin- strategies on Kraken history.

    PYTHONPATH=src:. python scripts/walk_forward.py --symbol XRP/USD --days 365 --interval 60
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.walk_forward import run_walk_forward, write_deploy

from scripts.backtest import fetch_history  # type: ignore


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward optimizer (stitched OOS curve)")
    ap.add_argument("--symbol", default="XRP/USD")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--interval", type=int, default=60, help="minutes")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--is-days", type=int, default=45)
    ap.add_argument("--oos-days", type=int, default=14)
    ap.add_argument("--out", default="logs/wfo_report.json")
    args = ap.parse_args()

    settings = Settings()
    settings.symbol = args.symbol
    settings.timeframe_minutes = args.interval
    gw = build_gateway(settings)
    bars = fetch_history(gw, args.symbol, args.days, args.interval)
    if "time" in bars.columns:
        bars = bars.set_index(pd_index(bars["time"]))
    bars = bars[["open", "high", "low", "close", "volume"]].astype(float)

    bars_per_day = max(int(24 * 60 / args.interval), 1)
    report = run_walk_forward(
        bars,
        settings,
        is_bars=args.is_days * bars_per_day,
        oos_bars=args.oos_days * bars_per_day,
        step_bars=args.oos_days * bars_per_day,
        start_equity=args.equity,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    deploy = write_deploy(report)
    h = report["holdout"]
    print(f"symbol          : {args.symbol}")
    print(f"folds           : {report['n_folds']}")
    print(f"OOS median SR   : {report['oos_median_sharpe']:.2f}")
    print(f"OOS p5 SR       : {report['oos_p5_sharpe']:.2f}")
    print(f"OOS win folds   : {report['oos_win_fold_frac']*100:.0f}%")
    print(f"holdout Sharpe  : {h['sharpe']:.2f}  DD {h['max_dd']*100:.1f}%  trades {h['n_trades']}")
    print(f"deploy params   : {report['deploy_params']}")
    print(f"verdict         : {report['verdict']}")
    print(f"wrote           : {out}  {deploy}")
    if report["verdict"] == "DO_NOT_DEPLOY":
        sys.exit(2)


def pd_index(series):
    import pandas as pd

    return pd.to_datetime(series.astype(float), unit="s", utc=True)


if __name__ == "__main__":
    main()
