"""Native Kraken Spot gateway.

Design notes
------------
* **Read-only by default.** Order submission is behind independent gates:
  ``dry_run``, ``paper_trading``/``allow_live_trading``, explicit risk
  acknowledgement, and ``allow_order_submission``.
* **Symbol resolution is data-driven.**
* **Every private call is signed, nonced, rate-limited, and retried** according
  to the error classification in ``errors.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import requests

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import (
    AuthenticationError,
    BrokerError,
    InvalidRequestError,
    RateLimitError,
    SafetyLockError,
    TransientBrokerError,
    classify_kraken_error,
)
from .freshness import FreshnessGuard, FreshnessVerdict, check_monotonic_bars
from .nonce import NonceGenerator
from .precision import PairPrecision, SizedOrder, size_order
from .ratelimit import KrakenRateLimiter, RateLimitTier

KRAKEN_API_BASE = "https://api.kraken.com"
_PUBLIC_PATH = "/0/public"
_PRIVATE_PATH = "/0/private"
VALID_INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
_ASSET_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}


@dataclass(frozen=True)
class SymbolMeta:
    key: str
    altname: str
    wsname: str
    base: str
    quote: str
    lot_decimals: int
    pair_decimals: int
    order_min: Decimal
    cost_min: Decimal
    status: str

    @property
    def tradable(self) -> bool:
        return self.status == "online"

    def to_precision(self) -> PairPrecision:
        return PairPrecision(
            pair=self.key,
            lot_decimals=self.lot_decimals,
            pair_decimals=self.pair_decimals,
            order_min=self.order_min,
            cost_min=self.cost_min,
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "altname": self.altname,
            "wsname": self.wsname,
            "base": self.base,
            "quote": self.quote,
            "lot_decimals": self.lot_decimals,
            "pair_decimals": self.pair_decimals,
            "order_min": str(self.order_min),
            "cost_min": str(self.cost_min),
            "status": self.status,
        }


class KrakenGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        rate_limiter: KrakenRateLimiter | None = None,
        nonce_generator: NonceGenerator | None = None,
        audit: AuditLog | None = None,
        allow_order_submission: bool = False,
        max_retries: int = 3,
        sleep_fn=time.sleep,
    ) -> None:
        self.settings = settings
        self._api_key = settings.kraken_api_key
        self._api_secret = settings.kraken_api_secret
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": "DublinBot/0.3"})
        self._limiter = rate_limiter or KrakenRateLimiter(
            _tier_from_name(getattr(settings, "kraken_tier", "starter"))
        )
        self._nonce = nonce_generator or NonceGenerator(Path(settings.nonce_state_path))
        self._audit = audit
        self._allow_order_submission = allow_order_submission
        self._max_retries = max_retries
        self._sleep = sleep_fn
        self._timeout = settings.http_timeout_seconds
        self._meta: dict[str, SymbolMeta] = {}
        self._meta_loaded_at: float = 0.0
        self._meta_ttl = 3600.0
        self.freshness = FreshnessGuard(
            settings.timeframe_minutes,
            max_bar_age_multiple=settings.max_bar_age_multiple,
            max_clock_skew_seconds=settings.max_clock_skew_seconds,
        )
        self.last_freshness: FreshnessVerdict | None = None

    @property
    def name(self) -> str:
        return "kraken"

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def order_submission_enabled(self) -> bool:
        s = self.settings
        return (
            self._allow_order_submission
            and s.live_execution_armed
            and not s.dry_run
            and not s.paper_trading
            and s.allow_live_trading
            and s.live_risk_acknowledgement == "I_ACCEPT_LIVE_TRADING_RISK"
            and self.has_credentials
        )

    def _log(self, event: AuditEvent, payload: dict, severity: str = "info") -> None:
        if self._audit is not None:
            self._audit.record(event, payload, severity=severity)

    def _handle_payload(self, payload: dict) -> dict:
        errors = payload.get("error") or []
        if errors:
            raise classify_kraken_error(list(errors))
        return payload.get("result", {})

    def _request(self, method: str, url: str, *, params=None, data=None, headers=None) -> dict:
        try:
            if method == "GET":
                response = self._session.get(url, params=params, timeout=self._timeout, headers=headers)
            else:
                response = self._session.post(url, data=data, timeout=self._timeout, headers=headers)
        except requests.Timeout as exc:
            raise TransientBrokerError(f"timeout calling {url}") from exc
        except requests.RequestException as exc:
            raise TransientBrokerError(f"network error calling {url}: {exc}") from exc
        status = getattr(response, "status_code", 200)
        if status == 429:
            raise RateLimitError("HTTP 429 from Kraken")
        if status in (500, 502, 503, 504):
            raise TransientBrokerError(f"HTTP {status} from Kraken")
        if status >= 400:
            raise InvalidRequestError(f"HTTP {status} from Kraken")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerError("Kraken returned a non-JSON response") from exc
        return self._handle_payload(payload)

    def _with_retries(self, description: str, call):
        delay = 1.0
        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return call()
            except (RateLimitError, TransientBrokerError) as exc:
                last = exc
                if isinstance(exc, RateLimitError):
                    self._log(AuditEvent.RATE_LIMIT, {"operation": description, "attempt": attempt}, severity="warning")
                if attempt == self._max_retries:
                    break
                self._sleep(delay)
                delay *= 2
            except AuthenticationError:
                raise
        assert last is not None
        raise last

    def _public(self, endpoint: str, params: dict | None = None) -> dict:
        self._limiter.acquire_public()
        url = f"{KRAKEN_API_BASE}{_PUBLIC_PATH}/{endpoint}"
        return self._with_retries(f"public:{endpoint}", lambda: self._request("GET", url, params=params))

    def _sign(self, urlpath: str, data: dict) -> str:
        encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        try:
            secret = base64.b64decode(self._api_secret)
        except Exception as exc:
            raise AuthenticationError("Kraken API secret is not valid base64") from exc
        mac = hmac.new(secret, message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.has_credentials:
            raise AuthenticationError("Kraken credentials required for private endpoints")
        urlpath = f"{_PRIVATE_PATH}/{endpoint}"
        url = f"{KRAKEN_API_BASE}{urlpath}"

        def call() -> dict:
            self._limiter.acquire_private(endpoint)
            body = dict(params or {})
            body["nonce"] = str(self._nonce.next())
            headers = {
                "API-Key": self._api_key,
                "API-Sign": self._sign(urlpath, body),
                "Content-Type": "application/x-www-form-urlencoded",
            }
            return self._request("POST", url, data=body, headers=headers)

        return self._with_retries(f"private:{endpoint}", call)

    def load_metadata(self, force: bool = False) -> dict[str, SymbolMeta]:
        if self._meta and not force and (time.time() - self._meta_loaded_at) < self._meta_ttl:
            return self._meta
        result = self._public("AssetPairs")
        meta: dict[str, SymbolMeta] = {}
        for key, info in result.items():
            if not isinstance(info, dict):
                continue
            meta[key] = SymbolMeta(
                key=key,
                altname=info.get("altname", key),
                wsname=info.get("wsname", ""),
                base=info.get("base", ""),
                quote=info.get("quote", ""),
                lot_decimals=int(info.get("lot_decimals", 8)),
                pair_decimals=int(info.get("pair_decimals", 8)),
                order_min=Decimal(str(info.get("ordermin", "0"))),
                cost_min=Decimal(str(info.get("costmin", "0"))),
                status=info.get("status", "online"),
            )
        self._meta = meta
        self._meta_loaded_at = time.time()
        return meta

    @staticmethod
    def _candidates(symbol: str) -> list[str]:
        raw = symbol.upper().strip()
        compact = raw.replace("/", "").replace("-", "").replace("_", "")
        options = {raw, compact}
        if "/" in raw:
            base, _, quote = raw.partition("/")
            base_alias = _ASSET_ALIASES.get(base, base)
            quote_alias = _ASSET_ALIASES.get(quote, quote)
            options.update({f"{base_alias}{quote_alias}", f"{base_alias}/{quote_alias}"})
        for src, dst in _ASSET_ALIASES.items():
            if compact.startswith(src):
                options.add(dst + compact[len(src):])
        return [o for o in options if o]

    def resolve_symbol(self, symbol: str | None = None) -> SymbolMeta:
        symbol = symbol or self.settings.symbol
        meta = self.load_metadata()
        candidates = self._candidates(symbol)
        for candidate in candidates:
            if candidate in meta:
                return meta[candidate]
        for candidate in candidates:
            for entry in meta.values():
                if candidate in (entry.altname.upper(), entry.wsname.upper()):
                    return entry
        raise InvalidRequestError(f"Kraken has no asset pair matching {symbol!r}")

    @property
    def pair(self) -> str:
        return self.resolve_symbol().key

    def asset_info(self, symbol: str | None = None) -> SymbolMeta | None:
        try:
            return self.resolve_symbol(symbol)
        except InvalidRequestError:
            return None

    def kraken_interval(self) -> int:
        mins = self.settings.timeframe_minutes
        if mins in VALID_INTERVALS:
            return mins
        return min(VALID_INTERVALS, key=lambda v: abs(v - mins))

    def server_time(self) -> float:
        return float(self._public("Time")["unixtime"])

    def get_bars(self, *, validate: bool = True) -> pd.DataFrame:
        meta = self.resolve_symbol()
        result = self._public("OHLC", {"pair": meta.key, "interval": self.kraken_interval()})
        rows = result.get(meta.key) or result.get(meta.altname) or []
        if not rows:
            for key, value in result.items():
                if key != "last" and isinstance(value, list) and value:
                    rows = value
                    break
        if not rows:
            raise BrokerError(f"No OHLC data returned for {meta.key}")
        timestamps = [pd.Timestamp(int(r[0]), unit="s", tz="UTC") for r in rows]
        frame = pd.DataFrame([
            {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
             "vwap": float(r[5]), "volume": float(r[6]), "trades": int(r[7])}
            for r in rows
        ], index=pd.DatetimeIndex(timestamps, name="timestamp")).sort_index()
        if len(frame) > 1:
            frame = frame.iloc[:-1]
        if validate:
            check_monotonic_bars(frame)
        return frame.tail(self.settings.lookback_bars)

    def get_ticker(self) -> dict:
        meta = self.resolve_symbol()
        result = self._public("Ticker", {"pair": meta.key})
        raw = result.get(meta.key) or result.get(meta.altname)
        if raw is None and result:
            raw = next(iter(result.values()))
        if not raw:
            raise BrokerError(f"No ticker data for {meta.key}")
        return {"bid": float(raw["b"][0]), "ask": float(raw["a"][0]), "last": float(raw["c"][0]),
                "volume_24h": float(raw["v"][1]), "vwap_24h": float(raw["p"][1]), "trades_24h": int(raw["t"][1])}

    def check_freshness(self, bars: pd.DataFrame | None = None) -> FreshnessVerdict:
        try:
            server = self.server_time()
        except BrokerError:
            server = None
        frame = self.get_bars() if bars is None else bars
        verdict = self.freshness.evaluate_bars(frame, server_time=server)
        self.last_freshness = verdict
        self._log(AuditEvent.DATA_FRESHNESS, verdict.to_dict(), severity="info" if verdict.fresh else "warning")
        return verdict

    def market_quality(self) -> dict:
        ticker = self.get_ticker()
        bid, ask = ticker["bid"], ticker["ask"]
        mid = (bid + ask) / 2
        return {"bid": bid, "ask": ask, "mid": mid,
                "spread_bps": ((ask - bid) / mid) * 10_000 if mid > 0 else float("inf"),
                "recent_dollar_volume": ticker["volume_24h"] * ticker["vwap_24h"]}

    def balances(self) -> dict[str, float]:
        if not self.has_credentials:
            return {}
        result = self._private("Balance")
        return {asset: float(amount) for asset, amount in result.items()}

    def account_equity(self) -> float:
        if not self.has_credentials:
            return self.settings.strategy_equity_usd
        try:
            result = self._private("TradeBalance", {"asset": "ZUSD"})
            return float(result.get("eb", 0.0))
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR, {"operation": "account_equity", "error": str(exc)}, severity="warning")
            return self.settings.strategy_equity_usd

    def positions(self) -> list[dict]:
        if not self.has_credentials:
            return []
        try:
            balances = self.balances()
            meta = self.resolve_symbol()
        except BrokerError:
            return []
        quantity = float(balances.get(meta.base, 0.0))
        if quantity <= float(meta.order_min or 0):
            return []
        try:
            price = self.get_ticker()["last"]
        except BrokerError:
            price = 0.0
        return [{"symbol": meta.altname, "asset": meta.base, "side": "long", "quantity": quantity,
                 "average_entry": 0.0, "market_value": quantity * price, "unrealized_pl": 0.0}]

    def has_position(self) -> bool:
        return bool(self.positions())

    def orders(self) -> list[dict]:
        if not self.has_credentials:
            return []
        try:
            result = self._private("OpenOrders")
        except BrokerError:
            return []
        return [{"id": order_id, "symbol": order.get("descr", {}).get("pair", ""),
                 "side": order.get("descr", {}).get("type", ""),
                 "order_type": order.get("descr", {}).get("ordertype", ""),
                 "status": order.get("status", "open"), "volume": float(order.get("vol", 0)),
                 "volume_executed": float(order.get("vol_exec", 0)), "notional": float(order.get("cost", 0)),
                 "userref": order.get("userref")}
                for order_id, order in (result.get("open") or {}).items()]

    def find_order_by_userref(self, userref: int) -> dict | None:
        if not self.has_credentials:
            return None
        for order in self.orders():
            if order.get("userref") == userref:
                return order
        try:
            result = self._private("ClosedOrders", {"userref": userref})
        except BrokerError:
            return None
        for order_id, order in (result.get("closed") or {}).items():
            if order.get("userref") == userref:
                descr = order.get("descr", {})
                return {"id": order_id, "symbol": descr.get("pair", ""), "side": descr.get("type", ""),
                        "status": order.get("status", "closed"), "volume": float(order.get("vol", 0)),
                        "volume_executed": float(order.get("vol_exec", 0)), "notional": float(order.get("cost", 0)),
                        "userref": userref}
        return None

    def size_buy(self, notional_usd: float, price: float | None = None) -> SizedOrder:
        meta = self.resolve_symbol()
        if price is None:
            price = self.get_ticker()["ask"]
        return size_order(notional_usd, price, meta.to_precision(), min_notional_usd=self.settings.min_order_notional_usd)

    def _assert_can_submit(self) -> None:
        if not self.order_submission_enabled:
            raise SafetyLockError(
                "Live order submission disabled: all live flags, acknowledgement, credentials, and execution arm are required."
            )

    def buy_notional(self, notional_usd: float, *, userref: int | None = None) -> str:
        sized = self.size_buy(notional_usd)
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", "pair": sized.pair, "side": "buy",
                                               "volume": sized.volume_str, "price": sized.price_str,
                                               "notional": str(sized.notional), "userref": userref})
            return f"kraken-dry-buy-{userref or int(time.time())}"
        self._assert_can_submit()
        params = {"pair": sized.pair, "type": "buy", "ordertype": "market", "volume": sized.volume_str}
        if userref is not None:
            params["userref"] = str(userref)
        result = self._private("AddOrder", params)
        order_id = (result.get("txid") or ["unknown"])[0]
        self._log(AuditEvent.ORDER_SUBMITTED, {"pair": sized.pair, "side": "buy", "volume": sized.volume_str,
                                               "order_id": order_id, "userref": userref}, severity="warning")
        return order_id

    def close_quantity(self, quantity: float, *, userref: int | None = None) -> str:
        """Sell only the quantity explicitly managed by Dublin."""
        meta = self.resolve_symbol()
        from .precision import round_volume
        volume = round_volume(quantity, meta.to_precision())
        if volume <= 0:
            raise BrokerError(f"Managed quantity {quantity} rounds to zero for {meta.key}")
        if volume > Decimal(str(quantity)):
            raise BrokerError("Rounded exit volume exceeds managed quantity")
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", "pair": meta.key, "side": "sell",
                                               "volume": format(volume, "f"), "userref": userref})
            return f"kraken-dry-close-{userref or int(time.time())}"
        self._assert_can_submit()
        params = {"pair": meta.key, "type": "sell", "ordertype": "market", "volume": format(volume, "f")}
        if userref is not None:
            params["userref"] = str(userref)
        result = self._private("AddOrder", params)
        order_id = (result.get("txid") or ["unknown"])[0]
        self._log(AuditEvent.ORDER_SUBMITTED, {"pair": meta.key, "side": "sell", "volume": format(volume, "f"),
                                               "order_id": order_id, "userref": userref}, severity="warning")
        return order_id

    def close_position(self, *, userref: int | None = None) -> str:
        """Compatibility helper. Prefer close_quantity() from the engine."""
        positions = self.positions()
        if not positions:
            raise BrokerError("No position to close")
        return self.close_quantity(positions[0]["quantity"], userref=userref)

    def health(self, *, include_freshness: bool = True) -> dict:
        status: dict[str, object] = {
            "broker": "kraken", "credentials_present": self.has_credentials,
            "order_submission_enabled": self.order_submission_enabled,
            "live_execution_armed": self.settings.live_execution_armed,
            "rate_limiter": self._limiter.snapshot(), "last_nonce": self._nonce.last,
        }
        started = time.monotonic()
        try:
            server = self.server_time()
            status.update({"reachable": True, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                           "clock_skew_seconds": round(server - time.time(), 3), "error": None})
        except Exception as exc:
            status.update({"reachable": False, "latency_ms": None, "clock_skew_seconds": None, "error": str(exc)})
        if include_freshness and self.last_freshness is None and status["reachable"]:
            try:
                self.check_freshness()
            except Exception as exc:
                status.setdefault("freshness_error", str(exc))
        if self.last_freshness is not None:
            status["freshness"] = self.last_freshness.to_dict()
        return status


def _tier_from_name(name: str) -> RateLimitTier:
    return {"starter": RateLimitTier.starter(), "intermediate": RateLimitTier.intermediate(),
            "pro": RateLimitTier.pro()}.get(str(name).lower(), RateLimitTier.starter())
