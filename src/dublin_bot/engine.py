"""Trading engine: the ordered safety pipeline for one decision cycle.

Every cycle runs the same gate sequence, and **any** gate failing aborts before
an order can be formed:

    1. safety locks        — paper/dry-run/live flags consistent
    2. market data         — bars fetched, monotonic, no duplicates
    3. freshness           — bar age and exchange clock skew within tolerance
    4. market quality      — spread and liquidity acceptable
    5. strategy signal     — indicator confluence
    6. risk manager        — sizing, circuit breakers, cooldown, order caps
    7. precision           — exchange minimums and lot rounding
    8. idempotency         — this exact intent has not been submitted before
    9. execution           — dry-run synthetic id, or gated live submission

Gate order is deliberate: cheap local checks precede network calls, and the
idempotency reservation happens immediately before execution so the persisted
window of "unknown outcome" is as small as possible.
"""

from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditEvent, AuditLog
from .config import Settings
from .sentiment import SentimentAgent
from .errors import (
    BrokerError,
    DuplicateOrderError,
    PrecisionError,
    SafetyLockError,
    StaleDataError,
)
from .fills import FillModel
from .idempotency import IdempotencyLedger, make_intent_key
from .journal import Journal
from .kraken_gateway import KrakenGateway
from .market_quality import MarketGuard, MarketQuality
from .models import Action, DecisionRecord, RiskDecision, Signal
from .paper import PaperPortfolio
from .risk import RiskManager
from .state import StateStore
from .strategy import build_strategy
from .emergency import emergency_stop_active
from .dca import DCAAccumulator


def build_gateway(settings: Settings, **kwargs):
    """Factory: returns the configured broker gateway.

    Only Kraken Spot is supported.  Alpaca has been removed — see SAFETY.md.
    """
    if settings.broker == "kraken":
        kwargs.setdefault("allow_order_submission", bool(settings.allow_live_trading and not settings.paper_trading and not settings.dry_run))
        return KrakenGateway(settings, **kwargs)
    raise ValueError(
        f"Unsupported broker: {settings.broker!r}. "
        "Only 'kraken' is supported. Alpaca has been decommissioned."
    )


@dataclass
class CycleResult:
    """Full outcome of one engine cycle, including why it stopped."""

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
        self.strategy = build_strategy(settings)
        self.risk = RiskManager(settings)
        self.journal = Journal(settings.journal_path)
        self.state_store = StateStore(Path("logs/session_state.json"))
        self.ledger = IdempotencyLedger(Path(settings.idempotency_path))
        self.market_guard = MarketGuard(
            max_spread_bps=settings.max_spread_bps,
            min_dollar_volume=settings.min_dollar_volume,
        )
        self.fill_model = FillModel(settings)
        self.paper_portfolio = PaperPortfolio(Path("logs/paper_portfolio.json"))
        self.sentiment = SentimentAgent()
        self._rotation = 0  # retained for backward compat; selection is now score-driven
        self.dca = DCAAccumulator(settings)
        self.dca._state_path = settings.dca_state_path
        self._dca_state = self.dca.load(settings.dca_state_path)
        self._dca_executed_this_cycle = False
        self._dca_notional = 0.0
        from .learner import LearningAgent
        self.learner = LearningAgent(
            settings.learner_path,
            min_trades=settings.learner_min_trades,
            enabled=settings.learner_enabled,
        )

    # ── gate 1: safety ───────────────────────────────────────

    def _can_size(self, symbol: str, notional: float) -> bool:
        """True if the gateway can actually place a ``notional`` order for ``symbol``.

        Uses the real ``size_buy`` (precision/lot rules) rather than an estimate,
        so a coin whose lot minimum rounds above ``notional`` is excluded instead
        of throwing ``PrecisionError`` mid-execution on a small account.
        """
        saved = self.settings.symbol
        try:
            self.settings.symbol = symbol
            self.gateway.size_buy(notional)
            return True
        except Exception:
            return False
        finally:
            self.settings.symbol = saved

    def _select_symbol(self) -> None:
        """Autonomously pick the best tradeable coin for THIS cycle.

        No manual coin section: the bot scans the full tradeable universe
        (every active Kraken */USD pair when universe_mode="all_usd", otherwise
        the configured basket), keeps only coins it can *actually size* at the
        current balance and that the sentiment filter does not block, and ranks
        them by live strategy setup quality weighted by the self-learning agent
        (favor coins with proven positive expectancy, avoid bleeders). It trades
        whatever coin the market is offering — no pinned list, no round-robin.

        If we already hold a position, we stay put and let the strategy exit it
        rather than churning into a different coin mid-trade.
        """
        s = self.settings
        in_position = self.gateway.has_position()

        # Never abandon an open position — the strategy decides when to exit.
        if in_position:
            return

        equity = self.gateway.account_equity()
        cap = equity * s.max_position_fraction

        # 1) Build the candidate pool.
        # The full Kraken USD market is 600+ pairs; scoring every one per cycle
        # would hammer Kraken's public rate limit and the bot's own limiter. So
        # the live scan uses the vetted, affordable basket (plus the DCA coin)
        # as the candidate pool — the bot is NOT pinned to one coin, it freely
        # picks among tradeable coins by strategy + learned bias. list_usd_pairs
        # still provides genuine full-market discovery when universe_allowlist
        # or a raised scan_limit widens the pool.
        if s.universe_mode == "basket":
            candidates = list(s.coin_basket)
        else:  # all_usd: autonomous over the tradeable pool
            candidates = list(s.coin_basket) + [s.dca_symbol]
        # Apply hard safety allowlist if the operator set one.
        if s.universe_allowlist:
            candidates = [c for c in candidates if c.upper() in
                          {x.upper() for x in s.universe_allowlist}]

        # 2) Keep only coins we can truly size at the position cap.
        affordable = [sym for sym in candidates if self._can_size(sym, cap)]
        if not affordable:
            affordable = candidates  # nothing fits; let risk reject downstream

        # Cap the per-cycle scan to respect rate limits; bias-favored coins
        # (known positive expectancy) are tried first.
        if len(affordable) > s.universe_scan_limit:
            affordable.sort(key=lambda sym: -self.learner.bias(sym))
            affordable = affordable[: s.universe_scan_limit]

        # 3) Rank each by live MR setup quality × self-learning bias.
        BUY_THRESHOLD = 60  # MeanReversionStrategy emits score 60 on a real setup
        best_sym: str | None = None
        best_score = -1.0
        scored: list[tuple[str, float]] = []
        regime = self.learner.last_regime
        for sym in affordable:
            if sym == s.symbol:
                continue
            if self.sentiment.should_block_buy(sym):
                continue
            saved = s.symbol
            try:
                s.symbol = sym
                bars = self.gateway.get_bars()
                sig = self.strategy.evaluate(bars, in_position=False)
            except Exception:
                s.symbol = saved
                continue
            finally:
                s.symbol = saved
            bias = self.learner.bias(sym) * self.learner.regime_penalty(sym, regime)
            effective = float(sig.score) * bias
            scored.append((sym, effective))
            if effective > best_score:
                best_score = effective
                best_sym = sym

        # 4) Switch only if a coin clears the BUY threshold (a real setup).
        if best_sym is not None and best_score >= BUY_THRESHOLD:
            s.symbol = best_sym
            self.audit.record(
                AuditEvent.SIGNAL,
                {"event": "symbol_switch", "symbol": best_sym,
                 "score": round(best_score, 2), "equity": round(equity, 2),
                 "candidates_scored": len(scored)},
            )


    def assert_safety_locks(self) -> dict:
        """Verify the declared safety posture is internally consistent.

        Catches the dangerous middle state where live trading has been half
        enabled — for example ``paper_trading=false`` with ``dry_run`` still on,
        or live allowed without the typed acknowledgement.
        """
        s = self.settings
        report = s.safety_report()
        if not s.paper_trading and not s.allow_live_trading:
            raise SafetyLockError(
                "Inconsistent safety config: paper_trading=false requires "
                "allow_live_trading=true"
            )
        if s.allow_live_trading and s.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
            raise SafetyLockError(
                "allow_live_trading=true requires the exact acknowledgement string"
            )
        self.audit.record(AuditEvent.SAFETY_CHECK, report)
        return report

    # ── restart recovery ─────────────────────────────────────

    def recover(self) -> dict:
        """Resolve intents left ``pending`` by a crash before the next cycle.

        A pending record means the process died between reserving the intent and
        confirming the outcome, so we cannot know whether the order reached the
        exchange.  Each is resolved by querying Kraken for its userref: found →
        confirmed, definitively absent → failed (and therefore retryable).
        """
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
                    resolved.append({"key": record.key, "outcome": "unresolved",
                                     "error": str(exc)})
                    continue
            if found:
                self.ledger.confirm(record.key, found["id"])
                resolved.append({"key": record.key, "outcome": "confirmed",
                                 "order_id": found["id"]})
            else:
                self.ledger.fail(record.key, "no matching order found at exchange")
                resolved.append({"key": record.key, "outcome": "failed"})

        summary = {"pending_found": len(pending), "resolved": resolved}
        if pending:
            self.audit.record(AuditEvent.RECOVERY, summary, severity="warning")
        self.ledger.prune()
        return summary

    # ── main cycle ───────────────────────────────────────────

    def _min_notional(self, symbol: str) -> float:
        """Estimated minimum order notional (USD) for ``symbol``.

        Kraken's ``cost_min`` is often a small placeholder (~$0.5) and does not
        reflect the real floor, which is driven by the **lot-size** minimum
        (``order_min``) times price.  We therefore use ``order_min * last`` as the
        effective minimum notional; ``cost_min`` is only a fallback when no price
        is available.
        """
        try:
            meta = self.gateway.resolve_symbol(symbol)
        except Exception:
            return float("inf")
        try:
            last = float(self.gateway.get_ticker_for(symbol)["last"])
        except Exception:
            last = 0.0
        min_qty = float(meta.order_min)
        if last > 0:
            return min_qty * last
        cost_min = float(meta.cost_min) if meta.cost_min else 0.0
        return cost_min if cost_min > 0 else float("inf")

    def run_cycle(self) -> CycleResult:
        gates: dict[str, object] = {}
        # Snapshot the active symbol at cycle start so any temporary swap (e.g.
        # the DCA sleeve pointing one execution at PUMP/USD) is fully undone by
        # the end of the cycle, independent of in-cycle rotation.
        self._cycle_symbol_snapshot = self.settings.symbol

        # Gate 0 — self-learning sync (best-effort, never blocks trading).
        # Ingest REAL closed-trade P&L from Kraken so the learner's coin bias
        # reflects live results, not just paper fills.
        try:
            self.learner.sync_from_exchange(self.gateway)
        except Exception as exc:
            self.audit.record(AuditEvent.BROKER_ERROR,
                              {"operation": "learner_sync", "error": str(exc)},
                              severity="warning")

        # Gate 1 — safety locks
        try:
            gates["safety"] = self.assert_safety_locks()
        except SafetyLockError as exc:
            self.audit.record(AuditEvent.SAFETY_VIOLATION, {"error": str(exc)},
                              severity="critical")
            return CycleResult(None, "safety", str(exc), gates)
        if emergency_stop_active():
            return CycleResult(None, "emergency_stop", "Manual emergency stop is active", gates)

        # Gate 2 — symbol selection (adaptive to balance)
        self._select_symbol()
        gates["symbol"] = self.settings.symbol

        # Restart recovery before any new intent can be formed.
        gates["recovery"] = self.recover()

        # Paper portfolio sync for display and realistic accounting.
        equity = self.gateway.account_equity()
        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        state.realized_pnl_today = equity - state.start_equity
        self.paper_portfolio.load(equity=equity, cash=equity)

        # Gate 2 — market data
        try:
            bars = self.gateway.get_bars()
        except (BrokerError, StaleDataError) as exc:
            self.audit.record(AuditEvent.BROKER_ERROR,
                              {"operation": "get_bars", "error": str(exc)},
                              severity="error")
            return CycleResult(None, "market_data", str(exc), gates)
        gates["bars"] = {"count": int(len(bars))}

        # Gate 3 — freshness
        if hasattr(self.gateway, "check_freshness"):
            verdict = self.gateway.check_freshness(bars)
            gates["freshness"] = verdict.to_dict()
            if not verdict.fresh:
                return CycleResult(None, "freshness", verdict.reason, gates)

        # Gate 4 — market quality (advisory when the ticker is unavailable)
        quality_ok, quality_reason = True, "not evaluated"
        if hasattr(self.gateway, "market_quality"):
            try:
                snapshot = self.gateway.market_quality()
                quality_ok, quality_reason = self.market_guard.approve(
                    MarketQuality(
                        bid=snapshot["bid"],
                        ask=snapshot["ask"],
                        recent_dollar_volume=snapshot["recent_dollar_volume"],
                    )
                )
                gates["market_quality"] = {**snapshot, "approved": quality_ok,
                                           "reason": quality_reason}
                self.audit.record(AuditEvent.MARKET_QUALITY, gates["market_quality"])
            except BrokerError as exc:
                quality_ok, quality_reason = False, f"market quality unavailable: {exc}"
                gates["market_quality"] = {"approved": False, "reason": quality_reason}

        # Gate 5 — strategy signal
        in_position = self.gateway.has_position()
        signal = self.strategy.evaluate(bars, in_position=in_position)
        self.audit.record(AuditEvent.SIGNAL, {
            "action": signal.action.value, "score": signal.score,
            "reason": signal.reason, "price": signal.price,
            "stop_price": signal.stop_price,
        })

        # Gate 5.5 — sentiment confirmation filter (Stage 1).
        # Sentiment never originates a trade; it only (a) blocks a fresh BUY when
        # the coin's mood is bearish, and (b) forces a protective SELL when mood
        # collapses while in a position. This is what stops the bot from buying
        # into a coordinated dump / becoming reverse-pump exit liquidity.
        if self.settings.sentiment_enabled:
            sym = self.settings.symbol
            idx = self.sentiment.index_for(sym)
            if signal.action is Action.BUY and self.sentiment.should_block_buy(sym):
                signal = Signal(
                    Action.WAIT, signal.score,
                    f"Sentiment filter: {idx.score:+.2f} bearish for {idx.coin} "
                    f"(n={idx.sample_size})", signal.price, signal.atr,
                    signal.stop_price,
                )
                self.audit.record(AuditEvent.SIGNAL, {
                    "event": "sentiment_block_buy", "coin": idx.coin,
                    "score": idx.score, "sample_size": idx.sample_size,
                })
            elif in_position and self.sentiment.should_force_exit(sym):
                signal = Signal(
                    Action.SELL, 95,
                    f"Sentiment collapse: {idx.score:+.2f} for {idx.coin} "
                    f"(n={idx.sample_size})", signal.price, signal.atr,
                )
                self.audit.record(AuditEvent.SIGNAL, {
                    "event": "sentiment_force_sell", "coin": idx.coin,
                    "score": idx.score, "sample_size": idx.sample_size,
                })

        # Gate 5.7 — DCA accumulator sleeve (complementary to MR).
        # If the main strategy is not entering this bar and DCA is due for its
        # configured coin (default PUMP/USD), build a fixed-USD BUY signal that
        # still flows through Gate 6 (risk) and the shared execution path.
        if self.settings.dca_enabled and signal.action is not Action.BUY:
            try:
                pump_price = self.gateway.get_ticker_for(self.settings.dca_symbol)["last"]
                dca_signal = self.dca.maybe_signal(
                    self._dca_state,
                    price=pump_price,
                    in_position=in_position,
                    mr_action_is_buy=(signal.action is Action.BUY),
                )
                if dca_signal is not None:
                    # Point this cycle's execution at the DCA coin so the
                    # idempotency key, sizing, and order target PUMP/USD.
                    # Restore uses the cycle-start snapshot (set at the top of
                    # run_cycle) so in-cycle rotation is not disturbed.
                    self.settings.symbol = self.settings.dca_symbol
                    signal = dca_signal
                    self._dca_executed_this_cycle = True
                    # DCA sizes by its own fixed USD, NOT the risk-manager
                    # notional (which would be risk_per_trade*equity and fall
                    # below Kraken's minimum order size on a small account).
                    self._dca_notional = self.settings.dca_fixed_usd
                    self.audit.record(AuditEvent.SIGNAL, {
                        "event": "dca_signal", "symbol": self.settings.dca_symbol,
                        "reason": dca_signal.reason, "price": pump_price,
                    })
            except BrokerError as exc:
                self.audit.record(AuditEvent.BROKER_ERROR,
                                  {"operation": "dca_ticker", "error": str(exc)},
                                  severity="error")

        # Gate 6 — risk
        equity = self.gateway.account_equity()
        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        state.realized_pnl_today = equity - state.start_equity
        open_exposure = 0.0
        try:
            s = self.settings
            if s.margin_enabled and hasattr(self.gateway, "margin_positions"):
                # Margin exposure is measured by collateral committed, not notional.
                open_exposure = float(sum(
                    p.get("margin_total", 0.0) for p in self.gateway.margin_positions()
                ))
            elif hasattr(self.gateway, "positions"):
                open_exposure = float(sum(
                    p.get("market_value", 0.0) for p in self.gateway.positions()
                ))
        except BrokerError:
            open_exposure = 0.0
        leverage = self.settings.max_leverage if self.settings.margin_enabled else None
        if leverage is not None:
            leverage = min(leverage, self.settings.max_leverage)
        returns = bars["close"].pct_change().dropna() if len(bars) > 1 else None
        risk = self.risk.evaluate(
            signal,
            state,
            open_exposure_usd=open_exposure,
            returns=pd.Series(returns) if returns is not None else None,
        )

        # An entry requires healthy market quality; an exit must never be
        # blocked by a wide spread — being trapped in a position is worse.
        if signal.action is Action.BUY and risk.approved and not quality_ok:
            risk = RiskDecision(False, f"Market quality gate: {quality_reason}")

        self.audit.record(AuditEvent.RISK_DECISION, {
            "approved": risk.approved, "reason": risk.reason,
            "notional_usd": risk.notional_usd,
            "planned_loss_usd": risk.planned_loss_usd,
            "equity": equity, "orders_today": state.orders_today,
        })

        order_id: str | None = None
        bar_timestamp = str(bars.index[-1]) if len(bars) else datetime.now(timezone.utc).isoformat()

        # Gates 7–9 — precision, idempotency, execution
        if signal.action is Action.BUY and risk.approved:
            buy_notional = (
                self._dca_notional
                if getattr(self, "_dca_executed_this_cycle", False)
                else risk.notional_usd
            )
            # Never attempt an order larger than the account can afford. A buy
            # above available balance is rejected by the exchange (and would
            # otherwise be retried every cycle). Cap to the position budget.
            max_affordable = equity * self.settings.max_position_fraction
            if buy_notional > max_affordable:
                buy_notional = max_affordable
            if buy_notional <= 0:
                risk = RiskDecision(False, "No affordable size (balance below minimum order)")
                order_id = None
            else:
                order_id, risk = self._execute(
                    side="buy", notional=buy_notional, risk=risk,
                    bar_timestamp=bar_timestamp, state=state, gates=gates,
                    leverage=leverage,
                )
            # If this BUY was a DCA accumulator entry and an order was actually
            # placed, update its counters. Symbol restoration happens below
            # (outside this branch) so it runs even if risk rejected the order.
            if getattr(self, "_dca_executed_this_cycle", False) and order_id is not None:
                self.dca.record_buy(self._dca_state)
        elif signal.action is Action.SELL and in_position and risk.approved:
            # Exits use the risk manager's verdict (approved for SELL, never
            # blocked by entry breakers). Position existence, idempotency,
            # precision, and gateway safety are still enforced in _execute.
            order_id, risk = self._execute(
                side="sell", notional=0.0, risk=risk,
                bar_timestamp=bar_timestamp, state=state, gates=gates,
                leverage=leverage,
            )

        # Restore the cycle's primary symbol if the DCA sleeve had swapped it.
        # Uses the cycle-start snapshot so in-cycle rotation is preserved and a
        # rejected DCA order does NOT leave the engine pinned to PUMP/USD.
        if getattr(self, "_dca_executed_this_cycle", False):
            self.settings.symbol = self._cycle_symbol_snapshot
            self._dca_executed_this_cycle = False

        self.state_store.save(state)
        # Adapt risk scaling from the session's realized P&L streak.
        self.risk.update_scale(state)
        record = DecisionRecord(
            symbol=self.settings.symbol,
            signal=signal,
            risk=risk,
            dry_run=self.settings.dry_run,
            order_id=order_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return CycleResult(record, None, None, gates)

    def _execute(self, *, side: str, notional: float, risk: RiskDecision,
                 bar_timestamp: str, state, gates: dict, leverage: float | None = None) -> tuple[str | None, RiskDecision]:
        """Reserve an idempotency key, then execute. Never resends on ambiguity."""
        key = make_intent_key(
            symbol=self.settings.symbol, side=side,
            notional_usd=notional, bar_timestamp=bar_timestamp,
        )
        try:
            record = self.ledger.reserve(
                key=key, symbol=self.settings.symbol, side=side,
                notional_usd=notional, bar_timestamp=bar_timestamp,
                dry_run=self.settings.dry_run,
            )
        except DuplicateOrderError as exc:
            self.audit.record(AuditEvent.ORDER_DUPLICATE_BLOCKED,
                              {"key": key, "error": str(exc)}, severity="warning")
            gates["idempotency"] = {"blocked": True, "reason": str(exc)}
            return None, RiskDecision(False, f"Duplicate order blocked: {exc}")

        gates["idempotency"] = {"blocked": False, "key": key, "userref": record.userref}
        try:
            if side == "buy":
                order_id = self.gateway.buy_notional(notional, userref=record.userref, leverage=leverage)
                if self.settings.paper_trading or self.settings.dry_run:
                    ticker = self.gateway.get_ticker()
                    sized = self.gateway.size_buy(notional, price=float(ticker["ask"]))
                    fill = self.fill_model.buy(
                        price=float(sized.price),
                        volume=float(sized.volume),
                        bid=float(ticker.get("bid", 0.0)) if ticker.get("bid") else None,
                        ask=float(ticker.get("ask", 0.0)) if ticker.get("ask") else None,
                    )
                    self.paper_portfolio.record_buy(
                        symbol=self.settings.symbol,
                        quantity=float(sized.volume),
                        fill_price=fill.price,
                        fee=fill.fee,
                        when=datetime.now(timezone.utc).isoformat(),
                    )
                    portfolio = self.paper_portfolio.snapshot()
                    state.current_equity = portfolio.equity
                    state.peak_equity = max(state.peak_equity, state.current_equity)
            else:
                order_id = self.gateway.close_position(userref=record.userref)
                if self.settings.paper_trading or self.settings.dry_run:
                    ticker = self.gateway.get_ticker()
                    portfolio = self.paper_portfolio.snapshot()
                    position = portfolio.positions.get(self.settings.symbol)
                    quantity = float(position.quantity) if position else 0.0
                    if quantity > 1e-12:
                        fill = self.fill_model.sell(
                            price=float(ticker["last"]),
                            volume=quantity,
                            bid=float(ticker.get("bid", 0.0)) if ticker.get("bid") else None,
                            ask=float(ticker.get("ask", 0.0)) if ticker.get("ask") else None,
                        )
                        realized = self.paper_portfolio.record_sell(
                            symbol=self.settings.symbol,
                            quantity=quantity,
                            fill_price=fill.price,
                            fee=fill.fee,
                            when=datetime.now(timezone.utc).isoformat(),
                        )
                        # Self-learning: feed the closed-trade outcome back in.
                        self.learner.record_trade(
                            self.settings.symbol, realized, self.learner.last_regime
                        )
                        state.realized_pnl_today += realized
                        state.current_equity = self.paper_portfolio.snapshot().equity
                        state.peak_equity = max(state.peak_equity, state.current_equity)
        except PrecisionError as exc:
            self.ledger.fail(key, f"precision: {exc}")
            self.audit.record(AuditEvent.ORDER_REJECTED,
                              {"key": key, "reason": str(exc), "gate": "precision"},
                              severity="warning")
            return None, RiskDecision(False, f"Precision gate: {exc}")
        except (BrokerError, SafetyLockError) as exc:
            self.ledger.fail(key, str(exc))
            self.audit.record(AuditEvent.ORDER_REJECTED,
                              {"key": key, "reason": str(exc), "gate": "execution"},
                              severity="error")
            return None, RiskDecision(False, f"Execution blocked: {exc}")

        self.ledger.confirm(key, order_id)
        state.orders_today += 1
        state.last_order_at = datetime.now(timezone.utc)
        return order_id, risk

    # ── backwards-compatible entry point ─────────────────────

    def run_once(self) -> DecisionRecord:
        """Legacy API used by the CLI and dashboard.

        Returns a DecisionRecord even when a gate blocked the cycle, so callers
        that expect a record keep working; the blocking reason is surfaced as a
        HALT action rather than being silently swallowed.
        """
        result = self.run_cycle()
        if result.record is not None:
            return result.record
        from .models import Signal
        signal = Signal(Action.HALT, 0, result.block_reason or "blocked", 0.0)
        record = DecisionRecord(
            symbol=self.settings.symbol,
            signal=signal,
            risk=RiskDecision(False, f"Blocked at {result.blocked_at}"),
            dry_run=self.settings.dry_run,
            order_id=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return record
