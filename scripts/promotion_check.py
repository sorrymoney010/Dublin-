#!/usr/bin/env python3
"""Live-promotion bar check — REPORT ONLY (writes logs/promotion_report.json).

Never reads .env, never changes PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING.
Exit code is always 0; the verdict is in the report.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.promotion import render, write_report  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--logs", type=Path, default=ROOT / "logs")
    a = ap.parse_args()
    print(render(write_report(a.logs)))


if __name__ == "__main__":
    main()
