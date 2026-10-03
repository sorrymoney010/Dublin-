"""Minimal public Kraken REST client for the pipeline (no keys, read-only)."""
from __future__ import annotations

import sys
import time
from typing import Callable

import requests

from .tickstore import Tick

API = "https://api.kraken.com/0/public/"
_RETRYABLE = ("Too many requests", "Unavailable", "Busy", "Throttled", "timeout")


class KrakenPublic:
    def __init__(self, *, session: requests.Session | None = None, timeout: float = 20.0,
                 min_interval: float = 1.0, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "mayo-bot-pipeline/1.0")
        self.timeout = timeout
        self.min_interval = min_interval
        self._sleep = sleep
        self._last = 0.0
        self._log = log or (lambda m: print(m, file=sys.stderr, flush=True))

    def get(self, method: str, params: dict, *, retries: int = 8) -> dict:
        delay = 2.0
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(API + method, params=params, timeout=self.timeout)
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == retries - 1:
                    raise
                self._log(f"pipeline REST {method} retry after {type(exc).__name__}")
                self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            errs = payload.get("error") or []
            if errs:
                err = ";".join(errs)
                if any(k in err for k in _RETRYABLE) and attempt < retries - 1:
                    self._log(f"pipeline REST {method} {err}; backoff {delay:.0f}s")
                    self._sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise RuntimeError(f"Kraken {method}: {err}")
            return payload["result"]
        raise RuntimeError(f"Kraken {method} failed after {retries} retries")

    def trades(self, pair: str, since: str | int | None, count: int = 1000) -> tuple[list[Tick], str]:
        """One page of public trades at/after ``since`` (unix s, or Kraken's ns ``last``).

        Returns (ticks, last_cursor). Kraken rows:
        [price, volume, time, side, ord_type, misc, trade_id].
        """
        params: dict = {"pair": pair, "count": count}
        if since is not None:
            params["since"] = str(since)
        res = self.get("Trades", params)
        key = next((k for k in res if k != "last"), None)
        rows = res.get(key, []) if key else []
        ticks = [Tick(trade_id=int(r[6]), ts=float(r[2]), price=str(r[0]), qty=str(r[1]),
                      side=str(r[3])[:1], ord_type=str(r[4])[:1]) for r in rows if len(r) >= 7]
        return ticks, str(res.get("last", ""))

    def ohlc(self, pair: str, interval: int, *, retries: int = 8) -> list[list]:
        res = self.get("OHLC", {"pair": pair, "interval": interval}, retries=retries)
        key = next((k for k in res if k != "last"), None)
        return res.get(key, []) if key else []
