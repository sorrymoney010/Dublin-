#!/bin/bash
# Self-healing watchdog for com.mayo.kraken.paper / com.mayo.kraken.ticks.
# Run every 5 min by launchd (com.mayo.kraken.watchdog). Needs no API keys,
# never reads .env, never edits config or the safety locks. docs/WATCHDOG.md.
set -euo pipefail
cd /Users/musicmancheef/mayo-bot
mkdir -p logs
exec /Users/musicmancheef/mayo-bot/.venv/bin/python /Users/musicmancheef/mayo-bot/scripts/watchdog.py \
  --data-dir /Users/musicmancheef/mayo-bot/data
