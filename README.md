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

The default configuration is paper-only and dry-run:

```env
PAPER_TRADING=true
DRY_RUN=true
ALLOW_LIVE_TRADING=false
```

Do not enable live trading until backtests and a meaningful paper-trading period are complete.

## Setup on macOS

```bash
git clone https://github.com/sorrymoney010/Dublin-.git
cd Dublin-
git checkout main
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
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

Run lint and tests:

```bash
ruff check .
pytest -q
```

## Important

This software does not guarantee profit. Paper fills are simulated and can differ from live fills, especially for low-liquidity assets. Cheap coin price alone is not an edge; liquidity, spread, volatility, and execution quality matter more.
