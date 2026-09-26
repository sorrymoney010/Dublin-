# PUMP bot — technicians + stragglers

For Grok bot `70815a54-2ca8-4b85-9474-d90be603bcbd`
Saved 2026-09-26. Research / paper only.

These are the pieces that were built in the edge thread and not all wired into live PUMP.

---

## Already in Dublin- (use these)

### 1. Sizing technician — `src/dublin_bot/sizing.py`
Vol target is the driver. Kelly is a multiplier only.

- Blend 10d / 30d realized vol, weights 0.4 / 0.6, floor 5% ann.
- `vol_scale = target_vol / vol`, cap `max_leverage`.
- Fractional Kelly: `(b p - q) / b * kelly_fraction * confidence`, hard cap 0.25.
- Then stop-distance gate: shares ≤ `equity * risk_per_trade / stop_distance`.
- Crypto annualize 365.

**PUMP freeze:** `target_vol=0.12`, `kelly_fraction=0.25`, `risk_per_trade` from Settings (≤ 2%). Do not put these in the WFO grid.

WFO currently hardcodes `EdgeEstimate(win_prob=0.52, avg_win=1.2, avg_loss=1.0, confidence=0.6)`. Do not treat that as learned PUMP edge.

### 2. WFO technician — `src/dublin_bot/walk_forward.py`
- Rolling IS / embargo / OOS + untouched holdout (15%).
- Score = net Sharpe − 1.5 maxDD − 0.15 turnover after 80 bp taker + 10 bp slip.
- Plateau = top-3 centroid, not IS max.
- Deploy params = **mode across folds**, not last fold.
- Thin OOS (<5 trades) → fold flagged; >50% thin → `FOLDS_TOO_THIN`.
- Verdicts: PAPER_DEPLOY | WATCH_ONLY | DO_NOT_DEPLOY | HOLD_OUT_TOO_THIN | FOLDS_TOO_THIN | INSUFFICIENT_FOLDS

### 3. Data technician — `src/dublin_bot/ohlc.py`
Page Kraken public OHLC (single call ~720 bars). Cache `logs/ohlc_PUMP-USD_60m.csv`.

### 4. CLI
```bash
PYTHONPATH=src:. python scripts/walk_forward.py --symbol PUMP/USD --days 365 --interval 60 --is-days 45 --oos-days 14
```
Writes `logs/wfo_deploy.json` and `logs/wfo_deploy_PUMP-USD.json`.

---

## Stragglers (not in the live PUMP path yet)

### S1. Engine does not load deploy JSON
Paper/live PUMP still uses `.env` + default momentum. Someone must paste params or wire a loader that only fires on `PAPER_DEPLOY` + holdout trades ≥ 20.

### S2. Replay ≠ live engine
WFO skips `RiskManager`, daily-loss, drawdown halt, cooldown, rotation, sentiment, universe allowlist, `FillModel` spread. A green WFO can still lose in the dashboard loop.

### S3. Residual z-score engine (sketch only)
PCA residual → OU half-life filter → z-score fade. Never landed in `src/`. Do not ship to live PUMP without its own WFO.

### S4. Regime + XGBoost pipeline (sketch only)
Vol + trend gate, then small TS-split XGB for P(edge). Never landed in `src/`. Same rule.

### S5. Sharpe scale
WFO Sharpe is trade-return × √365. Rank inside one run only. Do not compare to CTA 252-day Sharpe.

### S6. Last-audit PUMP evidence (2026-08-21)
Momentum PUMP: IS tiny +, **OOS expectancy negative**.
Mean-reversion PUMP: ~7 trades / 200d — not a sample.
`stop_loss_pct=4%` can gap through on PUMP.

### S7. Regime detector still dead
`learner.last_regime` stays `unknown`. Per-regime expectancy is flat.

### S8. Kelly edge not learned
Constant 0.52/1.2 in replay. Cosmetic until wired to closed-trade stats.

---

## What this bot should do first
1. Run the CLI on PUMP.
2. Reply with: folds, oos_trades, thin_fold_frac, holdout n/Sharpe/DD, verdict, deploy_params.
3. Stop. Do not live-size.
4. Only if PAPER_DEPLOY and holdout trades ≥ 20: propose paper `.env` diffs for strategy/RSI/ATR/lookback. Leave risk knobs alone.
