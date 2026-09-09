from __future__ import annotations

import time

from .audit import AuditEvent
from .errors import BrokerError
from .kraken_gateway import KrakenGateway
from .precision import round_volume


class ManagedKrakenGateway(KrakenGateway):
    """Kraken gateway with quantity-scoped exits for Dublin-managed exposure."""

    def sell_quantity(self, quantity: float, *, userref: int | None = None) -> str:
        if quantity <= 0:
            raise BrokerError("Managed sell quantity must be positive")
        meta = self.resolve_symbol()
        volume = round_volume(quantity, meta.to_precision())
        if volume <= 0:
            raise BrokerError(f"Managed quantity {quantity} rounds to zero for {meta.key}")

        # A managed quantity can never exceed the current account balance.
        positions = self.positions()
        available = float(positions[0]["quantity"]) if positions else 0.0
        if float(volume) > available:
            raise BrokerError(
                f"Managed quantity {volume} exceeds available Kraken balance {available}"
            )

        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {
                "mode": "dry_run",
                "pair": meta.key,
                "side": "sell",
                "volume": format(volume, "f"),
                "managed_exit": True,
                "userref": userref,
            })
            return f"kraken-dry-managed-sell-{userref or int(time.time())}"

        self._assert_can_submit()
        params = {
            "pair": meta.key,
            "type": "sell",
            "ordertype": "market",
            "volume": format(volume, "f"),
        }
        if userref is not None:
            params["userref"] = str(userref)
        result = self._private("AddOrder", params)
        order_id = (result.get("txid") or ["unknown"])[0]
        self._log(AuditEvent.ORDER_SUBMITTED, {
            "pair": meta.key,
            "side": "sell",
            "volume": format(volume, "f"),
            "managed_exit": True,
            "order_id": order_id,
            "userref": userref,
        }, severity="warning")
        return order_id
