# Watchdog, scorecard and tick-archive import (Mac, paper only)

## Watchdog — `com.mayo.kraken.watchdog`

`scripts/run_watchdog_mac.sh` → `scripts/watchdog.py` → `dublin_bot.watchdog`, run by launchd
every 5 minutes (`StartInterval` 300, `RunAtLoad`). Each pass:

| check | healthy when | fix |
|---|---|---|
| `com.mayo.kraken.paper` | loaded in `gui/<uid>` with a pid, and the newest `CYCLE`/`START` line in `logs/paper_trader.log` is < 15 min old | not loaded → `launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/com.mayo.kraken.paper.plist`; loaded but stale → `launchctl kickstart -k` |
| `com.mayo.kraken.ticks` | loaded with a pid, and the newest collector heartbeat (`updated_at` in `data/ticks/*/_status.json`) is < 2 min old | same |
| sleep/wake | the previous watchdog run was ≤ 15 min ago | otherwise (and also when ticks were stale or the collector reports `gaps_open`) → `pipeline_backfill.py --fill-gaps --since-hours 48 --no-compact`, at most every 30 min |
| scorecard | `logs/scorecard.json` was written today (UTC) | write it |

* Each job gets at most one restart per 15 min (cooldown), so a job that is still starting up is never restarted in a loop.
* The watchdog **never** edits config, environment, plists or the safety locks. It reads the
  latest `START` line and logs `ALERT locks …` if the locks ever look off, and takes no other action.
* Log: `logs/watchdog.log` (rotates at 5 MB). The `HEALTHY` line shows paper pid and cycle age, lock
  status, ticks pid, heartbeat age and open gaps. A `HEAL` line lists the problems and the actions taken.
  State is kept in `logs/.watchdog_state.json`.

```bash
cp deploy/com.mayo.kraken.watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mayo.kraken.watchdog.plist
tail -f logs/watchdog.log
launchctl bootout gui/$(id -u)/com.mayo.kraken.watchdog      # disable
```

## Daily scorecard — `logs/scorecard.json`

`scripts/scorecard.py` (also run by the watchdog once per UTC day) reads the closed trades the
sleeves record in `logs/learner.json`, appends new ones to the append-only
`logs/closed_trades.jsonl` (so history survives the learner's 200-trades-per-coin cap), and writes
per-sleeve and per-coin stats: trades, wins, win %, P&L in USD (realized, after fees), average and
median net bps, best and worst trade, plus `all` and `last_24h` totals.

## Tick history archive / import

`scripts/tick_archive.py` moves backfilled history between stores without touching a live collector:

```bash
python scripts/tick_archive.py manifest --data-dir /path/store --out MANIFEST.json  # sha256 + id ranges + continuity
python scripts/tick_archive.py import --src /path/extracted --data-dir data --manifest MANIFEST.json
python scripts/tick_archive.py verify --data-dir data                                # trade-id continuity
```

Import only writes **closed** UTC days. A day `.gz` that already exists is merged (dedup by
`trade_id`) through a temp file and an atomic rename. The collector's plain `.csv` files are never
touched (the collector's hourly compaction merges them later), and `_holes.json` is unioned.
The manifest check passes when every listed day exists with either the same sha256 or a superset of
its rows and id range.
