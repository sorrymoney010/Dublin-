#!/usr/bin/env python3
"""Write logs/scorecard.json (per-sleeve closed-trade scorecard). Read-only on
the trading side: no keys, no .env, no orders. The watchdog runs it daily."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.scorecard import write_scorecard  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", default=str(ROOT / "logs"))
    a = ap.parse_args()
    card = write_scorecard(Path(a.logs_dir))
    print(json.dumps({"all": card["all"], "sleeves": {k: {x: v[x] for x in
          ("trades", "win_pct", "avg_net_bps", "net_pnl_usd")} for k, v in card["sleeves"].items()}}))
