"""Kraken WebSocket v2 tick collector (public ``trade`` + ``ticker``; read-only).

* Subscribes to ``trade`` (with snapshot) and ``ticker`` for BTC/ETH/SOL on
  ``wss://ws.kraken.com/v2``. No keys, no private channels, no orders.
* Dedup: a trade whose ``trade_id`` <= the last stored id is dropped (the
  re-subscribe snapshot always replays the last 50 trades).
* Gap detection + fill: a trade id that jumps (> last + 1) — e.g. after a
  reconnect or the Mac sleeping — triggers a REST ``Trades`` backfill of the
  missing ids before the live trade is written. If the hole is too large to
  fill (``max_gap_pages``) it is left as a visible id hole; bars over it are
  never emitted, so strategies fall back to REST OHLC there.
* Reconnect with exponential backoff; a silent socket (no message for
  ``idle_timeout`` s, Kraken sends a heartbeat every second) is recycled.
* Heartbeat/status per symbol in ``ticks/<SYM>/_status.json``
  (``live_through`` = last time the feed was confirmed live and in sync).
* Top-of-book from ``ticker`` is recorded at most every ``quote_every`` s.
* Closed UTC days are compacted to ``csv.gz`` on start and hourly.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from . import PAIRS, SYMBOLS, canonical
from .backfill import backfill_range
from .kraken_rest import KrakenPublic
from .tickstore import Tick, TickStore

WS_URL = "wss://ws.kraken.com/v2"


def _parse_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def parse_trade(item: dict) -> tuple[str, Tick] | None:
    sym = canonical(item.get("symbol"))
    if sym is None:
        return None
    try:
        return sym, Tick(
            trade_id=int(item["trade_id"]), ts=_parse_ts(item["timestamp"]),
            price=str(item["price"]), qty=str(item["qty"]),
            side="b" if str(item.get("side", "")).lower().startswith("b") else "s",
            ord_type="l" if str(item.get("ord_type", "")).lower().startswith("l") else "m",
        )
    except (KeyError, TypeError, ValueError):
        return None


class TickCollector:
    def __init__(self, store: TickStore, *, symbols: Iterable[str] = SYMBOLS,
                 client: KrakenPublic | None = None, ws_url: str = WS_URL,
                 connect: Callable[..., Any] | None = None,
                 quote_every: float = 5.0, flush_every: float = 2.0,
                 backfill_hours: float = 2.0, max_gap_pages: int = 600,
                 idle_timeout: float = 30.0, now_fn: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print) -> None:
        self.store = store
        self.symbols = [s for s in (canonical(x) for x in symbols) if s]
        self.client = client or KrakenPublic(log=log)
        self.ws_url = ws_url
        self._connect = connect
        self.quote_every = quote_every
        self.flush_every = flush_every
        self.backfill_hours = backfill_hours
        self.max_gap_pages = max_gap_pages
        self.idle_timeout = idle_timeout
        self.now = now_fn
        self.log = log
        self.running = True
        self.connected = False
        self.reconnects = 0
        self.last_msg = 0.0
        self._last_flush = 0.0
        self._last_compact = 0.0
        self.st: dict[str, dict] = {s: {"last_id": None, "last_ts": None, "buf": [], "quotes": [],
                                        "last_quote": 0.0, "gaps_filled": 0, "gaps_open": 0,
                                        "written": 0, "in_sync": False} for s in self.symbols}

    # ── bootstrap / gap fill ───────────────────────────────────
    def bootstrap(self) -> None:
        """Load the newest stored trade per symbol and REST catch up to now."""
        now = self.now()
        for sym in self.symbols:
            self.store.compact(sym, now=now)
            self.store.compact(sym, now=now, kind="quotes")
            last = self.store.last_tick(sym)
            s = self.st[sym]
            if last is not None:
                s["last_id"], s["last_ts"] = last
            horizon = now - self.backfill_hours * 3600
            if last is None or last[1] < horizon:
                # Nothing recent: seed from the horizon; any older hole stays
                # visible (fill it with scripts/pipeline_backfill.py --fill-gaps).
                since, after = horizon, (last[0] if last else None)
            else:
                since, after = last[1], last[0]
            try:
                res = backfill_range(self.store, self.client, sym, since, after_id=after,
                                     max_pages=self.max_gap_pages)
                if res["last_id"] is not None:
                    s["last_id"] = res["last_id"]
                    lt = self.store.last_tick(sym)
                    s["last_ts"] = lt[1] if lt else s["last_ts"]
                self.log(f"BOOTSTRAP {sym} {res}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"BOOTSTRAP {sym} REST catch-up failed: {type(exc).__name__}: {exc}")
        self._last_compact = now

    def _fill_gap(self, sym: str, until_id: int) -> None:
        s = self.st[sym]
        self._flush_sym(sym)
        lo = s["last_id"]
        try:
            res = backfill_range(self.store, self.client, sym, float(s["last_ts"]) - 1,
                                 after_id=lo, until_id=until_id, max_pages=self.max_gap_pages)
            if res["last_id"] is not None:
                s["last_id"] = res["last_id"]
            filled = res["last_id"] is not None and res["last_id"] >= until_id - 1
            s["gaps_filled" if filled else "gaps_open"] += 1
            self.log(f"GAP {sym} ids {lo + 1}..{until_id - 1} "
                     f"{'filled' if filled else 'PARTIAL'} via REST ({res['written']} trades, "
                     f"{res['pages']} pages)")
        except Exception as exc:  # noqa: BLE001
            s["gaps_open"] += 1
            self.log(f"GAP {sym} ids {lo + 1}..{until_id - 1} left open: {type(exc).__name__}: {exc}")

    # ── message handling ───────────────────────────────────────
    def on_trade(self, sym: str, t: Tick) -> None:
        s = self.st[sym]
        if s["last_id"] is not None and t.trade_id <= s["last_id"]:
            return  # duplicate (snapshot replay / REST overlap)
        if s["last_id"] is not None and t.trade_id > s["last_id"] + 1:
            self._fill_gap(sym, t.trade_id)
            if t.trade_id <= s["last_id"]:
                return
        s["buf"].append(t)
        s["last_id"], s["last_ts"] = t.trade_id, t.ts
        s["in_sync"] = True

    def handle(self, raw: str | bytes) -> None:
        msg = json.loads(raw, parse_float=str)
        if not isinstance(msg, dict):
            return
        self.last_msg = self.now()
        ch = msg.get("channel")
        if ch == "trade":
            for item in msg.get("data") or []:
                parsed = parse_trade(item)
                if parsed and parsed[0] in self.st:
                    self.on_trade(*parsed)
        elif ch == "ticker":
            for item in msg.get("data") or []:
                sym = canonical(item.get("symbol"))
                if sym not in self.st:
                    continue
                s = self.st[sym]
                now = self.now()
                try:
                    bid, ask = float(item["bid"]), float(item["ask"])
                except (KeyError, TypeError, ValueError):
                    continue
                if now - s["last_quote"] >= self.quote_every and bid > 0 and ask > 0:
                    s["quotes"].append((now, bid, ask))
                    s["last_quote"] = now
        elif msg.get("method") == "subscribe" and not msg.get("success", True):
            self.log(f"SUBSCRIBE error: {msg.get('error')}")

    # ── persistence ────────────────────────────────────────────
    def _flush_sym(self, sym: str) -> None:
        s = self.st[sym]
        if s["buf"]:
            s["written"] += self.store.append(sym, s["buf"])
            s["buf"] = []
        if s["quotes"]:
            self.store.append_quotes(sym, s["quotes"])
            s["quotes"] = []

    def flush(self, force: bool = False) -> None:
        now = self.now()
        if not force and now - self._last_flush < self.flush_every:
            return
        for sym in self.symbols:
            self._flush_sym(sym)
            s = self.st[sym]
            live = self.connected and s["in_sync"] and now - self.last_msg < self.idle_timeout
            prev = self.store.read_status(sym)
            self.store.write_status(
                sym, pid=os.getpid(), updated_at=now, connected=self.connected,
                in_sync=bool(live),
                live_through=(min(self.last_msg, now) if live else prev.get("live_through")),
                last_trade_id=s["last_id"], last_trade_ts=s["last_ts"],
                written_session=s["written"], gaps_filled=s["gaps_filled"],
                gaps_open=s["gaps_open"], reconnects=self.reconnects)
        self._last_flush = now
        if now - self._last_compact > 3600:
            for sym in self.symbols:
                self.store.compact(sym, now=now)
                self.store.compact(sym, now=now, kind="quotes")
            self._last_compact = now

    # ── socket loop ────────────────────────────────────────────
    def _open(self):
        if self._connect is not None:
            return self._connect(self.ws_url)
        from websockets.sync.client import connect
        return connect(self.ws_url, open_timeout=15, close_timeout=5, max_size=2 ** 22)

    def subscribe_messages(self) -> list[str]:
        ws_syms = [PAIRS[s][1] for s in self.symbols]
        return [json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": ws_syms,
                                                               "snapshot": True}}),
                json.dumps({"method": "subscribe", "params": {"channel": "ticker", "symbol": ws_syms}})]

    def run_once(self) -> None:
        """One connection lifetime: connect, subscribe, read until error/idle."""
        ws = self._open()
        try:
            for m in self.subscribe_messages():
                ws.send(m)
            self.connected = True
            self.last_msg = self.now()
            self.log(f"CONNECTED {self.ws_url} symbols={','.join(self.symbols)}")
            while self.running:
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    raw = None
                if raw is not None:
                    self.handle(raw)
                elif self.now() - self.last_msg > self.idle_timeout:
                    raise TimeoutError(f"no message for {self.idle_timeout:.0f}s")
                self.flush()
        finally:
            self.connected = False
            for s in self.st.values():
                s["in_sync"] = False
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.flush(force=True)

    def run_forever(self) -> None:
        self.bootstrap()
        backoff = 1.0
        while self.running:
            started = self.now()
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — network faults are expected
                self.log(f"DISCONNECTED {type(exc).__name__}: {exc}")
            if not self.running:
                break
            self.reconnects += 1
            backoff = 1.0 if self.now() - started > 120 else min(backoff * 2, 60.0)
            self.log(f"RECONNECT in {backoff:.0f}s (#{self.reconnects})")
            slept = 0.0
            while self.running and slept < backoff:
                time.sleep(0.5)
                slept += 0.5
        self.flush(force=True)
        self.log("STOP collector")
