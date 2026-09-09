from __future__ import annotations

import time

from .audit import AuditEvent
from .errors import BrokerError
from .kraken_gateway import KrakenGateway
from .precision import round_volume


class ManagedKrakenGateway(KrakenGateway):
    """Kraken gateway with quantity-scoped exits and fail-closed live reads."""

    def account_equity(self) -> float:
        """Fail closed on private account-data errors whenever live execution is armed."""
        if not self.has_credentials:
            if self.settings.live_execution_armed:
                raise BrokerError("Live execution armed but Kraken credentials are unavailable")
            return self.settings.strategy_equity_usd
        try:
            result = self._private("TradeBalance", {"asset": "ZUSD"})
            value = float(result.get("eb", 0.0))
            if value <= 0 and self.settings.live_execution_armed:
                raise BrokerError("Kraken returned non-positive account equity in live mode")
            return value
        except BrokerError:
            if self.settings.live_execution_armed:
                raise
            return self.settings.strategy_equity_usd

    def available_base_quantity(self) -> float:
        """Read the configured base-asset balance directly.

        This intentionally bypasses the legacy positions() helper because that
        helper converts broker failures into an empty list. Live reconciliation
        needs the distinction between "zero balance" and "could not read balance".
        """
        if not self.has_credentials:
            if self.settings.live_execution_armed:
                raise BrokerError("Live execution armed but Kraken credentials are unavailable")
            return 0.0
        meta = self.resolve_symbol()
        balances = self.balances()
        return float(balances.get(meta.base, 0.0))

    def sell_quantity(self, quantity: float, *, userref: int | None = None) -> str:
        if quantity <= 0:
            raise BrokerError("Managed sell quantity must be positive")
        meta = self.resolve_symbol()
        volume = round_volume(quantity, meta.to_precision())
        if volume <= 0:
            raise BrokerError(f"Managed quantity {quantity} rounds to zero for {meta.key}")

        available = self.available_base_quantity()
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
