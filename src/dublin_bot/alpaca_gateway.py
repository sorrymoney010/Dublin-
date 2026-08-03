from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from .config import Settings


class AlpacaGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data = CryptoHistoricalDataClient(
            api_key=settings.alpaca_api_key or None,
            secret_key=settings.alpaca_api_secret or None,
        )
        self.trading = None
        if settings.has_credentials:
            self.trading = TradingClient(
                settings.alpaca_api_key,
                settings.alpaca_api_secret,
                paper=settings.paper_trading,
            )

    def get_bars(self) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=self.settings.timeframe_minutes * self.settings.lookback_bars * 2)
        request = CryptoBarsRequest(
            symbol_or_symbols=[self.settings.symbol],
            timeframe=TimeFrame(self.settings.timeframe_minutes, TimeFrameUnit.Minute),
            start=start,
            end=end,
            limit=self.settings.lookback_bars,
        )
        response = self.data.get_crypto_bars(request)
        frame = response.df
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(self.settings.symbol)
        return frame[["open", "high", "low", "close", "volume"]].tail(self.settings.lookback_bars)

    def account_equity(self) -> float:
        if self.trading is None:
            return self.settings.strategy_equity_usd
        return float(self.trading.get_account().equity)

    def has_position(self) -> bool:
        if self.trading is None:
            return False
        normalized = self.settings.symbol.replace("/", "")
        return any(position.symbol.replace("/", "") == normalized for position in self.trading.get_all_positions())

    def buy_notional(self, notional_usd: float) -> str:
        if self.settings.dry_run:
            return f"dry-run-buy-{notional_usd:.2f}"
        if self.trading is None:
            raise RuntimeError("Trading credentials are required to submit an order")
        order = MarketOrderRequest(
            symbol=self.settings.symbol,
            notional=notional_usd,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        return str(self.trading.submit_order(order_data=order).id)

    def close_position(self) -> str:
        if self.settings.dry_run:
            return "dry-run-close"
        if self.trading is None:
            raise RuntimeError("Trading credentials are required to close a position")
        normalized = self.settings.symbol.replace("/", "")
        return str(self.trading.close_position(normalized).id)
