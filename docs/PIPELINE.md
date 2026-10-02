# Market-data pipeline: Data → Ticks → Bars → Technicals → Strategy

_Added 2026-10-02. PAPER ONLY — the pipeline is read-only public market data. It
cannot place, cancel or modify orders, needs no API keys, and changes nothing about
the safety locks (`PAPER_TRADING=true DRY_RUN=true ALLOW_LIVE_TRADING=false`)._

Universe: **BTC/USD, ETH/USD, SOL/USD only** (`dublin_bot.pipeline.SYMBOLS`).

```
Kraken WS v2 trade+ticker ─┐                 ┌─> 1m bars (+flow) ─> 15m / 1h / 4h bars
Kraken REST Trades (gaps) ─┼─> tick store ───┤        (only fully-covered closed bars)
                           │  data/ticks/    │
                           │  data/quotes/   └─> technicals.add_technicals  (ONE module)
                                                       │
            gateway.get_bars()  ── pipeline.source ────┴─> regime_trend@60m, meanrev_mk@240m
                 └─ fallback / history: Kraken REST OHLC
```

## 1. Data — `scripts/tick_collector.py` (launchd `com.mayo.kraken.ticks`)

* Public WebSocket v2 (`wss://ws.kraken.com/v2`): `trade` (with snapshot) and `ticker`.
* **Store:** `data/ticks/<SYM>/<YYYY-MM-DD>.csv` — append-only while the UTC day is open,
  compacted (dedup + sort) into `<day>.csv.gz` once it closes. Row:
  `trade_id,ts,price,qty,side,ord_type` (Kraken's exact decimal strings; side = taker side).
  Top of book from `ticker` → `data/quotes/<SYM>/<day>.csv` (`ts,bid,ask`, ≤ 1 row / 5 s).
* **Dedup:** Kraken trade ids are sequential per pair; anything ≤ the last stored id is dropped
  (the resubscribe snapshot always replays the last 50 trades). Readers dedup again by id.
* **Gap detection / fill:** an id jump (reconnect, Mac asleep) triggers a REST `Trades` backfill
  of exactly the missing ids before the live trade is written. Holes too large to fill
  (> 600 pages) stay visible as id holes. On start the collector REST-catches-up from the newest
  stored trade (or 6 h back). `scripts/pipeline_backfill.py --fill-gaps` fills old holes.
* **Reconnect:** exponential backoff 1 → 60 s; a socket silent for 30 s is recycled.
* **Status/heartbeat:** `data/ticks/<SYM>/_status.json` (`live_through`, `in_sync`,
  `last_trade_id`, `gaps_filled`, `gaps_open`, `reconnects`).
* Size: roughly 2–3 MB/day gzipped for BTC on busy days, less for ETH/SOL. `data/ticks/`,
  `data/quotes/` and `data/bars1m/` are gitignored.

## 2. Ticks → bars — `dublin_bot.pipeline.bars`

* 1m bars: OHLC, volume, VWAP, trade count, **buy/sell (taker) volume and counts, order-flow
  imbalance `ofi = (buy − sell) / volume`**, mean quoted spread (bps) when quotes exist.
* 15m / 1h / 4h are aggregated from 1m on the UTC epoch grid (same as Kraken OHLC).
* **Completeness rule:** a bar is emitted only if it lies inside a *coverage run* — stored trades
  with consecutive ids whose first trade is before the bar opens and whose end (last trade, or the
  collector's `live_through` heartbeat while in sync) is at/after the bar closes. Partial first
  bars, bars over an id hole and the still-forming bar are never emitted.
* Verified: rebuilt 1m/15m bars match Kraken's own OHLC exactly (open/high/low/close, volume,
  trade count); see `tests/test_pipeline.py::test_real_ticks_rebuild_kraken_ohlc_exactly`
  (committed 38 KB SOL fixture).
* Closed days' 1m bars are cached in `data/bars1m/<SYM>/` (rebuilt when the tick file changes).

## 3. Technicals — `dublin_bot.technicals`

One module used by the live strategies **and** the backtester (`backtest_core.add_indicators`
now delegates to it; the refactor was checked to give identical trades on every grid spec):
EMA20/50/200, RSI14, ATR14, ATR%, ADX14, ATR% rank, `vol_z` (20-bar volume z-score), and order
flow `ofi`, `ofi_z` (50-bar z), `flow3` (3-bar net taker flow / volume), `cvd`, `spread_bps`.
Order-flow columns are NaN on bars that came from REST OHLC. Candidate entry filters live in
`technicals.FLOW_FILTERS`; switched on, a filter fails **closed** on NaN flow.

## 4. Strategy wiring — `dublin_bot.pipeline.source`

`KrakenGateway.get_bars()` (used by both `regime_trend@60m` and the `meanrev_mk@240m` sleeve)
routes BTC/ETH/SOL at 1/15/60/240m through `PipelineBarSource` when `PIPELINE_ENABLED=true`:

1. build the fully-covered local bars;
2. if the feed is live (heartbeat < `PIPELINE_STALE_SECONDS`, default 600) **and** local bars alone
   give `LOOKBACK_BARS` contiguous bars up to the last closed bar → use them, no REST call;
3. else fetch Kraken REST OHLC (as before) and overlay every local bar on it (local wins; REST
   supplies history and holes). REST down → local bars if any, otherwise the same error as before.

So the bot never stalls on the pipeline: with no ticks at all it behaves exactly like the old REST
path. Until the store holds `LOOKBACK_BARS` of history (500 bars ≈ 21 days at 1h, ≈ 83 days at 4h)
the frames are mixed (`src` column = `ticks` / `ohlc`). The paper loop logs a `PIPELINE` line
every cycle (feed age per coin, bar source per symbol/timeframe).

Config (all default OFF / unchanged; set in `scripts/run_paper_mac.sh`):
`PIPELINE_ENABLED`, `PIPELINE_DATA_DIR`, `PIPELINE_SYMBOLS`, `PIPELINE_STALE_SECONDS`,
`REGIME_FLOW_FILTER=""`, `MEANREV_FLOW_FILTER=""`.

## Operations (Mac)

```bash
cp deploy/com.mayo.kraken.ticks.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mayo.kraken.ticks.plist
tail -f logs/tick_collector.log                      # CONNECTED / GAP / RECONNECT lines
.venv/bin/python scripts/pipeline_backfill.py --fill-gaps    # fill old id holes
.venv/bin/python scripts/pipeline_backfill.py --days 30      # deeper history (slow, ~1 req/s)
launchctl bootout gui/$(id -u)/com.mayo.kraken.ticks          # stop collector (bot falls back to REST)
```

## Order-flow walk-forward study — results (2026-10-02)

Run with `scripts/study_orderflow.py --data-dir <tick store>` on 120 days of
tick-built bars per coin (BTC, ETH and SOL each about 120 days, no holes left after
`--fill-gaps`; the verified Kraken exchange holes are recorded in `_holes.json`).
Full output is in `data/orderflow_study_report.txt` and `data/orderflow_study_results.json`.
Costs: taker 40+5 bps/side (90 bps round trip), maker 25 bps/side; stress 80+10 bps/side.
Numbers are **net bps per trade**, out of sample, folds 1–4, with the live spec held fixed (view A).

### regime @ 60m (baseline, no flow filter)

| coin | trades | win % | mean | median | ex-best-2 | stress |
|------|-------:|------:|-----:|-------:|----------:|-------:|
| BTC  | 13 | 15 | +51  | −110 | −131 | −39 |
| ETH  | 18 | 22 | +8   | −139 | −165 | −78 |
| SOL  | 9  | 56 | +410 | +98  | +72  | +324 |
| ALL  | 40 | 28 | +112 | −117 | −8   | +25 |

Fold means: −135 / −89 / +393 / +176. The positive mean comes entirely from a few
large trend winners: the median trade loses about 1.2% after fees, and without the best two
trades the result is negative. Filters `ofi_pos`, `ofi_z_pos` and `flow3_pos` (ALL means
+123 / +120 / +120 on 38 trades) mostly just remove a couple of ETH trades. None beats
baseline in 3 of 4 folds or has ex-best-2 > 0, so **none is promoted**.

### meanrev (maker entry) @ 240m (baseline, no flow filter)

| coin | trades | win % | mean | median | ex-best-2 | stress |
|------|-------:|------:|-----:|-------:|----------:|-------:|
| BTC  | 6  | 67  | +17  | +58  | −20  | −28 |
| ETH  | 4  | 100 | +288 | +258 | +134 | +241 |
| SOL  | 4  | 75  | +36  | +112 | −179 | −10 |
| ALL  | 14 | 79  | +100 | +91  | +43  | +54 |

Filters `ofi_pos`, `flow3_pos` and `ofi_rising` leave 8–13 trades, below the 30-trade
minimum, and none beats baseline in 3 of 4 folds, so **none is promoted**. Even the baseline
sample is too small (14 trades) to call it an edge.

### Verdict

Pre-registered promotion rules (all must hold, pooled over the 3 coins, view A, base costs):
≥30 OOS trades; mean > 0 and above baseline; beats baseline in ≥3 of 4 folds;
ex-best-2 mean > 0; stress mean > 0; positive on ≥2 coins; and view B (walk-forward
selection) mean > 0. **No order-flow filter passed**, so `REGIME_FLOW_FILTER` and
`MEANREV_FLOW_FILTER` stay empty and the live paper strategy is unchanged. Re-run the
study once the Mac collector has a few more months of native tick history.
