# Dublin Trading OS

A private, paper-first crypto trading system built around strict risk controls, transparent decisions, and testable strategies.

## Current scope

- BTC/USD first, with a reusable multi-asset architecture
- $25 strategy budget by default
- Dollar-notional fractional orders
- Alpaca paper trading only
- No leverage, no shorting, no withdrawal access
- Trend + breakout confirmation
- ATR-based risk sizing
- Daily loss, drawdown, cooldown, and order-count circuit breakers
- JSONL decision journal
- Unit tests and GitHub Actions

## Safety status

Live trading is blocked unless all three settings are deliberately changed:

```env
PAPER_TRADING=false
ALLOW_LIVE_TRADING=true
DRY_RUN=false
```

Even then, the execution layer refuses live mode unless an explicit acknowledgement is present. Do not enable live trading until backtests and a meaningful paper-trading period are complete.

## Setup on macOS

```bash
git clone https://github.com/sorrymoney010/Dublin-.git
cd Dublin-
git checkout build/paper-trading-core
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Add your **paper** Alpaca key and secret to `.env`. Never commit `.env`.

Validate configuration:

```bash
dublin-bot doctor
```

Run one paper/dry cycle:

```bash
dublin-bot run-once
```

Run tests:

```bash
pytest
```

## Important

This software does not guarantee profit. Paper fills are simulated and can differ from live fills, especially for low-liquidity assets. Cheap coin price alone is not an edge; liquidity, spread, volatility, and execution quality matter more.
