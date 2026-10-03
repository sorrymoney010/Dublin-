#!/usr/bin/env python3
"""Replay backtest trades through the learner: old policy vs the audit policy.

For each strategy (as shipped: D1 on) and coin, trades from the pipeline data
are fed chronologically: before each entry the learner gate is asked (at the
entry time); a taken trade is recorded at its exit time; a blocked trade is not
recorded (exactly like the paper loop, where blocked signals only become
shadow trades).

Policies:
  old   = min sample 8, rolling window 20, bench if mean net bps < 0, probation 0.25x
  audit = min sample 30, window 50, bench only if the 90% UPPER bound of mean net
          bps < 0, sizes clamped to [0.5, 1.0], prior weight <= 2  (current code)

Output: data/learner_replay.txt / .json. Read-only on market data; no network.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dublin_bot import learner as L  # noqa: E402
from dublin_bot.backtest_core import Spec, default_grid, simulate  # noqa: E402
from dublin_bot.pipeline import SYMBOLS  # noqa: E402
from study_d1_trendhold import BASE, frame  # noqa: E402


class OldPolicy(L.LearningAgent):
    """The pre-audit gate: n >= 8 (window 20) and mean < 0 benches; probation 0.25x."""

    def __init__(self, path):
        super().__init__(path, strategy_key="replay", min_sample=8, window=20)

    def _should_bench(self, d):
        return d.live_n >= self.min_sample and d.live_bps is not None and d.live_bps < 0

    def _evaluate_key(self, key, symbol, regime, now):
        d = super()._evaluate_key(key, symbol, regime, now)
        if d.state == "probation":
            d.size_mult = 0.25
        return d


def new_policy(path):
    return L.LearningAgent(path, strategy_key="replay", min_sample=30)


def replay(trades, make) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        la = make(Path(tmp) / "learner.json")
        pending, taken, blocked, sized = [], [], [], []
        benches = 0
        for t in sorted(trades, key=lambda x: x.entry_time):
            for p in sorted([p for p in pending if p.exit_time <= t.entry_time], key=lambda x: x.exit_time):
                la.record_trade("X/USD", 100.0 * p.net, "trend", notional=100.0, ts=float(p.exit_time))
                pending.remove(p)
            d = la.gate("X/USD", now=float(t.entry_time), persist=False)
            if d.allow:
                taken.append(t.net * 1e4)
                sized.append(t.net * 1e4 * d.size_mult)
                pending.append(t)
            else:
                blocked.append(t.net * 1e4)
            benches = sum(1 for _ in la.benches)
        return {"n": len(trades), "taken": len(taken), "blocked": len(blocked),
                "taken_sum_bps": round(sum(taken), 1), "blocked_sum_bps": round(sum(blocked), 1),
                "size_weighted_sum_bps": round(sum(sized), 1), "benched_at_end": benches}


def bench_rate(pool_bps, make, *, n_trades=60, sims=400, shift=0.0, seed=7) -> dict:
    """Bootstrap sequences from a trade pool; how often does the policy bench at least once?"""
    import random

    from dublin_bot.backtest_core import Trade

    rng = random.Random(seed)
    hit, blocked = 0, 0
    for _ in range(sims):
        seq = [Trade(0, 0, k * 86400, k * 86400 + 3600, 1.0, 1.0, 0.03, 0.0,
                     (rng.choice(pool_bps) + shift) / 1e4, "x") for k in range(n_trades)]
        r = replay(seq, make)
        hit += r["blocked"] > 0
        blocked += r["blocked"]
    return {"p_any_bench": round(hit / sims, 3), "avg_trades_blocked": round(blocked / sims, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--daily-dir", type=Path, required=True)
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "learner_replay")
    a = ap.parse_args()
    now = 4e9
    grid = default_grid()
    sleeves = {"regime@60m": (60, Spec(grid["regime"][0].name, {**grid["regime"][0].params, "d1": True})),
               "regime@60m(noD1)": (60, grid["regime"][0]),
               "meanrev_mk@240m": (240, Spec(grid["meanrev_mk"][0].name, {**grid["meanrev_mk"][0].params,
                                                                          "d1": True})),
               "trendhold@240m": (240, Spec("trendhold", {"d1": True, "warm": 100}))}
    res: dict = {}
    for key, (tf, spec) in sleeves.items():
        for sym in a.symbols:
            d, _ = frame(a.data_dir, a.daily_dir, sym, tf, now)
            tr = simulate(d, spec, BASE)
            tr = [t for t in tr if t.reason != "eod_mark"]
            res[f"{key} {sym}"] = {"old": replay(tr, OldPolicy), "audit": replay(tr, new_policy)}
    lines = ["learner replay (pipeline data, base costs, D1 on unless noted) — trades a strategy+coin would take",
             f"{'strategy coin':<28}{'n':>4} | {'old: taken blocked blk_sum':>28} | {'audit: taken blocked blk_sum':>30}"]
    tot = {"old": [0, 0, 0.0], "audit": [0, 0, 0.0]}
    for k, v in res.items():
        o, n = v["old"], v["audit"]
        lines.append(f"{k:<28}{o['n']:>4} | {o['taken']:>12} {o['blocked']:>7} {o['blocked_sum_bps']:>+8.0f} | "
                     f"{n['taken']:>13} {n['blocked']:>7} {n['blocked_sum_bps']:>+8.0f}")
        for p, x in (("old", o), ("audit", n)):
            tot[p][0] += x["taken"]
            tot[p][1] += x["blocked"]
            tot[p][2] += x["blocked_sum_bps"]
    lines.append(f"{'TOTAL':<28}     | {tot['old'][0]:>12} {tot['old'][1]:>7} {tot['old'][2]:>+8.0f} | "
                 f"{tot['audit'][0]:>13} {tot['audit'][1]:>7} {tot['audit'][2]:>+8.0f}")
    lines.append("blk_sum = summed net bps of the trades the bench skipped (negative = the bench saved money)")
    # Monte Carlo: bootstrap the pooled regime@60m (no D1) trades, the noisiest sleeve.
    pool = []
    for sym in a.symbols:
        d, _ = frame(a.data_dir, a.daily_dir, sym, 60, now)
        pool += [t.net * 1e4 for t in simulate(d, grid["regime"][0], BASE) if t.reason != "eod_mark"]
    mean = sum(pool) / len(pool)
    mc = {}
    for label, shift in (("as_is", 0.0), ("true_mean_0", -mean), ("true_mean_-50", -mean - 50.0)):
        mc[label] = {"true_mean_bps": round(mean + shift, 1), "old": bench_rate(pool, OldPolicy, shift=shift),
                     "audit": bench_rate(pool, new_policy, shift=shift)}
    res["_monte_carlo_regime_pool"] = {"pool_n": len(pool), "pool_mean_bps": round(mean, 1), **mc}
    lines.append("")
    lines.append(f"Monte Carlo: 400 sequences x 60 trades bootstrapped from {len(pool)} regime@60m trades "
                 f"(mean {mean:+.0f} bps), P(at least one bench) / avg trades blocked")
    for v in mc.values():
        lines.append(f"  true mean {v['true_mean_bps']:>+6.0f} bps: old {v['old']['p_any_bench']:.2f} / "
                     f"{v['old']['avg_trades_blocked']:.1f}   audit {v['audit']['p_any_bench']:.2f} / "
                     f"{v['audit']['avg_trades_blocked']:.1f}")
    txt = "\n".join(lines) + "\n"
    a.out.with_suffix(".txt").write_text(txt, encoding="utf-8")
    a.out.with_suffix(".json").write_text(json.dumps({"results": res, "totals": tot}, indent=1), encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
