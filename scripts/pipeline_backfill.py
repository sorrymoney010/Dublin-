#!/usr/bin/env python3
"""Backfill / gap-fill the tick store from Kraken public REST Trades (no keys).

    .venv/bin/python scripts/pipeline_backfill.py --days 120            # history
    .venv/bin/python scripts/pipeline_backfill.py --fill-gaps           # id holes only
    .venv/bin/python scripts/pipeline_backfill.py --symbols SOL/USD --days 7

Resumable: if the store already has ticks it continues after the newest one.
~1 request/s, 1000 trades/request: 120 days of BTC is ~8.5k requests (~2.5 h).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.pipeline import SYMBOLS, canonical  # noqa: E402
from dublin_bot.pipeline.backfill import backfill_range  # noqa: E402
from dublin_bot.pipeline.kraken_rest import KrakenPublic  # noqa: E402
from dublin_bot.pipeline.tickstore import TickStore, find_gaps  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    ap.add_argument("--days", type=float, default=2.0)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--fill-gaps", action="store_true", help="only fill trade-id holes")
    ap.add_argument("--pause", type=float, default=1.0, help="min seconds between requests")
    ap.add_argument("--max-gap-pages", type=int, default=400)
    a = ap.parse_args()

    store = TickStore(a.data_dir)
    client = KrakenPublic(min_interval=a.pause, log=log)
    for raw in a.symbols:
        sym = canonical(raw)
        if sym is None:
            log(f"skip {raw}: not in pipeline universe {SYMBOLS}")
            continue
        if a.fill_gaps:
            ticks = store.read(sym)
            gaps = find_gaps(ticks)
            log(f"{sym}: {len(gaps)} id gap(s) in {len(ticks)} stored ticks")
            for lo, hi, ts_before, _ts_after in gaps:
                res = backfill_range(store, client, sym, ts_before, after_id=lo - 1,
                                     until_id=hi + 1, max_pages=a.max_gap_pages, progress=log)
                log(f"{sym}: gap {lo}-{hi} -> {res}")
        else:
            since = time.time() - a.days * 86400
            last = store.last_tick(sym)
            after = None
            if last is not None and last[1] > since:
                after, since = last[0], last[1]
                log(f"{sym}: resuming after trade_id {after}")
            res = backfill_range(store, client, sym, since, after_id=after, progress=log)
            log(f"{sym}: {res}")
        done = store.compact(sym)
        if done:
            log(f"{sym}: compacted {len(done)} day file(s)")


if __name__ == "__main__":
    main()
