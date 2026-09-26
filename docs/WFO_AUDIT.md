# WFO merge audit — 2026-09-26

Merged: PR #5 squash → `main` @ `60c653d648fbc99e329dca05314d857ea17d3244`
Scope: `walk_forward.py`, CLI, tests, `docs/WFO_HANDOFF.md`
No exchange writes. No live orders.

## Verdict
Safe to keep on `main` as **research-only**. Do **not** treat a `PAPER_DEPLOY` print as permission to raise live size. Prior 2026-08-21 audit still stands: live expectancy evidence is thin.

## What landed correctly
- WFO does not call the gateway for orders.
- Risk knobs (`target_vol`, `kelly_fraction`, `risk_per_trade`) are frozen.
- Fees default to Settings paper taker (80 bp) + slippage (10 bp).
- Embargoed rolling splits + untouched holdout.
- Plateau pick beats a single IS max.
- Tests cover split hygiene and plateau clustering.

## P1 — fix before trusting a verdict

### A1. CLI import is fragile
`scripts/walk_forward.py` does `from scripts.backtest import fetch_history`.
`scripts/` is not a package. Run as `python scripts/walk_forward.py` will often fail.
**Fix:** copy `fetch_history` into the CLI (same as `scripts/backtest.py`) or add `scripts/__init__.py` and run as a module.

### A2. Kraken OHLC cap
Public OHLC returns a limited candle window (~720 rows). `--days 365 --interval 60` will **not** give a year. Folds will be fewer than the handoff implies. Use 15m/60m in chunks or a stored cache.

### A3. Replay ≠ live engine
WFO does not use `RiskManager`, cooldown, daily-loss, drawdown halt, universe allowlist, rotation, sentiment, or `FillModel` spread. A green WFO can still be a red live cycle.

### A4. Sharpe is not comparable
Trade-return Sharpe is annualized with `min(periods_per_year, 365)`. That is not bar Sharpe and not 365 independent trades/year. Use it only rank-wise inside one run.

### A5. Deploy params = last fold only
`deploy_params` is the final window's plateau, not a consensus across folds. If the last fold is a trend spike, paper will inherit it. Prefer modal params across folds.

### A6. No engine hook
`logs/wfo_deploy.json` is not read by `config.py` / `engine.py`. Merge does not change live behavior until someone pastes values into `.env`.

### A7. Hardcoded Kelly edge
`EdgeEstimate(0.52, 1.2, 1.0, 0.6)` is constant. Sizing conviction does not learn. Harmless (conservative) but the Kelly layer is mostly cosmetic in WFO.

### A8. Replay cost
`evaluate(window[:i+1])` each bar is O(n²). Year of 15m bars will be slow. Pre-enrich once per param set.

## P2 — still true from the Aug 21 audit
- Momentum PUMP OOS expectancy was negative in the live-pipeline backtest.
- Mean-reversion sample sizes were 6–7 trades / 200d — not evidence.
- BTC/USD mean-reversion was negative and still allowlisted.
- `stop_loss_pct=4%` can be tight on PUMP.
- Regime detector still missing (`learner.last_regime` stays unknown).

## Go / no-go
| Action | Status |
|---|---|
| Merge to main | Done |
| Run WFO CLI | Fix A1 + A2 first |
| Paper with deploy JSON | Only if holdout trades ≥ 20 and verdict ≠ DO_NOT_DEPLOY |
| Live size up | No |

## Recommended next patch (small)
1. Inline `fetch_history` in the CLI.
2. Cache OHLC to `logs/ohlc_{symbol}_{interval}.csv`.
3. Deploy = mode of fold params, not last fold.
4. Print fold trade counts so thin OOS cannot hide.
