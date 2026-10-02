"""REST ``Trades`` backfill / gap-fill into the tick store."""
from __future__ import annotations

import time
from typing import Callable

from . import PAIRS, canonical
from .kraken_rest import KrakenPublic
from .tickstore import TickStore


def backfill_range(store: TickStore, client: KrakenPublic, symbol: str, since_ts: float,
                   *, until_id: int | None = None, until_ts: float | None = None,
                   after_id: int | None = None, max_pages: int | None = None,
                   progress: Callable[[str], None] | None = None) -> dict:
    """Page public trades forward from ``since_ts`` and append them.

    Stops when the page reaches ``until_id`` (exclusive, e.g. the first live
    WebSocket trade), ``until_ts``, the present, or ``max_pages``. Only ids
    strictly greater than ``after_id`` are written (dedup vs the store).
    """
    sym = canonical(symbol)
    pair = PAIRS[sym][0]
    cursor: str = str(int(since_ts))
    pages = written = 0
    last_id = after_id
    stop_reason = "caught_up"
    while True:
        ticks, last_cursor = client.trades(pair, cursor)
        pages += 1
        if not ticks:
            break
        keep = [t for t in ticks
                if (last_id is None or t.trade_id > last_id)
                and (until_id is None or t.trade_id < until_id)
                and (until_ts is None or t.ts < until_ts)]
        if keep:
            written += store.append(sym, keep)
            last_id = max(t.trade_id for t in keep)
        newest = ticks[-1]
        if until_id is not None and newest.trade_id >= until_id - 1:
            stop_reason = "reached_live"
            break
        if until_ts is not None and newest.ts >= until_ts:
            stop_reason = "reached_until_ts"
            break
        if len(ticks) < 1000 and newest.ts > time.time() - 5:
            break
        if max_pages is not None and pages >= max_pages:
            stop_reason = "max_pages"
            break
        # Next page from Kraken's own ``last`` cursor (nanoseconds); dedup by
        # id covers any overlap. A cursor that does not advance means done.
        if not last_cursor or last_cursor == cursor:
            break
        cursor = last_cursor
        if progress and pages % 50 == 0:
            progress(f"{sym} backfill page {pages} at {time.strftime('%Y-%m-%d %H:%M', time.gmtime(newest.ts))}Z "
                     f"written={written}")
    return {"symbol": sym, "pages": pages, "written": written, "last_id": last_id,
            "stop": stop_reason}
