from __future__ import annotations

import json
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
            return ManagedPosition(**raw)
        except (OSError, ValueError, TypeError):
            return None

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
