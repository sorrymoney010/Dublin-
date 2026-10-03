"""Per-sleeve closed-trade scorecard (paper book).

Source of truth for closed trades is the learner history in
``logs/learner.json`` (each entry: realized ``pnl`` after fees, ``net_bps`` vs
cost basis, ``strategy`` = sleeve key, ``ts``). The learner keeps only the last
200 trades per coin, so every run first appends new trades to an append-only
``logs/closed_trades.jsonl`` (dedup by symbol/ts/pnl) and the scorecard is
computed from that ledger — history survives the learner cap.

Pure stdlib + json; reads no settings, no .env, places nothing.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path


def _key(t: dict) -> str:
    return f"{t.get('symbol')}|{float(t.get('ts', 0)):.3f}|{float(t.get('pnl', 0)):.6f}"


def learner_trades(learner_path: Path) -> list[dict]:
    try:
        data = json.loads(Path(learner_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for sym, c in (data.get("coins") or {}).items():
        for h in c.get("history") or []:
            try:
                pnl = float(h.get("pnl"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(pnl):
                continue
            nb = h.get("net_bps")
            out.append({"symbol": sym, "ts": float(h.get("ts") or 0.0), "pnl": pnl,
                        "net_bps": None if nb is None else float(nb),
                        "sleeve": str(h.get("strategy") or data.get("strategy_key") or "unknown"),
                        "regime": h.get("regime", "unknown")})
    return sorted(out, key=lambda t: t["ts"])


def sync_ledger(learner_path: Path, ledger_path: Path) -> list[dict]:
    """Append unseen learner trades to the jsonl ledger; return the full ledger."""
    ledger_path = Path(ledger_path)
    have: list[dict] = []
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                have.append(json.loads(line))
            except ValueError:
                continue
    seen = {_key(t) for t in have}
    new = [t for t in learner_trades(learner_path) if _key(t) not in seen]
    if new:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fh:
            for t in new:
                fh.write(json.dumps(t, sort_keys=True) + "\n")
    return sorted(have + new, key=lambda t: t["ts"])


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    bps = [t["net_bps"] for t in trades if t.get("net_bps") is not None]
    return {
        "trades": n,
        "wins": sum(p > 0 for p in pnl),
        "win_pct": round(100.0 * sum(p > 0 for p in pnl) / n, 1) if n else None,
        "net_pnl_usd": round(sum(pnl), 4),
        "avg_net_bps": round(statistics.fmean(bps), 1) if bps else None,
        "median_net_bps": round(statistics.median(bps), 1) if bps else None,
        "best_bps": round(max(bps), 1) if bps else None,
        "worst_bps": round(min(bps), 1) if bps else None,
        "last_close_ts": max((t["ts"] for t in trades), default=None),
    }


def build(trades: list[dict], *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    sleeves: dict[str, list[dict]] = {}
    for t in trades:
        sleeves.setdefault(t["sleeve"], []).append(t)
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "date_utc": time.strftime("%Y-%m-%d", time.gmtime(now)),
           "note": "paper book; pnl is realized after fees; net_bps vs cost basis",
           "all": _stats(trades), "last_24h": _stats([t for t in trades if t["ts"] >= now - 86400]),
           "sleeves": {}}
    for name, ts in sorted(sleeves.items()):
        by_sym: dict[str, list[dict]] = {}
        for t in ts:
            by_sym.setdefault(t["symbol"], []).append(t)
        out["sleeves"][name] = {**_stats(ts), "by_symbol": {s: _stats(v) for s, v in sorted(by_sym.items())}}
    return out


def write_scorecard(logs_dir: Path, *, now: float | None = None) -> dict:
    logs_dir = Path(logs_dir)
    trades = sync_ledger(logs_dir / "learner.json", logs_dir / "closed_trades.jsonl")
    card = build(trades, now=now)
    logs_dir.mkdir(parents=True, exist_ok=True)
    tmp = logs_dir / "scorecard.json.tmp"
    tmp.write_text(json.dumps(card, indent=1), encoding="utf-8")
    tmp.replace(logs_dir / "scorecard.json")
    return card
