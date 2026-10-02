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


def verify_hole(client: KrakenPublic, symbol: str, lo: int, hi: int, ts_before: float,
                *, max_pages: int = 3) -> bool:
    """True if Kraken's own REST history jumps from ``lo - 1`` straight to ``hi + 1``.

    That means ids ``lo..hi`` were never published (an exchange-side hole, not
    a collection gap), so it is safe to treat the stream as continuous there.
    """
    pair = PAIRS[canonical(symbol)][0]
    cursor: str = str(int(ts_before) - 1)
    for _ in range(max_pages):
        ticks, last = client.trades(pair, cursor)
        ids = [t.trade_id for t in ticks]
        for a, b in zip(ids, ids[1:], strict=False):
            if a == lo - 1:
                return b == hi + 1
            if a >= lo:
                return False
        if not ticks or ids[-1] >= lo or not last or last == cursor:
            return False
        cursor = last
    return False


def fill_gaps(store: TickStore, client: KrakenPublic, symbol: str, *, max_pages: int = 400,
              progress: Callable[[str], None] | None = None) -> dict:
    """Fill every id hole in the store from REST; record holes Kraken itself has."""
    from .tickstore import find_gaps

    sym = canonical(symbol)
    known = store.verified_holes(sym)
    gaps = [g for g in find_gaps(store.read(sym)) if (g[0], g[1]) not in known]
    filled = verified = still_open = 0
    for lo, hi, ts_before, _ts_after in gaps:
        backfill_range(store, client, sym, ts_before - 1, after_id=lo - 1, until_id=hi + 1,
                       max_pages=max_pages)
        lo_day = store.read(sym, ts_before - 1, _ts_after + 1)
        rem = [g for g in find_gaps(lo_day) if g[0] >= lo and g[1] <= hi]
        if not rem:
            filled += 1
            continue
        for rlo, rhi, rts, _ in rem:
            if verify_hole(client, sym, rlo, rhi, rts):
                store.add_verified_hole(sym, rlo, rhi)
                verified += 1
            else:
                still_open += 1
        if progress:
            progress(f"{sym} gap {lo}-{hi}: {len(rem)} sub-hole(s) remain after REST fill")
    return {"symbol": sym, "gaps": len(gaps), "filled": filled, "exchange_holes": verified,
            "open": still_open}
