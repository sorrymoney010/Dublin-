from __future__ import annotations

import json
from math import isfinite

from .errors import BrokerError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ManagedPosition:
    symbol: str
    quantity: float
    entry_price: float
    stop_price: float
    opened_at: str
    order_id: str


class ManagedPositionStore:
    """Persist only the exposure opened by Dublin itself.

    This prevents an exit signal from selling unrelated spot holdings already
    present in the Kraken account.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> ManagedPosition | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text())
            position = ManagedPosition(**raw)
            if not isinstance(position.symbol, str) or not position.symbol:
                raise ValueError("missing symbol")
            for name in ("quantity", "entry_price", "stop_price"):
                value = float(getattr(position, name))
                if not isfinite(value) or value <= 0:
                    raise ValueError("position values must be finite and positive")
                setattr(position, name, value)
            return position
        except (OSError, ValueError, TypeError) as exc:
            raise BrokerError("Managed position ledger is unreadable or invalid") from exc

    def save(self, position: ManagedPosition) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(position), sort_keys=True, indent=2))
        temp.replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def new(*, symbol: str, quantity: float, entry_price: float,
            stop_price: float, order_id: str) -> ManagedPosition:
        return ManagedPosition(
            symbol=symbol,
            quantity=float(quantity),
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            opened_at=datetime.now(timezone.utc).isoformat(),
            order_id=order_id,
        )
