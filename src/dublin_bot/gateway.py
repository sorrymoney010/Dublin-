from __future__ import annotations

from typing import Protocol

import pandas as pd

from .alpaca_gateway import AlpacaGateway
from .config import Settings


class TradingGateway(Protocol):
    def get_bars(self) -> pd.DataFrame: ...

    def account_equity(self) -> float: ...

    def has_position(self) -> bool: ...

    def buy_notional(self, notional_usd: float) -> str: ...

    def close_position(self) -> str: ...

    def diagnostic(self) -> dict[str, object]: ...


def build_gateway(settings: Settings) -> TradingGateway:
    if settings.exchange == "kraken":
        from .kraken_gateway import KrakenGateway

        return KrakenGateway(settings)
    return AlpacaGateway(settings)
