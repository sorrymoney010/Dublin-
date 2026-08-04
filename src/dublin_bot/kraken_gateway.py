from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pandas as pd
import requests

from .config import Settings


class KrakenAPIError(RuntimeError):
    pass


class KrakenGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.kraken_base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Dublin-Trading-OS/0.2"})

    def _public(self, path: str, params: dict[str, object] | None = None) -> dict:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise KrakenAPIError("; ".join(payload["error"]))
        return payload["result"]

    def _private(self, path: str, data: dict[str, object] | None = None) -> dict:
        if not self.settings.has_credentials:
            raise KrakenAPIError("Kraken API credentials are not configured")
        nonce = str(time.time_ns())
        body = {"nonce": nonce, **(data or {})}
        encoded = urlencode(body)
        digest = hashlib.sha256((nonce + encoded).encode()).digest()
        message = path.encode() + digest
        secret = base64.b64decode(self.settings.kraken_api_secret)
        signature = base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode()
        headers = {
            "API-Key": self.settings.kraken_api_key,
            "API-Sign": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        response = self.session.post(
            f"{self.base_url}{path}", data=body, headers=headers, timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise KrakenAPIError("; ".join(payload["error"]))
        return payload["result"]

    def get_bars(self) -> pd.DataFrame:
        result = self._public(
            "/0/public/OHLC",
            {"pair": self.settings.kraken_pair, "interval": self.settings.timeframe_minutes},
        )
        pair_key = next(key for key in result if key != "last")
        rows = result[pair_key]
        frame = pd.DataFrame(
            rows,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "vwap",
                "volume",
                "count",
            ],
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s", utc=True)
        frame = frame.set_index("timestamp")
        numeric = ["open", "high", "low", "close", "volume"]
        frame[numeric] = frame[numeric].astype(float)
        return frame[numeric].tail(self.settings.lookback_bars)

    def _balances(self) -> dict[str, float]:
        if not self.settings.has_credentials:
            return {}
        raw = self._private("/0/private/Balance")
        return {asset: float(amount) for asset, amount in raw.items()}

    def account_equity(self) -> float:
        balances = self._balances()
        if not balances:
            return self.settings.strategy_equity_usd
        usd = balances.get("ZUSD", balances.get("USD", 0.0))
        btc = balances.get("XXBT", balances.get("XBT", balances.get("BTC", 0.0)))
        return usd + btc * self._last_price()

    def has_position(self) -> bool:
        balances = self._balances()
        btc = balances.get("XXBT", balances.get("XBT", balances.get("BTC", 0.0)))
        return btc > 0.0

    def _last_price(self) -> float:
        result = self._public("/0/public/Ticker", {"pair": self.settings.kraken_pair})
        pair_key = next(iter(result))
        return float(result[pair_key]["c"][0])

    def buy_notional(self, notional_usd: float) -> str:
        if self.settings.dry_run:
            return f"kraken-dry-run-buy-{notional_usd:.2f}"
        if not self.settings.live_execution_enabled:
            raise KrakenAPIError("Live Kraken execution is locked")
        volume = notional_usd / self._last_price()
        result = self._private(
            "/0/private/AddOrder",
            {
                "pair": self.settings.kraken_pair,
                "type": "buy",
                "ordertype": "market",
                "volume": f"{volume:.8f}",
                "cl_ord_id": f"dublin-{time.time_ns()}",
            },
        )
        return str(result["txid"][0])

    def close_position(self) -> str:
        if self.settings.dry_run:
            return "kraken-dry-run-close"
        if not self.settings.live_execution_enabled:
            raise KrakenAPIError("Live Kraken execution is locked")
        balances = self._balances()
        volume = balances.get("XXBT", balances.get("XBT", balances.get("BTC", 0.0)))
        if volume <= 0:
            raise KrakenAPIError("No BTC balance is available to close")
        result = self._private(
            "/0/private/AddOrder",
            {
                "pair": self.settings.kraken_pair,
                "type": "sell",
                "ordertype": "market",
                "volume": f"{volume:.8f}",
                "cl_ord_id": f"dublin-close-{time.time_ns()}",
            },
        )
        return str(result["txid"][0])

    def diagnostic(self) -> dict[str, object]:
        bars = self.get_bars()
        result: dict[str, object] = {
            "exchange": "kraken",
            "public_market_data": not bars.empty,
            "latest_close": float(bars.iloc[-1]["close"]),
            "credentials_present": self.settings.has_credentials,
            "private_api": False,
            "withdraw_permission_detected": None,
        }
        if self.settings.has_credentials:
            key_info = self._private("/0/private/GetApiKeyInfo")
            permissions = set(key_info.get("permissions", []))
            result["private_api"] = True
            result["withdraw_permission_detected"] = "withdraw-funds" in permissions
            result["permissions"] = sorted(permissions)
        return result
