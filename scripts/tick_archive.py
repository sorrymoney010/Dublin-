#!/usr/bin/env python3
"""Tick-store archive tool (no network, no keys).

    # on the source machine: write a manifest (sha256 + id ranges + continuity)
    python scripts/tick_archive.py manifest --data-dir /path/to/store --out MANIFEST.json
    # on the destination: merge closed days in (never touches the open day)
    python scripts/tick_archive.py import --src /tmp/extracted --data-dir data --manifest MANIFEST.json
    # continuity report (trade-id holes, verified exchange holes excluded)
    python scripts/tick_archive.py verify --data-dir data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.pipeline import SYMBOLS  # noqa: E402
from dublin_bot.pipeline.archive import (  # noqa: E402
    build_manifest, check_manifest, continuity, dumps, merge_store)


def _brief(c: dict) -> dict:
    return {k: c[k] for k in ("days", "first_day", "last_day", "rows", "first_id", "last_id",
                              "contiguous")} | {"open_gaps": len(c["open_gaps"]),
                                                "verified_holes": len(c["verified_holes"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--data-dir", required=True)
    m.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("--src", required=True)
    i.add_argument("--data-dir", default=str(ROOT / "data"))
    i.add_argument("--manifest")
    v = sub.add_parser("verify")
    v.add_argument("--data-dir", default=str(ROOT / "data"))
    for p in (m, i, v):
        p.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    a = ap.parse_args()

    if a.cmd == "manifest":
        man = build_manifest(a.data_dir, a.symbols)
        Path(a.out).write_text(dumps(man), encoding="utf-8")
        for sym, info in man["symbols"].items():
            print(sym, json.dumps(_brief(info["continuity"])))
        return 0
    if a.cmd == "import":
        print("merge", json.dumps(merge_store(a.src, a.data_dir, a.symbols)))
        ok = True
        if a.manifest:
            chk = check_manifest(a.data_dir, json.loads(Path(a.manifest).read_text(encoding="utf-8")))
            print("manifest_check", json.dumps(chk))
            ok = all(r["ok"] for r in chk.values())
        for sym in a.symbols:
            print(sym, json.dumps(_brief(continuity(a.data_dir, sym))))
        return 0 if ok else 2
    bad = False
    for sym in a.symbols:
        c = continuity(a.data_dir, sym)
        bad |= not c["contiguous"]
        print(sym, json.dumps(_brief(c)), "open:", c["open_gaps"][:5])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
