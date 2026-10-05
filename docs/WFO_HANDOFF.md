# Dublin- walk-forward handoff (phase 2)

On `main`. Research-only. No orders.

## Phase 2 changes
- CLI no longer imports `scripts.backtest`
- Kraken OHLC is **paged** and cached at `logs/ohlc_{SYMBOL}_{interval}m.csv`
- Deploy params = **mode across folds**, not the last fold
- Each fold prints trade count; `thin_fold_frac` and verdict `FOLDS_TOO_THIN` if most OOS windows have < 5 trades

## Run
```bash
PYTHONPATH=src:. python scripts/walk_forward.py \
  --symbol XRP/USD --days 365 --interval 60 \
  --is-days 45 --oos-days 14
```

`--refresh` ignores the CSV cache.
Repeat for PUMP/USD and BTC/USD.

## Verdicts
PAPER_DEPLOY | WATCH_ONLY | DO_NOT_DEPLOY | HOLD_OUT_TOO_THIN | FOLDS_TOO_THIN | INSUFFICIENT_FOLDS

Do not raise live risk because a fold looked good.
