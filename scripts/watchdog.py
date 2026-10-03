#!/usr/bin/env python3
"""One watchdog pass (run every 5 min by launchd com.mayo.kraken.watchdog).
See dublin_bot.watchdog. No keys, never reads .env, never changes the locks."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.scorecard import write_scorecard  # noqa: E402
from dublin_bot.watchdog import Watchdog  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    a = ap.parse_args()
    res = Watchdog(repo=ROOT, data_dir=Path(a.data_dir), scorecard=write_scorecard).check()
    raise SystemExit(0)
