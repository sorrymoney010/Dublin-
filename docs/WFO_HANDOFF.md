# Dublin- walk-forward handoff

Date: 2026-09-26
Branch: `feat/walk-forward-handoff`
Goal: maximize the *stitched OOS equity curve*, then freeze params for paper.

## What this is
A research-only optimizer. It does **not** place orders.

It walk-forward-fits the **signal knobs** of the live `momentum` / `mean_reversion` strategies, while **freezing** the risk stack you already have:

- `PositionSizer` (vol target + 1/4 Kelly + risk-percent)
- `risk_per_trade`, `max_position_fraction`, daily-loss / drawdown breakers
- Kraken starter fees: 80 bp taker + 10 bp slippage (from Settings)

In-sample winner is the **plateau centroid of the top-3 scores**, not the single max. That is the curve-quality rule.

Score per window:

`Sharpe_net - 1.5 * maxDD - 0.15 * turnover`

## Files
| Path | Role |
|---|---|
| `src/dublin_bot/walk_forward.py` | splits, replay, plateau pick, WFO, deploy JSON |
| `scripts/walk_forward.py` | CLI against real Kraken OHLC |
| `tests/test_walk_forward.py` | synthetic tests, no keys |
| `logs/wfo_deploy.json` | written at runtime — paste these params into `.env` / Settings |

## Run
```bash
PYTHONPATH=src:. python scripts/walk_forward.py \
  --symbol XRP/USD --days 365 --interval 60 \
  --is-days 45 --oos-days 14
```

Repeat for `PUMP/USD` and `BTC/USD`. Coin edge is **not** portable.

## Verdicts
- `PAPER_DEPLOY` — median OOS Sharpe > 0.4, p5 not awful, >=50% folds green, holdout not dead
- `WATCH_ONLY` — median OOS > 0 but fragile. Paper only, tiny size
- `DO_NOT_DEPLOY` — stitched curve did not survive costs
- `HOLD_OUT_TOO_THIN` — add days

Do not raise TARGET_VOL / KELLY_FRACTION / RISK_PER_TRADE because a fold looked good.
Paper for two OOS windows (~28d) before live size.
