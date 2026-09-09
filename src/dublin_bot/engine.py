"""Trading engine: ordered safety and execution pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from math import isfinite

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import BrokerError, DuplicateOrderError, PrecisionError, SafetyLockError, StaleDataError
from .idempotency import IdempotencyLedger, make_intent_key
from .journal import Journal
from .managed_gateway import ManagedKrakenGateway
from .managed_position import ManagedPositionStore
from .market_quality import MarketGuard, MarketQuality
from .models import Action, DecisionRecord, RiskDecision, Signal
from .protective import ProtectiveStop, StopMonitor
from .risk import RiskManager
from .state import StateStore
from .strategy import TrendBreakoutStrategy


def build_gateway(settings: Settings, **kwargs):
    """Build Kraken with live submission reachable only through explicit arming."""
    if settings.broker == "kraken":
        kwargs.setdefault("allow_order_submission", settings.live_execution_armed)
        return ManagedKrakenGateway(settings, **kwargs)
    raise ValueError(f"Unsupported broker: {settings.broker!r}; only Kraken is supported")


@dataclass
class CycleResult:
    record: DecisionRecord | None
    blocked_at: str | None = None
    block_reason: str | None = None
    gates: dict[str, object] = field(default_factory=dict)

    @property
    def executed(self) -> bool:
        return self.record is not None and self.record.order_id is not None

    def to_dict(self) -> dict:
        return {
            "blocked_at": self.blocked_at,
            "block_reason": self.block_reason,
            "gates": self.gates,
            "decision": self.record.to_dict() if self.record else None,
        }


class TradingEngine:
    def __init__(self, settings: Settings, gateway=None, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self.gateway = gateway or build_gateway(settings, audit=self.audit)
        self.strategy = TrendBreakoutStrategy(settings)
        self.risk = RiskManager(settings)
        self.journal = Journal(settings.journal_path)
        self.state_store = StateStore(Path("logs/session_state.json"))
        self.position_store = ManagedPositionStore(Path(settings.managed_position_path))
        self.ledger = IdempotencyLedger(Path(settings.idempotency_path))
        self.market_guard = MarketGuard(
            max_spread_bps=settings.max_spread_bps,
            min_dollar_volume=settings.min_dollar_volume,
        )

    def assert_safety_locks(self) -> dict:
        s = self.settings
        report = s.safety_report()
        if s.live_execution_armed and not s.live_ready:
            raise SafetyLockError("Live execution is armed but the full live-ready configuration is incomplete")
        if s.allow_live_trading and s.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
            raise SafetyLockError("allow_live_trading=true requires the exact acknowledgement string")
        self.audit.record(AuditEvent.SAFETY_CHECK, report)
        return report

    def recover(self) -> dict:
        pending = self.ledger.pending()
        resolved: list[dict] = []
        for record in pending:
            if record.dry_run:
                self.ledger.fail(record.key, "dry-run intent abandoned at restart")
                resolved.append({"key": record.key, "outcome": "failed_dry_run"})
                continue
            found = None
            if hasattr(self.gateway, "find_order_by_userref"):
                try:
                    found = self.gateway.find_order_by_userref(record.userref)
                except BrokerError as exc:
                    resolved.append({"key": record.key, "outcome": "unresolved", "error": str(exc)})
                    continue
            if found:
                self.ledger.confirm(record.key, found["id"])
                resolved.append({"key": record.key, "outcome": "confirmed", "order_id": found["id"]})
            else:
                self.ledger.fail(record.key, "no matching order found at exchange")
                resolved.append({"key": record.key, "outcome": "failed"})
        summary = {"pending_found": len(pending), "resolved": resolved}
        if pending:
            self.audit.record(AuditEvent.RECOVERY, summary, severity="warning")
        self.ledger.prune()
        return summary

    def _managed_position_status(self) -> tuple[bool, object | None]:
        managed = self.position_store.load()
        if managed is None:
            return False, None
        if managed.symbol.replace("/", "") != self.settings.symbol.replace("/", ""):
            return False, managed
        return managed.quantity > 0, managed

    def run_cycle(self) -> CycleResult:
        gates: dict[str, object] = {}
        try:
            gates["safety"] = self.assert_safety_locks()
        except SafetyLockError as exc:
            self.audit.record(AuditEvent.SAFETY_VIOLATION, {"error": str(exc)}, severity="critical")
            return CycleResult(None, "safety", str(exc), gates)

        gates["recovery"] = self.recover()

        try:
            in_position, managed = self._managed_position_status()
            if managed is not None and not in_position:
                raise BrokerError("Managed position does not match the configured symbol")
        except BrokerError as exc:
            return CycleResult(None, "reconciliation", str(exc), gates)

        # Reconciliation is authoritative for managed exposure. If Dublin thinks
        # it owns more than Kraken reports, live mode halts rather than risking an
        # invalid or oversized exit.
        available_quantity = 0.0
        try:
            if self.settings.paper_trading:
                # Simulated fills do not create an exchange balance.
                available_quantity = float(managed.quantity) if managed else 0.0
            elif hasattr(self.gateway, "available_base_quantity"):
                available_quantity = float(self.gateway.available_base_quantity())
            else:
                raise BrokerError("Live reconciliation requires an authoritative balance reader")
            if not isfinite(available_quantity) or available_quantity < 0:
                raise BrokerError("Invalid exchange base quantity")
        except (BrokerError, ValueError, TypeError, KeyError, IndexError) as exc:
            if not self.settings.paper_trading:
                return CycleResult(None, "reconciliation", f"Kraken position read failed: {exc}", gates)

        exchange_has_position = available_quantity > 0
        shortage = bool(in_position and managed is not None and available_quantity < float(managed.quantity))
        gates["position_reconciliation"] = {
            "managed_position": in_position,
            "managed_quantity": float(managed.quantity) if managed is not None else 0.0,
            "exchange_quantity": available_quantity,
            "exchange_base_balance_present": exchange_has_position,
            "orphaned_exchange_holding": bool(exchange_has_position and not in_position),
            "shortage": shortage,
        }
        if shortage:
            reason = (
                f"Managed quantity {managed.quantity} exceeds Kraken available quantity "
                f"{available_quantity}; entry/exit halted pending reconciliation"
            )
            self.audit.record(AuditEvent.SAFETY_VIOLATION, {"error": reason}, severity="critical")
            return CycleResult(None, "reconciliation", reason, gates)

        signal = None

        # Protective stop is evaluated from the latest ticker, independent of the
        # slower strategy exit. It can force an exit even if the candle-based COO
        # model still says HOLD.
        if in_position and managed is not None and float(managed.stop_price) > 0:
            try:
                last_price = float(self.gateway.get_ticker()["last"])
                triggered = StopMonitor.should_exit(last_price, ProtectiveStop(
                    managed.symbol, float(managed.stop_price), float(managed.quantity)
                ))
            except (BrokerError, ValueError, TypeError, KeyError, IndexError) as exc:
                return CycleResult(None, "protective_stop", f"Stop price check failed: {exc}", gates)
            gates["protective_stop"] = {
                "stop_price": float(managed.stop_price),
                "last_price": last_price,
                "triggered": triggered,
            }
            if triggered:
                signal = Signal(
                    Action.SELL,
                    100,
                    f"Protective stop triggered at {last_price:.8f} <= {managed.stop_price:.8f}",
                    last_price,
                    stop_price=float(managed.stop_price),
                )

        # Entry data gates must not delay an already-triggered protective exit.
        bar_timestamp = f"protective-stop:{managed.order_id}" if signal is not None else ""
        quality_ok, quality_reason = True, "protective exit"
        if signal is None:
            try:
                bars = self.gateway.get_bars()
            except (BrokerError, StaleDataError) as exc:
                self.audit.record(AuditEvent.BROKER_ERROR, {"operation": "get_bars", "error": str(exc)}, severity="error")
                return CycleResult(None, "market_data", str(exc), gates)
            gates["bars"] = {"count": int(len(bars))}

            if hasattr(self.gateway, "check_freshness"):
                try:
                    verdict = self.gateway.check_freshness(bars)
                except BrokerError as exc:
                    return CycleResult(None, "freshness", str(exc), gates)
                gates["freshness"] = verdict.to_dict()
                if not verdict.fresh:
                    return CycleResult(None, "freshness", verdict.reason, gates)

            quality_ok, quality_reason = True, "not evaluated"
            if hasattr(self.gateway, "market_quality"):
                try:
                    snapshot = self.gateway.market_quality()
                    quality_ok, quality_reason = self.market_guard.approve(MarketQuality(
                        bid=snapshot["bid"], ask=snapshot["ask"], recent_dollar_volume=snapshot["recent_dollar_volume"]
                    ))
                    gates["market_quality"] = {**snapshot, "approved": quality_ok, "reason": quality_reason}
                    self.audit.record(AuditEvent.MARKET_QUALITY, gates["market_quality"])
                except BrokerError as exc:
                    quality_ok, quality_reason = False, f"market quality unavailable: {exc}"
                    gates["market_quality"] = {"approved": False, "reason": quality_reason}
            signal = self.strategy.evaluate(bars, in_position=in_position)
            bar_timestamp = (str(bars.index[-1]) if len(bars)
                             else datetime.now(timezone.utc).isoformat())

        self.audit.record(AuditEvent.SIGNAL, {
            "action": signal.action.value, "score": signal.score, "reason": signal.reason,
            "price": signal.price, "stop_price": signal.stop_price,
        })

        try:
            account_equity = float(self.gateway.account_equity())
            if not isfinite(account_equity) or account_equity <= 0:
                raise BrokerError("Invalid account equity")
            equity = min(account_equity, self.settings.strategy_equity_usd)
        except (BrokerError, ValueError, TypeError, KeyError) as exc:
            self.audit.record(AuditEvent.BROKER_ERROR, {"operation": "account_equity", "error": str(exc)}, severity="critical")
            return CycleResult(None, "account_data", str(exc), gates)

        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        state.realized_pnl_today = equity - state.start_equity
        risk = self.risk.evaluate(signal, state)

        if signal.action is Action.BUY and risk.approved and not quality_ok:
            risk = RiskDecision(False, f"Market quality gate: {quality_reason}")
        if signal.action is Action.BUY and risk.approved and in_position:
            risk = RiskDecision(False, "Managed Dublin position already open")

        self.audit.record(AuditEvent.RISK_DECISION, {
            "approved": risk.approved, "reason": risk.reason,
            "notional_usd": risk.notional_usd, "planned_loss_usd": risk.planned_loss_usd,
            "equity": equity, "orders_today": state.orders_today,
        })

        order_id: str | None = None

        if signal.action is Action.BUY and risk.approved:
            order_id, risk = self._execute_buy(risk, signal, bar_timestamp, state, gates)
        elif signal.action is Action.SELL and in_position and managed is not None:
            order_id, risk = self._execute_managed_sell(managed, bar_timestamp, state, gates)

        self.state_store.save(state)
        record = DecisionRecord(
            symbol=self.settings.symbol, signal=signal, risk=risk,
            dry_run=self.settings.dry_run, order_id=order_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return CycleResult(record, None, None, gates)

    def _reserve(self, *, side: str, notional: float, bar_timestamp: str, gates: dict):
        key = make_intent_key(symbol=self.settings.symbol, side=side,
                              notional_usd=notional, bar_timestamp=bar_timestamp)
        try:
            record = self.ledger.reserve(
                key=key, symbol=self.settings.symbol, side=side, notional_usd=notional,
                bar_timestamp=bar_timestamp, dry_run=self.settings.dry_run,
            )
        except DuplicateOrderError as exc:
            self.audit.record(AuditEvent.ORDER_DUPLICATE_BLOCKED,
                              {"key": key, "error": str(exc)}, severity="warning")
            gates["idempotency"] = {"blocked": True, "reason": str(exc)}
            return None, None
        gates["idempotency"] = {"blocked": False, "key": key, "userref": record.userref}
        return key, record

    def _execute_buy(self, risk: RiskDecision, signal, bar_timestamp: str, state, gates):
        key, intent = self._reserve(side="buy", notional=risk.notional_usd,
                                    bar_timestamp=bar_timestamp, gates=gates)
        if key is None:
            return None, RiskDecision(False, "Duplicate order blocked")
        try:
            sized = self.gateway.size_buy(risk.notional_usd)
            order_id = self.gateway.buy_notional(risk.notional_usd, userref=intent.userref)
        except PrecisionError as exc:
            self.ledger.fail(key, f"precision: {exc}")
            return None, RiskDecision(False, f"Precision gate: {exc}")
        except (BrokerError, SafetyLockError) as exc:
            self.ledger.fail(key, str(exc))
            return None, RiskDecision(False, f"Execution blocked: {exc}")

        self.ledger.confirm(key, order_id)
        self.position_store.save(ManagedPositionStore.new(
            symbol=self.settings.symbol,
            quantity=float(sized.volume),
            entry_price=float(sized.price),
            stop_price=float(signal.stop_price or 0.0),
            order_id=order_id,
        ))
        state.orders_today += 1
        state.last_order_at = datetime.now(timezone.utc)
        return order_id, risk

    def _execute_managed_sell(self, managed, bar_timestamp: str, state, gates):
        notional_marker = round(float(managed.quantity) * max(float(managed.entry_price), 0.01), 2)
        key, intent = self._reserve(side="sell", notional=notional_marker,
                                    bar_timestamp=bar_timestamp, gates=gates)
        if key is None:
            return None, RiskDecision(False, "Duplicate exit blocked")
        try:
            order_id = self.gateway.sell_quantity(managed.quantity, userref=intent.userref)
        except (BrokerError, PrecisionError, SafetyLockError) as exc:
            self.ledger.fail(key, str(exc))
            return None, RiskDecision(False, f"Exit blocked: {exc}")
        self.ledger.confirm(key, order_id)
        self.position_store.clear()
        state.orders_today += 1
        state.last_order_at = datetime.now(timezone.utc)
        return order_id, RiskDecision(True, "Managed Dublin exit executed")

    def run_once(self) -> DecisionRecord:
        result = self.run_cycle()
        if result.record is not None:
            return result.record
        signal = Signal(Action.HALT, 0, result.block_reason or "blocked", 0.0)
        record = DecisionRecord(
            symbol=self.settings.symbol, signal=signal,
            risk=RiskDecision(False, f"Blocked at {result.blocked_at}"),
            dry_run=self.settings.dry_run, order_id=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return record
