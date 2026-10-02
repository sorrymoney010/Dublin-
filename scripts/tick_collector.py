#!/usr/bin/env python3
"""Long-running public tick collector for BTC/USD, ETH/USD, SOL/USD (read-only).

    .venv/bin/python scripts/tick_collector.py                 # data/ticks, data/quotes
    .venv/bin/python scripts/tick_collector.py --data-dir /tmp/x --backfill-hours 6

Runs under launchd as ``com.mayo.kraken.ticks`` (scripts/run_ticks_mac.sh).
Uses only Kraken's PUBLIC WebSocket v2 + REST. Never needs or reads API keys.
"""
from __future__ import annotations

import argparse
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.pipeline import SYMBOLS  # noqa: E402
from dublin_bot.pipeline.collector import TickCollector  # noqa: E402
from dublin_bot.pipeline.kraken_rest import KrakenPublic  # noqa: E402
from dublin_bot.pipeline.tickstore import TickStore  # noqa: E402


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    ap.add_argument("--backfill-hours", type=float, default=6.0,
                    help="REST catch-up horizon on start (older holes stay visible)")
    ap.add_argument("--quote-every", type=float, default=5.0)
    a = ap.parse_args()
    c = TickCollector(TickStore(a.data_dir), symbols=a.symbols,
                      client=KrakenPublic(log=log), backfill_hours=a.backfill_hours,
                      quote_every=a.quote_every, log=log)

    def _stop(*_):
        c.running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log(f"START tick collector symbols={','.join(c.symbols)} data={a.data_dir}")
    c.run_forever()


if __name__ == "__main__":
    main()
