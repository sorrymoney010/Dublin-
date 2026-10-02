#!/bin/bash
# Public tick collector (BTC/USD, ETH/USD, SOL/USD) for the market-data pipeline.
# Read-only: Kraken PUBLIC WebSocket v2 + REST only. Needs no API keys, never
# reads .env, never places orders. Run by launchd as com.mayo.kraken.ticks.
set -euo pipefail
cd /Users/musicmancheef/mayo-bot
mkdir -p logs data
exec /Users/musicmancheef/mayo-bot/.venv/bin/python /Users/musicmancheef/mayo-bot/scripts/tick_collector.py \
  --data-dir /Users/musicmancheef/mayo-bot/data --backfill-hours 6
