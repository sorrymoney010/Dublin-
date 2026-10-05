#!/bin/bash
# Keep the dashboard running from this checkout (repo-relative paths).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p logs
while true; do
  PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m dublin_bot.cli dashboard >> logs/dashboard.log 2>&1
  echo "[$(date)] dashboard exited code $? -- restarting in 2s" >> logs/dashboard.log
  sleep 2
done
