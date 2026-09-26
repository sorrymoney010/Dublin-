# PUMP bot handoff

Grok bot / conversation id: `70815a54-2ca8-4b85-9474-d90be603bcbd`
Source session: Dublin- WFO work on `main`.
Date: 2026-09-26

This file is the packet that session should start from. I cannot push into another Grok chat; the id is recorded here and in the GitHub issue.

## Scope
Coin: **PUMP/USD** on Kraken via Dublin-.
Not a new repo. Same engine, same risk locks.

## Code to use
- `src/dublin_bot/walk_forward.py` — rolling IS/OOS, plateau + fold-mode consensus, holdout, verdict
- `src/dublin_bot/ohlc.py` — paged Kraken OHLC + `logs/ohlc_PUMP-USD_60m.csv`
- `src/dublin_bot/sizing.py` — vol target + 1/4 Kelly (frozen in WFO)
- `scripts/walk_forward.py` — default `--symbol PUMP/USD`

## Command
```bash
PYTHONPATH=src:. python scripts/walk_forward.py \
  --symbol PUMP/USD --days 365 --interval 60 \
  --is-days 45 --oos-days 14
```

Outputs:
- `logs/wfo_report.json`
- `logs/wfo_deploy.json`
- `logs/wfo_deploy_PUMP-USD.json` (symbol copy)

## Hard rules for that bot
1. Do not place live orders from this packet.
2. Do not flip `ALLOW_LIVE_TRADING`.
3. Apply params to paper `.env` only if verdict == `PAPER_DEPLOY` and holdout `n_trades` >= 20.
4. If verdict is `DO_NOT_DEPLOY`, `FOLDS_TOO_THIN`, or `HOLD_OUT_TOO_THIN`, leave PUMP on current paper settings.
5. Do not optimize `target_vol`, `kelly_fraction`, or `risk_per_trade` in the grid.
6. 4% fixed stop is tight on PUMP — treat gap risk as open.

## Known gaps this bot should close next
- Replay does not use RiskManager / FillModel / rotation / sentiment.
- Engine does not auto-load deploy JSON unless you wire it.
- Prior live-pipeline backtest: momentum PUMP OOS expectancy was negative; MR sample was tiny.

## First reply expected from that bot
Paste: fold count, oos_trades list, thin_fold_frac, holdout trades/Sharpe/DD, verdict, deploy_params.
Then stop.
