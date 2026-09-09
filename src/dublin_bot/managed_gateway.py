from __future__ import annotations

import time
from math import isfinite

from .audit import AuditEvent
from .errors import BrokerError
from .kraken_gateway import KrakenGateway
from .precision import round_volume


class ManagedKrakenGateway(KrakenGateway):
    """Kraken gateway with quantity-scoped exits and fail-closed live reads."""

    def account_equity(self) -> float:
        """Fail closed on private account-data errors in live mode."""
        if not self.has_credentials:
            if not self.settings.paper_trading:
                raise BrokerError("Live mode configured but Kraken credentials are unavailable")
            return self.settings.strategy_equity_usd
        try:
            result = self._private("TradeBalance", {"asset": "ZUSD"})
            value = float(result["eb"])
            if not isfinite(value) or value <= 0:
                raise BrokerError("Kraken returned invalid account equity")
            return value
        except (BrokerError, ValueError, TypeError, KeyError, AttributeError) as exc:
            if not self.settings.paper_trading:
                raise BrokerError("Kraken account equity unavailable or invalid") from exc
            return self.settings.strategy_equity_usd

    def available_base_quantity(self) -> float:
        """Read the configured base-asset balance directly for live reconciliation."""
        if not self.has_credentials:
            if not self.settings.paper_trading:
                raise BrokerError("Live mode configured but Kraken credentials are unavailable")
            return 0.0
        meta = self.resolve_symbol()
        try:
            balances = self.balances()
            quantity = float(balances.get(meta.base, 0.0))
            if not isfinite(quantity) or quantity < 0:
                raise ValueError("base quantity must be finite and nonnegative")
            return quantity
        except (ValueError, TypeError, AttributeError) as exc:
            raise BrokerError("Kraken balance data is invalid") from exc

    def sell_quantity(self, quantity: float, *, userref: int | None = None) -> str:
        if not isfinite(quantity) or quantity <= 0:
            raise BrokerError("Managed sell quantity must be positive")
        meta = self.resolve_symbol()
        volume = round_volume(quantity, meta.to_precision())
        if volume <= 0:
            raise BrokerError(f"Managed quantity {quantity} rounds to zero for {meta.key}")

        # Paper/dry-run exits are local simulations. They must not depend on a
        # real Kraken balance because no real entry was placed.
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

        # Live exits are quantity-scoped and fail closed if Kraken reports less
        # base asset than Dublin's managed position requires.
        available = self.available_base_quantity()
        if float(volume) > available:
            raise BrokerError(
                f"Managed quantity {volume} exceeds available Kraken balance {available}"
            )

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
