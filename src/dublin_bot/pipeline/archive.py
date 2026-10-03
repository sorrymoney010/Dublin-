"""Tick-store archive tools: manifest, verified merge/import, continuity check.

Used to move backfilled history between machines without disturbing a live
collector:

* only CLOSED UTC days (``YYYY-MM-DD.csv.gz``) are ever written;
* an existing day ``.gz`` in the destination is merged (dedup by ``trade_id``,
  sorted) into a temp file and atomically replaced, never overwritten blindly;
* a plain ``.csv`` (the collector's open/uncompacted day) is never touched —
  ``TickStore.compact`` merges it with the ``.gz`` later;
* ``_holes.json`` (verified exchange-side holes) is unioned.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import canonical, fs_key
from .tickstore import TICK_COLS, TickStore, _read_csv_any, utc_day


def sha256_file(path: Path | str, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(bufsize), b""):
            h.update(chunk)
    return h.hexdigest()


def _ids(path: Path) -> np.ndarray:
    df = _read_csv_any(path, TICK_COLS)
    if not len(df):
        return np.empty(0, dtype=np.int64)
    ids = pd.to_numeric(df["trade_id"], errors="coerce").dropna().to_numpy(np.int64)
    return np.unique(ids)


def _closed_gz_days(store: TickStore, sym: str, today: str) -> list[Path]:
    return [p for p in store.day_files(sym) if p.name.endswith(".csv.gz") and p.name[:10] < today]


def merge_day(src: Path, dst: Path) -> str:
    """Merge one closed-day gz into ``dst`` atomically. Returns added|merged|same."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".import.tmp")
    if not dst.exists():
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        return "added"
    a, b = _read_csv_any(dst, TICK_COLS), _read_csv_any(src, TICK_COLS)
    df = pd.concat([x for x in (a, b) if len(x)] or [pd.DataFrame(columns=TICK_COLS)])
    df["_k"] = pd.to_numeric(df["trade_id"], errors="coerce")
    df = df.dropna(subset=["_k"]).drop_duplicates("_k").sort_values("_k").drop(columns="_k")
    if len(df) == len(a):
        return "same"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        df[TICK_COLS].to_csv(fh, index=False)
    tmp.replace(dst)
    return "merged"


def merge_store(src_root: Path | str, dst_root: Path | str, symbols, *,
                now: float | None = None) -> dict:
    """Import every closed-day gz of ``symbols`` from src into dst (see module doc)."""
    today = utc_day(now if now is not None else time.time())
    src, dst = TickStore(src_root), TickStore(dst_root)
    out = {}
    for raw in symbols:
        sym = canonical(raw) or raw
        counts = {"added": 0, "merged": 0, "same": 0, "skipped_open_day": 0}
        for p in src.day_files(sym):
            if not p.name.endswith(".csv.gz"):
                continue
            if p.name[:10] >= today:
                counts["skipped_open_day"] += 1
                continue
            counts[merge_day(p, dst.sym_dir(sym) / p.name)] += 1
        for lo, hi in sorted(src.verified_holes(sym)):
            if (lo, hi) not in dst.verified_holes(sym):
                dst.add_verified_hole(sym, lo, hi)
        out[sym] = counts
    return out


def continuity(root: Path | str, symbol: str) -> dict:
    """Stream every day file in order and report trade-id holes.

    Holes listed in ``_holes.json`` (verified exchange-side) are reported
    separately and do not count as open.
    """
    store = TickStore(root)
    sym = canonical(symbol) or symbol
    known = store.verified_holes(sym)
    by_day: dict[str, list[Path]] = {}
    for p in store.day_files(sym):
        by_day.setdefault(p.name[:10], []).append(p)
    prev_last = None
    rows = 0
    open_gaps, verified = [], []
    first_id = last_id = None
    for day in sorted(by_day):
        ids = np.unique(np.concatenate([_ids(p) for p in by_day[day]]))
        if not len(ids):
            continue
        if prev_last is not None and ids[0] <= prev_last:
            ids = ids[ids > prev_last]
            if not len(ids):
                continue
        chain = ids if prev_last is None else np.concatenate([[prev_last], ids])
        for i in np.nonzero(np.diff(chain) > 1)[0]:
            g = (int(chain[i] + 1), int(chain[i + 1] - 1))
            (verified if g in known else open_gaps).append(list(g))
        rows += len(ids)
        first_id = int(ids[0]) if first_id is None else first_id
        last_id = prev_last = int(ids[-1])
    return {"symbol": sym, "days": len(by_day), "first_day": min(by_day, default=None),
            "last_day": max(by_day, default=None), "rows": rows, "first_id": first_id,
            "last_id": last_id, "open_gaps": open_gaps, "verified_holes": verified,
            "contiguous": not open_gaps}


def build_manifest(root: Path | str, symbols, *, now: float | None = None) -> dict:
    """Per-symbol closed-day files with sha256 + row/id range, plus continuity."""
    today = utc_day(now if now is not None else time.time())
    store = TickStore(root)
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "layout": "ticks/<SYM>/<YYYY-MM-DD>.csv.gz", "schema": TICK_COLS, "symbols": {}}
    for raw in symbols:
        sym = canonical(raw) or raw
        files = []
        for p in _closed_gz_days(store, sym, today):
            ids = _ids(p)
            files.append({"file": f"ticks/{fs_key(sym)}/{p.name}", "sha256": sha256_file(p),
                          "bytes": p.stat().st_size, "rows": int(len(ids)),
                          "first_id": int(ids[0]) if len(ids) else None,
                          "last_id": int(ids[-1]) if len(ids) else None})
        out["symbols"][sym] = {"files": files, "continuity": continuity(root, sym)}
    return out


def check_manifest(root: Path | str, manifest: dict) -> dict:
    """Compare a store against a manifest: every listed day must exist and hold
    at least the listed id range/rows (merged days may hold more)."""
    root = Path(root)
    res = {}
    for sym, info in manifest.get("symbols", {}).items():
        exact = superset = missing = short = 0
        for f in info["files"]:
            p = root / f["file"]
            if not p.exists():
                missing += 1
                continue
            if sha256_file(p) == f["sha256"]:
                exact += 1
                continue
            ids = _ids(p)
            if f["rows"] and len(ids) >= f["rows"] and ids[0] <= f["first_id"] and ids[-1] >= f["last_id"]:
                superset += 1
            else:
                short += 1
        res[sym] = {"listed": len(info["files"]), "sha_match": exact, "merged_superset": superset,
                    "missing": missing, "short": short, "ok": missing == 0 and short == 0}
    return res


def dumps(obj) -> str:
    buf = io.StringIO()
    json.dump(obj, buf, indent=1, default=str)
    return buf.getvalue()
