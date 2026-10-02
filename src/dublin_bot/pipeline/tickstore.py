"""Append-only tick store: ``<root>/ticks/<SYM>/<YYYY-MM-DD>.csv[.gz]``.

Row schema (header on every file)::

    trade_id,ts,price,qty,side,ord_type

* ``trade_id`` – Kraken's per-pair sequential trade id (int). It is the dedup
  key and the gap detector: consecutive stored ids must differ by exactly 1.
* ``ts``       – exchange timestamp, float seconds since the epoch (UTC).
* ``price`` / ``qty`` – decimal strings exactly as Kraken sent them.
* ``side``     – taker side: ``b`` (buy) / ``s`` (sell).
* ``ord_type`` – ``m`` (market) / ``l`` (limit).

The open UTC day is a plain csv that is only ever appended to (one ``write``
per batch). ``compact()`` turns every closed day into a deduplicated, sorted
``csv.gz`` atomically. Readers merge both forms, dedup by ``trade_id`` and
skip a torn last line, so a crash mid-write can never corrupt history.

Top-of-book quotes from the ticker feed go to ``<root>/quotes/<SYM>/`` with
schema ``ts,bid,ask`` (same day/compaction rules).
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from . import fs_key

TICK_COLS = ["trade_id", "ts", "price", "qty", "side", "ord_type"]
QUOTE_COLS = ["ts", "bid", "ask"]


@dataclass(frozen=True)
class Tick:
    trade_id: int
    ts: float
    price: str
    qty: str
    side: str      # "b" | "s"
    ord_type: str  # "m" | "l"

    def line(self) -> str:
        return f"{self.trade_id},{self.ts:.6f},{self.price},{self.qty},{self.side},{self.ord_type}\n"


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _days_between(start_ts: float, end_ts: float) -> list[str]:
    d0 = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
    d1 = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
    out = []
    while d0 <= d1:
        out.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    return out


def _read_csv_any(path: Path, cols: list[str]) -> pd.DataFrame:
    """Read a plain or gzip csv, tolerating a torn final line / member."""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = path.read_text(encoding="utf-8")
    except (EOFError, OSError):
        # Truncated gzip member: salvage what decompresses.
        text = _salvage_gzip(path) if path.suffix == ".gz" else ""
    if not text.strip():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(io.StringIO(text), dtype=str, on_bad_lines="skip")
    df = df[[c for c in cols if c in df.columns]]
    df = df[df[cols[0]] != cols[0]]  # repeated headers from appended batches
    return df


def _salvage_gzip(path: Path) -> str:
    out = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                out.append(chunk)
    except (EOFError, OSError):
        pass
    return "".join(out)


class TickStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ── paths ──────────────────────────────────────────────────
    def sym_dir(self, symbol: str, kind: str = "ticks") -> Path:
        return self.root / kind / fs_key(symbol)

    def status_path(self, symbol: str) -> Path:
        return self.sym_dir(symbol) / "_status.json"

    def day_files(self, symbol: str, kind: str = "ticks") -> list[Path]:
        d = self.sym_dir(symbol, kind)
        if not d.exists():
            return []
        return sorted(p for p in d.iterdir() if p.name[:4].isdigit()
                      and (p.name.endswith(".csv") or p.name.endswith(".csv.gz")))

    # ── write ──────────────────────────────────────────────────
    def append(self, symbol: str, ticks: Iterable[Tick]) -> int:
        """Append ticks (any order, may span days). One write per day file."""
        by_day: dict[str, list[str]] = {}
        n = 0
        for t in ticks:
            by_day.setdefault(utc_day(t.ts), []).append(t.line())
            n += 1
        for day, lines in by_day.items():
            self._append_lines(self.sym_dir(symbol) / f"{day}.csv", TICK_COLS, lines)
        return n

    def append_quotes(self, symbol: str, rows: Iterable[tuple[float, float, float]]) -> int:
        by_day: dict[str, list[str]] = {}
        n = 0
        for ts, bid, ask in rows:
            by_day.setdefault(utc_day(ts), []).append(f"{ts:.3f},{bid},{ask}\n")
            n += 1
        for day, lines in by_day.items():
            self._append_lines(self.sym_dir(symbol, "quotes") / f"{day}.csv", QUOTE_COLS, lines)
        return n

    @staticmethod
    def _append_lines(path: Path, cols: list[str], lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        payload = ("".join([",".join(cols) + "\n"] if new else []) + "".join(lines)).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    def write_status(self, symbol: str, **info) -> None:
        p = self.status_path(symbol)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"symbol": symbol, **info}, default=str), encoding="utf-8")
        tmp.replace(p)

    def read_status(self, symbol: str) -> dict:
        try:
            data = json.loads(self.status_path(symbol).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, KeyError):
            return {}

    # ── read ───────────────────────────────────────────────────
    def read(self, symbol: str, start_ts: float | None = None,
             end_ts: float | None = None) -> pd.DataFrame:
        """Deduplicated ticks sorted by trade_id, typed. Columns: TICK_COLS."""
        files = self._files_for(symbol, "ticks", start_ts, end_ts)
        parts = [_read_csv_any(p, TICK_COLS) for p in files]
        parts = [p for p in parts if len(p)]
        if not parts:
            return _empty_ticks()
        df = pd.concat(parts, ignore_index=True)
        df["trade_id"] = pd.to_numeric(df["trade_id"], errors="coerce")
        df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
        df = df.dropna(subset=["trade_id", "ts", "price", "qty"])
        df = df[df["side"].isin(["b", "s"])]
        df["trade_id"] = df["trade_id"].astype(np.int64)
        df = (df.drop_duplicates("trade_id", keep="first")
              .sort_values("trade_id", kind="stable").reset_index(drop=True))
        if start_ts is not None:
            df = df[df["ts"] >= start_ts]
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        return df.reset_index(drop=True)

    def read_quotes(self, symbol: str, start_ts: float | None = None,
                    end_ts: float | None = None) -> pd.DataFrame:
        files = self._files_for(symbol, "quotes", start_ts, end_ts)
        parts = [_read_csv_any(p, QUOTE_COLS) for p in files]
        parts = [p for p in parts if len(p)]
        if not parts:
            return pd.DataFrame({"ts": pd.Series(dtype=float), "bid": pd.Series(dtype=float),
                                 "ask": pd.Series(dtype=float)})
        df = pd.concat(parts, ignore_index=True)
        for c in QUOTE_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna().drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        if start_ts is not None:
            df = df[df["ts"] >= start_ts]
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        return df.reset_index(drop=True)

    def _files_for(self, symbol: str, kind: str, start_ts, end_ts) -> list[Path]:
        files = self.day_files(symbol, kind)
        if start_ts is None and end_ts is None:
            return files
        lo = utc_day(start_ts) if start_ts is not None else "0000-00-00"
        hi = utc_day(end_ts) if end_ts is not None else "9999-99-99"
        return [p for p in files if lo <= p.name[:10] <= hi]

    def last_tick(self, symbol: str) -> tuple[int, float] | None:
        """(trade_id, ts) of the newest stored trade, scanning newest day first."""
        files = self.day_files(symbol)
        days = sorted({p.name[:10] for p in files}, reverse=True)
        for day in days[:3]:
            df = self.read(symbol, *_day_bounds(day))
            if len(df):
                row = df.iloc[-1]
                return int(row["trade_id"]), float(row["ts"])
        return None

    def first_tick(self, symbol: str) -> tuple[int, float] | None:
        files = self.day_files(symbol)
        days = sorted({p.name[:10] for p in files})
        for day in days[:3]:
            df = self.read(symbol, *_day_bounds(day))
            if len(df):
                row = df.iloc[0]
                return int(row["trade_id"]), float(row["ts"])
        return None

    # ── maintenance ────────────────────────────────────────────
    def compact(self, symbol: str, *, now: float | None = None, kind: str = "ticks") -> list[str]:
        """Gzip every closed UTC day (merge with an existing .gz, dedup, sort)."""
        today = utc_day(now if now is not None else time.time())
        cols = TICK_COLS if kind == "ticks" else QUOTE_COLS
        key = "trade_id" if kind == "ticks" else "ts"
        done = []
        for p in self.day_files(symbol, kind):
            if not p.name.endswith(".csv") or p.name[:10] >= today:
                continue
            gz = p.with_name(p.name + ".gz")
            parts = [_read_csv_any(p, cols)]
            if gz.exists():
                parts.append(_read_csv_any(gz, cols))
            df = pd.concat([x for x in parts if len(x)] or [pd.DataFrame(columns=cols)])
            if len(df):
                df["_k"] = pd.to_numeric(df[key], errors="coerce")
                df = (df.dropna(subset=["_k"]).drop_duplicates("_k")
                      .sort_values("_k").drop(columns="_k"))
            tmp = gz.with_name(gz.name + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
                df[cols].to_csv(fh, index=False)
            tmp.replace(gz)
            p.unlink()
            done.append(gz.name)
        return done


def _day_bounds(day: str) -> tuple[float, float]:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def _empty_ticks() -> pd.DataFrame:
    return pd.DataFrame({
        "trade_id": pd.Series(dtype=np.int64), "ts": pd.Series(dtype=float),
        "price": pd.Series(dtype=float), "qty": pd.Series(dtype=float),
        "side": pd.Series(dtype=str), "ord_type": pd.Series(dtype=str),
    })


def find_gaps(ticks: pd.DataFrame) -> list[tuple[int, int, float, float]]:
    """Missing trade-id ranges: [(first_missing_id, last_missing_id, ts_before, ts_after)]."""
    if len(ticks) < 2:
        return []
    ids = ticks["trade_id"].to_numpy(np.int64)
    ts = ticks["ts"].to_numpy(float)
    holes = np.nonzero(np.diff(ids) > 1)[0]
    return [(int(ids[i] + 1), int(ids[i + 1] - 1), float(ts[i]), float(ts[i + 1])) for i in holes]
