"""Market-data pipeline: Data -> Ticks -> Bars -> Technicals (BTC/ETH/SOL only).

Layers (each importable and testable on its own):

* ``tickstore``  – append-only per-symbol daily tick files (``csv`` while the
  UTC day is open, compacted to ``csv.gz`` once it closes), dedup by Kraken's
  per-pair sequential ``trade_id``, gap detection from id holes.
* ``kraken_rest`` / ``backfill`` – public REST ``Trades`` paging used for
  history and to fill gaps (no keys, read-only).
* ``collector``  – long-running WebSocket v2 ``trade`` + ``ticker`` client with
  reconnect and REST gap-fill. Writes ticks + throttled top-of-book quotes.
* ``bars``       – ticks -> 1m/15m/1h/4h OHLCV + microstructure (buy/sell
  volume, trade count, VWAP, order-flow imbalance, spread). Only bars whose
  whole interval is provably covered by the tick stream are emitted.
* ``source``     – what the strategies call: local tick-built bars, stitched
  onto Kraken REST OHLC when local data is missing/short/stale.

Indicators live in ``dublin_bot.technicals`` (shared by live and backtest).
Nothing in this package can place, cancel or modify orders.
"""
from __future__ import annotations

# The pipeline is deliberately restricted to three liquid coins.
SYMBOLS: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")

# canonical -> (REST pair, WebSocket v2 symbol, file-system key)
PAIRS: dict[str, tuple[str, str, str]] = {
    "BTC/USD": ("XBTUSD", "BTC/USD", "BTCUSD"),
    "ETH/USD": ("ETHUSD", "ETH/USD", "ETHUSD"),
    "SOL/USD": ("SOLUSD", "SOL/USD", "SOLUSD"),
}

TIMEFRAMES: tuple[int, ...] = (1, 15, 60, 240)

_ALIASES = {
    "XBTUSD": "BTC/USD", "XXBTZUSD": "BTC/USD", "BTCUSD": "BTC/USD", "XBT/USD": "BTC/USD",
    "ETHUSD": "ETH/USD", "XETHZUSD": "ETH/USD",
    "SOLUSD": "SOL/USD",
}


def canonical(symbol: str | None) -> str | None:
    """Map any Kraken spelling (XBTUSD, XXBTZUSD, BTC/USD, ...) to BTC/USD etc.

    Returns ``None`` for symbols outside the pipeline universe.
    """
    if not symbol:
        return None
    s = str(symbol).strip().upper()
    if s in PAIRS:
        return s
    flat = s.replace("/", "").replace("-", "").replace("_", "")
    return _ALIASES.get(flat) or _ALIASES.get(s)


def fs_key(symbol: str) -> str:
    c = canonical(symbol)
    if c is None:
        raise ValueError(f"{symbol!r} is not in the pipeline universe {SYMBOLS}")
    return PAIRS[c][2]
