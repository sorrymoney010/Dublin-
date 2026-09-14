"""
Multi-symbol market data adapter for the rotation strategy.

Wraps a KrakenGateway to fetch bars for multiple symbols by temporarily
reconfiguring the gateway's symbol resolution for each call.

The Kraken gateway's get_bars() uses settings.symbol internally, so we
need to switch symbols between calls to scan the full universe.
"""

from __future__ import annotations

from typing import Optional

from dublin_bot.kraken_gateway import KrakenGateway, SymbolMeta
from dublin_bot.config import Settings


class MultiSymbolGateway:
    """
    Wraps KrakenGateway to support multi-symbol bar fetching.
    
    Temporarily swaps the settings.symbol to fetch each coin's bars,
    then restores the original symbol.
    """

    def __init__(self, gateway: KrakenGateway, settings: Settings):
        self.gateway = gateway
        self.settings = settings
        self._original_symbol = settings.symbol

    def get_bars_for(self, symbol: str) -> Optional[object]:
        """
        Fetch OHLC bars for a specific symbol.
        
        Args:
            symbol: Trading symbol like "PUMP/USD" or "BTC/USD"
        
        Returns:
            pandas DataFrame with OHLCV data, or None on failure
        """
        original = self.settings.symbol
        try:
            self.settings.symbol = symbol
            # Force metadata reload for the new symbol
            self.gateway._meta = {}
            bars = self.gateway.get_bars(validate=False)
            return bars
        except Exception as e:
            # Log but don't crash - just return None for this symbol
            print(f"Warning: Failed to fetch bars for {symbol}: {e}")
            return None
        finally:
            # Always restore original symbol
            self.settings.symbol = original

    def get_bars_for_all(self, symbols: list[str]) -> dict[str, Optional[object]]:
        """
        Fetch bars for all symbols in the list.
        
        Args:
            symbols: List of trading symbols
        
        Returns:
            Dict mapping symbol -> DataFrame or None
        """
        result = {}
        for symbol in symbols:
            bars = self.get_bars_for(symbol)
            result[symbol] = bars
        return result

    def get_ticker_for(self, symbol: str) -> Optional[dict]:
        """
        Fetch ticker data for a specific symbol.
        
        Args:
            symbol: Trading symbol
        
        Returns:
            Ticker dict with bid/ask/last/volume, or None on failure
        """
        original = self.settings.symbol
        try:
            self.settings.symbol = symbol
            self.gateway._meta = {}
            ticker = self.gateway.get_ticker()
            return ticker
        except Exception as e:
            print(f"Warning: Failed to fetch ticker for {symbol}: {e}")
            return None
        finally:
            self.settings.symbol = original

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        """
        Get current prices for all symbols.
        
        Args:
            symbols: List of trading symbols
        
        Returns:
            Dict mapping symbol -> current price (float)
        """
        prices = {}
        for symbol in symbols:
            ticker = self.get_ticker_for(symbol)
            if ticker and "last" in ticker:
                prices[symbol] = float(ticker["last"])
            else:
                prices[symbol] = 0.0
        return prices
