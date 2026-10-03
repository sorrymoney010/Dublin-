# Learner policy, paper telemetry, shadow trades, live-promotion check

PAPER ONLY. Nothing here changes a safety lock. Every file listed below is written
under `logs/`. These files are never committed and contain no secrets.

## Learner policy (`src/dublin_bot/learner.py`)

| Rule | Before | Now |
|---|---|---|
| Closed trades needed before a bench is possible (per strategy + coin) | 8 | **30**. Config `LEARNER_MIN_SAMPLE` defaults to 30, and any lower value, such as an old env file's 8, is raised to 30. |
| Bench condition | mean net bps < 0 | **the one-sided 90% upper confidence bound of mean net bps < 0** (Student-t) |
| Rolling window | 20 (so a sample of 30 could never be reached) | 50 (always at least min_sample) |
| Size multipliers for allowed entries | 0.25 to 1.0 | **clamped to [0.5, 1.0]**. Probation size is 0.5. |
| Backtest prior weight | ≤ 5 pseudo-trades | **≤ 2** |
| Existing benches placed on fewer than 30 trades | — | dropped automatically when `learner.json` loads (one-time migration, no manual step) |

### Replay (`scripts/learner_replay.py`, output in `data/learner_replay.txt`)

The replay feeds pipeline-data backtest trades chronologically through the gate. A
blocked trade is not learned from, which matches the paper loop, where it only becomes
a shadow trade.

**Actual trades** (the strategies as shipped, plus regime without D1):

| Strategy | Old policy | Audit policy |
|---|---|---|
| D1-on strategies: regime@60m, meanrev_mk@240m, trend-hold@240m (5–12 trades per coin) | benches nothing | benches nothing |
| regime@60m **without D1** | benched 15 trades (BTC 6, ETH 9). Those 15 trades summed **+2,605 bps**, so the old bench cost money. | benched 0 |

**Monte Carlo** (400 sequences of 60 trades, bootstrapped from 53 regime@60m trades):

| True mean net bps | Old: P(any bench) | Old: avg trades blocked | Audit: P(any bench) | Audit: avg trades blocked |
|---:|---:|---:|---:|---:|
| +93 | 0.93 | 21.8 | 0.12 | 1.7 |
| 0 | 0.99 | 30.4 | 0.40 | 6.4 |
| −50 | 1.00 | 33.7 | 0.58 | 9.7 |

The trade-off is explicit. The audit policy almost never benches a profitable but noisy
strategy. It catches a genuinely losing one more slowly: in 58% of runs within 60 trades,
instead of always. Until it does, the strategy keeps trading at 0.5× size, because a
negative blended expectancy halves the size.

## Telemetry files

| File | Written by | One row per |
|---|---|---|
| `decision_snapshots.jsonl` | engine (via the loop) and every sleeve | new decision (strategy, symbol, closed bar, action). Each row has: bar open/close, data age (s), indicator snapshot (close, EMA20/50/200, ADX, ATR, ATR rank, RSI, D1), D1 state, bid/ask/spread (bps), strategy id, learner state (n, mean, 90% upper bound, bench), risk verdict, raw vs final action, git hash. (`decisions.jsonl` remains the engine's older DecisionRecord journal.) |
| `closed_trades.jsonl` | `LearningAgent.record_trade` at each paper close (`source: "fill"`) and the scorecard's learner sync (older rows) | closed trade. Each row has: entry/exit signal price vs fill price, slippage (bps), fees (USD), maker/taker on each side, exit reason, MAE/MFE (bps, from bars during the hold), hold time, qty, cost basis, net bps, P&L, git hash |
| `shadow_signals.jsonl` | loop (engine shadow candidates, plus each sleeve's `blocked` list) | blocked entry signal (`type: signal`) and later its hypothetical result (`type: outcome`) |
| `equity.jsonl` | loop, every 5 min | paper book marked to market with public ticker prices: cash, positions, owner sleeve, exposure %, equity |
| `promotion_report.json` | scorecard / `scripts/promotion_check.py` | report snapshot |

### Shadow trades (`src/dublin_bot/shadow.py`)

A BUY that the strategy's own rules produced but a gate stopped is recorded once per
strategy, symbol and bar. Gates include: learner bench, max positions, exposure, sleeve
ownership, market quality, breakers, cooldown, and the D1 filter (the raw rules fired
but D1 was off).

Every 15 minutes, open shadow signals are scored by replaying the strategy's backtest
rules (`backtest_core.simulate`, with D1 off) from the signal bar on closed **public**
Kraken OHLC. The outcome is one of:

| Outcome | Meaning |
|---|---|
| `scored` | entry/exit, exit reason, hold time and net bps after fees |
| `no_fill` | meanrev limit would not have filled; 0 bps |
| `not_reproduced` | the rules did not fire again on public OHLC |
| `expired` | older than 45 days |

The scorecard reports, for each strategy and gate: signals, scored, open, average and
sum of net bps. This shows whether each gate saves or costs money.

## Live-promotion bar (`src/dublin_bot/promotion.py`, `scripts/promotion_check.py`)

**Report only.** The module never reads settings or env files, never touches
`PAPER_TRADING` / `DRY_RUN` / `ALLOW_LIVE_TRADING`, and no trading code imports it.
Going live remains a manual human change.

A strategy passes when all of these hold on its closed **paper** trades, after fees:
1. At least 30 closed trades.
2. Mean net bps > 0 **and** the one-sided 90% lower bound > 0.
3. Mean net bps is still > 0 without the best 2 trades.
4. At least as good as cash: total realized P&L ≥ $0. The report also shows the paper
   book's equity against its seed.

Run it with `python scripts/promotion_check.py`. It prints the verdict and writes
`logs/promotion_report.json`. The daily scorecard run refreshes the same report and
embeds a summary in `logs/scorecard.json`.
