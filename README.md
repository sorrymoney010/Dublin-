# Dublin Trading OS

A private, paper-first crypto trading system built around strict risk controls, transparent decisions, and testable strategies.

## Current scope

- Native Alpaca and Kraken Spot gateways
- BTC/USD first, with reusable multi-asset architecture
- $25 strategy budget by default
- No leverage or shorting
- Trend, momentum, breakout, volume, and ATR risk logic
- Persistent daily-loss, drawdown, cooldown, and order-count circuit breakers
- Cost-aware backtesting and walk-forward validation components
- JSONL decision journal, reconciliation, protective-stop checks, and CI tests

## Kraken safety model

Kraken Spot has no general paper-trading endpoint. Dublin therefore uses its own local simulation layer while still reading real Kraken market data.

Keep these defaults:

```env
EXCHANGE=kraken
PAPER_TRADING=true
DRY_RUN=true
ALLOW_LIVE_TRADING=false
```

For the first API key, enable only the permissions needed to query funds and account/trade information. Do **not** enable withdrawals, deposits, Earn, or address management. The `doctor` command inspects the key information and returns a safety error when withdrawal permission is detected.

Live order submission remains locked unless all of these are deliberately configured locally:

```env
PAPER_TRADING=false
DRY_RUN=false
ALLOW_LIVE_TRADING=true
LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK
```

Do not make those changes until cost-aware backtests and a meaningful forward dry-run period pass.

## Setup on macOS

```bash
git clone https://github.com/sorrymoney010/Dublin-.git
cd Dublin-
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Open `.env` locally and add your Kraken API key and secret. Never paste credentials into chat and never commit `.env`.

Validate public data, credentials, permissions, and safety locks:

```bash
dublin-bot doctor
```

Run one real-market-data cycle with simulated execution:

```bash
dublin-bot run-once
```

Run lint and tests:

```bash
ruff check .
pytest -q
```

## Switching back to Alpaca

Set `EXCHANGE=alpaca` and configure the Alpaca credential fields. Strategy and risk code remain unchanged because execution is broker-neutral.

## Important

This software does not guarantee profit. Cheap coin price alone is not an edge; liquidity, spread, fees, slippage, volatility, and execution quality matter more. Keep real-money trading disabled until the readiness report and forward-testing requirements pass.
