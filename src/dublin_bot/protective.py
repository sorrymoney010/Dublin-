from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class ProtectiveStop:
    symbol: str
    stop_price: float
    quantity: float


class StopMonitor:
    """Pure stop evaluator designed to run outside the entry loop."""

    @staticmethod
    def should_exit(last_price: float, stop: ProtectiveStop) -> bool:
        if any(not isfinite(v) or v <= 0 for v in (last_price, stop.stop_price, stop.quantity)):
            raise ValueError("invalid stop monitoring values")
        return last_price <= stop.stop_price

