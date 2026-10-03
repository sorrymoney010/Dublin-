"""Tick archive import, watchdog self-healing and scorecard (no network)."""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

from dublin_bot import watchdog as wd
from dublin_bot.pipeline.archive import (build_manifest, check_manifest, continuity,
                                         merge_store, sha256_file)
from dublin_bot.pipeline.tickstore import TICK_COLS, TickStore
from dublin_bot.scorecard import build, sync_ledger, write_scorecard

DAY = 86400.0
T0 = 1790000000.0 - (1790000000.0 % DAY)  # a UTC midnight


def _gz(path: Path, ids, t0: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(",".join(TICK_COLS) + "\n")
        for k, i in enumerate(ids):
            fh.write(f"{i},{t0 + k:.6f},100.0,0.1,b,m\n")


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ── archive ─────────────────────────────────────────────────────
def test_merge_store_never_clobbers_live_files(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    d1, d2, d3 = _day(T0), _day(T0 + DAY), _day(T0 + 2 * DAY)
    _gz(src / "ticks/BTCUSD" / f"{d1}.csv.gz", range(1, 11), T0)
    _gz(src / "ticks/BTCUSD" / f"{d2}.csv.gz", range(11, 21), T0 + DAY)
    _gz(src / "ticks/BTCUSD" / f"{d3}.csv.gz", range(21, 25), T0 + 2 * DAY)  # "today"
    (src / "ticks/BTCUSD/_holes.json").write_text(json.dumps({"verified": [[5, 5]]}))
    # destination already has a partial d2.gz (collector) and an open d3 csv
    _gz(dst / "ticks/BTCUSD" / f"{d2}.csv.gz", range(15, 23), T0 + DAY)
    live = dst / "ticks/BTCUSD" / f"{d3}.csv"
    live.write_text(",".join(TICK_COLS) + "\n23,1.0,1,1,b,m\n")
    before = live.read_bytes()
    res = merge_store(src, dst, ["BTC/USD"], now=T0 + 2 * DAY + 60)
    assert res["BTC/USD"] == {"added": 1, "merged": 1, "same": 0, "skipped_open_day": 1}
    assert live.read_bytes() == before
    assert not (dst / "ticks/BTCUSD" / f"{d3}.csv.gz").exists()
    ids = TickStore(dst).read("BTC/USD")["trade_id"].tolist()
    assert ids == sorted(set(ids)) and ids[0] == 1 and 22 in ids
    assert (5, 5) in TickStore(dst).verified_holes("BTC/USD")
    # idempotent second import
    again = merge_store(src, dst, ["BTC/USD"], now=T0 + 2 * DAY + 60)
    assert again["BTC/USD"]["same"] == 2 and again["BTC/USD"]["added"] == 0


def test_continuity_and_manifest_check(tmp_path):
    root = tmp_path / "s"
    _gz(root / "ticks/ETHUSD" / f"{_day(T0)}.csv.gz", [1, 2, 3, 7, 8], T0)
    _gz(root / "ticks/ETHUSD" / f"{_day(T0 + DAY)}.csv.gz", [9, 10, 12], T0 + DAY)
    c = continuity(root, "ETH/USD")
    assert c["open_gaps"] == [[4, 6], [11, 11]] and not c["contiguous"]
    TickStore(root).add_verified_hole("ETH/USD", 4, 6)
    TickStore(root).add_verified_hole("ETH/USD", 11, 11)
    c = continuity(root, "ETH/USD")
    assert c["contiguous"] and c["rows"] == 8 and len(c["verified_holes"]) == 2
    man = build_manifest(root, ["ETH/USD"], now=T0 + 5 * DAY)
    files = man["symbols"]["ETH/USD"]["files"]
    assert len(files) == 2 and files[0]["sha256"] == sha256_file(root / files[0]["file"])
    other = tmp_path / "o"
    merge_store(root, other, ["ETH/USD"], now=T0 + 5 * DAY)
    assert check_manifest(other, man)["ETH/USD"]["ok"]
    (other / files[1]["file"]).unlink()
    assert check_manifest(other, man)["ETH/USD"]["missing"] == 1


# ── watchdog ────────────────────────────────────────────────────
class FakeLaunchd:
    def __init__(self, loaded: dict[str, int | None]):
        self.loaded = dict(loaded)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            label = cmd[2].split("/")[-1]
            if label not in self.loaded:
                return 113, "Could not find service"
            pid = self.loaded[label]
            return 0, f"{label} = {{\n\tstate = running\n" + (f"\tpid = {pid}\n" if pid else "") + "}"
        if cmd[:2] == ["launchctl", "bootstrap"]:
            self.loaded[Path(cmd[3]).stem] = 999
            return 0, ""
        if cmd[:2] == ["launchctl", "kickstart"]:
            return 0, ""
        if "pipeline_backfill.py" in " ".join(cmd):
            return 0, "[x] BTC/USD: {'symbol': 'BTC/USD', 'gaps': 0, 'filled': 0}"
        return 1, "unexpected"


def _setup(tmp_path, now, *, cycle_age=60, hb_age=10, locks="locked=True paper=True dry_run=True allow_live=False"):
    repo, data, agents = tmp_path / "repo", tmp_path / "repo/data", tmp_path / "LaunchAgents"
    (repo / "logs").mkdir(parents=True)
    agents.mkdir()
    for lbl in (wd.PAPER, wd.TICKS):
        (agents / f"{lbl}.plist").write_text("<plist/>")
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))  # noqa: E731
    (repo / "logs/paper_trader.log").write_text(
        f"[{iso(now - 7200)}] START paper loop interval=300s equity=500.0 {locks}\n"
        f"[{iso(now - cycle_age)}] CYCLE sleeve=meanrev_4h active=True actions=0 errors=0\n")
    for s in ("BTCUSD", "ETHUSD", "SOLUSD"):
        (data / "ticks" / s).mkdir(parents=True)
        (data / "ticks" / s / "_status.json").write_text(json.dumps({"updated_at": now - hb_age, "gaps_open": 0}))
    return repo, data, agents


def _beat(data, now):
    for s in ("BTCUSD", "ETHUSD", "SOLUSD"):
        (data / "ticks" / s / "_status.json").write_text(json.dumps({"updated_at": now - 5, "gaps_open": 0}))


def _dog(repo, data, agents, fake, now, cards=None):
    return wd.Watchdog(repo=repo, data_dir=data, uid=501, agents_dir=agents, python="py",
                       run=fake, now=lambda: now,
                       scorecard=(lambda p: (cards.append(p), (p / "scorecard.json").write_text("{}")))
                       if cards is not None else None)


def _mutating(fake):
    return [c for c in fake.calls if c[:2] != ["launchctl", "print"]]


def test_watchdog_healthy_does_nothing(tmp_path):
    now = T0 + 3600
    repo, data, agents = _setup(tmp_path, now)
    fake = FakeLaunchd({wd.PAPER: 101, wd.TICKS: 102})
    cards: list = []
    res = _dog(repo, data, agents, fake, now, cards).check()
    assert res["status"] == "HEALTHY" and res["locks_ok"] is True
    assert _mutating(fake) == [] and len(cards) == 1
    log = (repo / "logs/watchdog.log").read_text()
    assert "HEALTHY paper=pid:101" in log and "ticks=pid:102" in log
    # scorecard only once per UTC day
    _beat(data, now + 300)
    _dog(repo, data, agents, fake, now + 300, cards).check()
    assert len(cards) == 1


def test_watchdog_bootstraps_unloaded_paper_job(tmp_path):
    now = T0 + 3600
    repo, data, agents = _setup(tmp_path, now, cycle_age=3000)
    fake = FakeLaunchd({wd.TICKS: 102})
    res = _dog(repo, data, agents, fake, now).check()
    assert res["status"] == "HEAL"
    assert ["launchctl", "bootstrap", "gui/501", str(agents / f"{wd.PAPER}.plist")] in fake.calls
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in fake.calls)
    # within cooldown: no second restart even though log still stale
    fake.calls.clear()
    fake.loaded[wd.PAPER] = 999
    _beat(data, now + 300)
    _dog(repo, data, agents, fake, now + 300).check()
    assert _mutating(fake) == []
    assert "cooldown" in (repo / "logs/watchdog.log").read_text()


def test_watchdog_kickstarts_stale_ticks_and_gapfills(tmp_path):
    now = T0 + 3600
    repo, data, agents = _setup(tmp_path, now, hb_age=600)
    fake = FakeLaunchd({wd.PAPER: 101, wd.TICKS: 102})
    res = _dog(repo, data, agents, fake, now).check()
    assert ["launchctl", "kickstart", "-k", f"gui/501/{wd.TICKS}"] in fake.calls
    fills = [c for c in fake.calls if "pipeline_backfill.py" in " ".join(c)]
    assert len(fills) == 1 and "--fill-gaps" in fills[0] and "--no-compact" in fills[0]
    assert any("reason=ticks stale" in a for a in res["actions"])


def test_watchdog_gapfills_after_wake(tmp_path):
    now = T0 + 3600
    repo, data, agents = _setup(tmp_path, now)
    (repo / "logs/.watchdog_state.json").write_text(json.dumps({"last_run": now - 4000}))
    fake = FakeLaunchd({wd.PAPER: 101, wd.TICKS: 102})
    res = _dog(repo, data, agents, fake, now).check()
    assert res["woke"] and any("reason=wake" in a for a in res["actions"])
    assert not any(c[:2] in (["launchctl", "kickstart"], ["launchctl", "bootstrap"]) for c in fake.calls)


def test_watchdog_alerts_on_locks_but_never_touches_them(tmp_path):
    now = T0 + 3600
    repo, data, agents = _setup(tmp_path, now,
                                locks="locked=False paper=False dry_run=False allow_live=True")
    fake = FakeLaunchd({wd.PAPER: 101, wd.TICKS: 102})
    res = _dog(repo, data, agents, fake, now).check()
    assert res["locks_ok"] is False and "locks" in res["problems"]
    assert _mutating(fake) == []
    assert "ALERT locks" in (repo / "logs/watchdog.log").read_text()
    src = Path(wd.__file__).read_text()
    for forbidden in ("ALLOW_LIVE_TRADING", "PAPER_TRADING=", "DRY_RUN=", ".env", "setenv"):
        assert forbidden not in src


def test_watchdog_missing_plist_is_reported(tmp_path):
    now = T0 + 3600
    repo, data, agents = _setup(tmp_path, now)
    (agents / f"{wd.TICKS}.plist").unlink()
    fake = FakeLaunchd({wd.PAPER: 101})
    res = _dog(repo, data, agents, fake, now).check()
    assert any("plist missing" in a for a in res["actions"])


# ── scorecard ───────────────────────────────────────────────────
def _learner(path: Path, hist: dict) -> None:
    path.write_text(json.dumps({"strategy_key": "regime_trend@60m",
                                "coins": {s: {"history": h} for s, h in hist.items()}}))


def test_scorecard_per_sleeve_and_ledger_survives_cap(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _learner(logs / "learner.json", {
        "BTC/USD": [{"pnl": 2.0, "net_bps": 40.0, "ts": T0 + 1, "strategy": "regime_trend@60m"},
                    {"pnl": -1.0, "net_bps": -20.0, "ts": T0 + 2, "strategy": None}],
        "ETH/USD": [{"pnl": 3.0, "net_bps": 60.0, "ts": T0 + 3, "strategy": "meanrev_mk@240m"}]})
    card = write_scorecard(logs, now=T0 + 10)
    rt = card["sleeves"]["regime_trend@60m"]
    assert rt["trades"] == 2 and rt["win_pct"] == 50.0 and rt["net_pnl_usd"] == 1.0
    assert rt["avg_net_bps"] == 10.0 and card["sleeves"]["meanrev_mk@240m"]["trades"] == 1
    assert card["all"]["trades"] == 3 and (logs / "scorecard.json").exists()
    # learner history rolls over (cap): earlier trades stay in the ledger
    _learner(logs / "learner.json", {"ETH/USD": [{"pnl": -0.5, "net_bps": -10.0, "ts": T0 + 4,
                                                  "strategy": "meanrev_mk@240m"}]})
    trades = sync_ledger(logs / "learner.json", logs / "closed_trades.jsonl")
    assert len(trades) == 4
    assert build(trades)["sleeves"]["meanrev_mk@240m"]["trades"] == 2


def test_scorecard_empty_book(tmp_path):
    card = write_scorecard(tmp_path)
    assert card["all"]["trades"] == 0 and card["sleeves"] == {}
