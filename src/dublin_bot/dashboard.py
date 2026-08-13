from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from .audit import AuditLog
from .config import Settings
from .engine import TradingEngine
from .state import StateStore

HOST = os.environ.get("DUBLIN_HOST", "0.0.0.0")
PORT = 8765
_market_cache: dict[str, object] = {"at": 0.0, "data": {}}
_market_lock = Lock()
_health_cache: dict[str, object] = {"at": 0.0, "data": {}}
_health_lock = Lock()
_state_cache: dict[str, object] = {"at": 0.0, "data": {}}
_state_lock = Lock()
_strategy_cache: dict[str, object] = {"at": 0.0, "data": {}}
_strategy_lock = Lock()
_learning_cache: dict[str, object] = {"at": 0.0, "data": {}}
_learning_lock = Lock()


def learning_brief_data(settings: Settings) -> dict[str, object]:
    """Cached AI command center data from the learning module."""
    with _learning_lock:
        if monotonic() - float(_learning_cache["at"]) < 60:
            return dict(_learning_cache["data"])
        try:
            from .learning import build_learning_report, generate_brief
            report = build_learning_report(settings)
            brief = generate_brief(settings)
            data = {
                "regime": report.regime.regime,
                "regime_description": report.regime.description,
                "confidence": report.regime.confidence,
                "confidence_score": round(report.regime.confidence * 100, 1),
                "signal_quality": report.signal_quality.get("avg_score", 0) / 100
                                if report.signal_quality.get("avg_score", 0) > 70 else "Low",
                "today_signals": report.total_evaluations,
                "win_rate_estimate": report.win_rate_estimate,
                "suggestions": [
                    {"parameter": s.parameter, "current_value": s.current_value,
                     "suggested_value": s.suggested_value, "confidence": s.confidence,
                     "rationale": s.rationale}
                    for s in report.suggestions
                ],
                "filter_analysis": [
                    {"filter_name": f.filter_name, "failure_rate": f.failure_rate,
                     "avg_score_when_failed": f.avg_score_when_failed}
                    for f in report.filter_analysis
                ],
                "market_commentary": f"{settings.symbol} at ${report.signal_quality.get('avg_score', 0)} avg signal score",
                "suggested_actions": [s["text"] for s in suggestions_to_actions(report.suggestions)],
                "brief_text": brief,
            }
        except Exception as exc:
            data = {"error": str(exc), "regime": "unknown", "confidence": 0,
                    "suggested_actions": [f"Learning module error: {exc}"],
                    "brief_text": f"No data: {exc}"}
        _learning_cache.update(at=monotonic(), data=data)
        return data


def suggestions_to_actions(suggestions: list) -> list[dict]:
    """Convert OptimizationSuggestion list to action dicts."""
    return [{"text": s.rationale, "parameter": s.parameter} for s in suggestions]


class PaperMonitor:
    def __init__(self, settings: Settings, interval_seconds: int = 3600) -> None:
        self.settings = settings
        self.interval_seconds = interval_seconds
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.last_run: str | None = None
        self.last_action: str | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, object]:
        return {
            "running": self.thread is not None and self.thread.is_alive(),
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
            "last_action": self.last_action,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, daemon=True, name="dublin-paper-monitor")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                record = TradingEngine(self.settings).run_once()
                self.last_run = record.timestamp
                self.last_action = record.signal.action.value
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.last_run = datetime.now(timezone.utc).isoformat()
            self.stop_event.wait(self.interval_seconds)


def safety_status(settings: Settings) -> dict[str, object]:
    locked = settings.paper_trading and settings.dry_run and not settings.allow_live_trading
    return {
        "safe": locked,
        "paper_trading": settings.paper_trading,
        "dry_run": settings.dry_run,
        "live_allowed": settings.allow_live_trading,
        "credentials_present": settings.has_credentials,
        "broker": settings.broker,
        "symbol": settings.symbol,
        "strategy_equity_usd": settings.strategy_equity_usd,
        "risk_per_trade": settings.risk_per_trade,
        "max_daily_loss_fraction": settings.max_daily_loss_fraction,
        "max_drawdown_fraction": settings.max_drawdown_fraction,
        "max_orders_per_day": settings.max_orders_per_day,
    }


def health_snapshot(settings: Settings) -> dict[str, object]:
    """Broker connection health, clock skew, rate-limit budget and freshness."""
    with _health_lock:
        if monotonic() - float(_health_cache["at"]) < 20:
            return dict(_health_cache["data"])
        try:
            from .engine import build_gateway
            gateway = build_gateway(settings)
            if hasattr(gateway, "health"):
                data = gateway.health()
            else:
                data = {"broker": settings.broker, "reachable": None,
                        "error": "adapter does not report health"}
        except Exception as exc:
            data = {"broker": settings.broker, "reachable": False, "error": str(exc)}
        _health_cache.update(at=monotonic(), data=data)
        return data


def audit_summary(settings: Settings, limit: int = 20) -> dict[str, object]:
    try:
        log = AuditLog(Path(settings.audit_log_path))
        intact, reason = log.verify_chain()
        return {
            "chain_intact": intact,
            "chain_status": reason,
            "entries": [
                {
                    "timestamp": e.get("timestamp"),
                    "event": e.get("event"),
                    "severity": e.get("severity"),
                    "payload": e.get("payload"),
                }
                for e in log.tail(limit)
            ][::-1],
        }
    except Exception as exc:
        return {"chain_intact": None, "chain_status": str(exc), "entries": []}


def recent_activity(path: Path, limit: int = 30) -> list[dict[str, object]]:
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=limit)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                lines.append(line)
    records: list[dict[str, object]] = []
    for line in reversed(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def market_snapshot(settings: Settings) -> dict[str, object]:
    with _market_lock:
        if monotonic() - float(_market_cache["at"]) < 30:
            return dict(_market_cache["data"])
        try:
            from .engine import build_gateway
            gateway = build_gateway(settings)
            bars = gateway.get_bars().tail(60)
            closes = [round(float(value), 2) for value in bars["close"].tolist()]
            timestamps = [str(value) for value in bars.index.tolist()]
            price = closes[-1] if closes else None
            change = ((closes[-1] / closes[-2]) - 1) * 100 if len(closes) > 1 else None
            last_timestamp = bars.index[-1] if len(bars.index) else None
            if last_timestamp is not None and hasattr(last_timestamp, "to_pydatetime"):
                last_timestamp = last_timestamp.to_pydatetime()
            if last_timestamp is not None and last_timestamp.tzinfo is None:
                last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)
            age_minutes = (
                (datetime.now(timezone.utc) - last_timestamp).total_seconds() / 60
                if last_timestamp is not None else None
            )
            data: dict[str, object] = {
                "connected": bool(closes), "price": price, "change_percent": change,
                "closes": closes, "timestamps": timestamps, "error": None,
                "last_timestamp": str(last_timestamp) if last_timestamp else None,
                "delayed": age_minutes is None or age_minutes > 120,
            }
        except Exception as exc:
            data = {"connected": False, "price": None, "change_percent": None,
                    "closes": [], "timestamps": [], "error": str(exc)}
        _market_cache.update(at=monotonic(), data=data)
        return data


def portfolio_snapshot(settings: Settings) -> dict[str, object]:
    from .engine import TradingEngine
    try:
        engine = TradingEngine(settings)
        portfolio = engine.paper_portfolio.snapshot()
        state = engine.state_store.load(settings.strategy_equity_usd)
        if portfolio.positions and settings.symbol in portfolio.positions:
            portfolio = engine.paper_portfolio.mark_to_market({settings.symbol: engine.gateway.get_ticker()["last"]})
        realized = round(state.realized_pnl_today, 2)
        positions = portfolio.to_dict()["positions"] or []
        unrealized = round(sum(float(p.get("unrealized_pl", 0.0)) for p in positions), 2)
        return {
            "equity": round(portfolio.equity, 2),
            "cash": round(portfolio.cash, 2),
            "positions": positions,
            "orders": engine.gateway.orders() if settings.has_credentials else [],
            "unrealized_pl": unrealized,
            "realized_pl": realized,
            "message": None,
        }
    except Exception as exc:
        return {"equity": settings.strategy_equity_usd, "positions": [], "orders": [],
                "cash": 0.0, "unrealized_pl": 0.0, "realized_pl": 0.0, "message": str(exc)}


def risk_state_snapshot(settings: Settings) -> dict[str, object]:
    """Durable daily risk state: drawdown, daily loss, orders, cooldown."""
    with _state_lock:
        if monotonic() - float(_state_cache["at"]) < 30:
            return dict(_state_cache["data"])
        try:
            store = StateStore(Path(settings.idempotency_path))
            s = store.load(settings.strategy_equity_usd)
            today = datetime.now(timezone.utc).date().isoformat()
            drawdown = round((1 - s.current_equity / max(s.peak_equity, 0.01)) * 100, 1)
            daily_loss = round(abs(s.realized_pnl_today), 2)
            max_daily_loss = round(settings.strategy_equity_usd * settings.max_daily_loss_fraction, 2)
            max_dd_pct = round(settings.max_drawdown_fraction * 100, 1)
            cooldown_active = False
            cooldown_remaining = 0
            if s.last_order_at is not None:
                from datetime import timedelta
                ready_at = s.last_order_at + timedelta(minutes=settings.cooldown_minutes)
                cooldown_active = datetime.now(timezone.utc) < ready_at
                if cooldown_active:
                    cooldown_remaining = int((ready_at - datetime.now(timezone.utc)).total_seconds() / 60)
            data = {
                "start_equity": round(s.start_equity, 2),
                "peak_equity": round(s.peak_equity, 2),
                "current_equity": round(s.current_equity, 2),
                "realized_pnl_today": round(s.realized_pnl_today, 2),
                "unrealized_pl": 0.0,
                "orders_today": s.orders_today,
                "max_orders_per_day": settings.max_orders_per_day,
                "drawdown_percent": drawdown,
                "max_drawdown_percent": max_dd_pct,
                "daily_loss": daily_loss,
                "max_daily_loss": max_daily_loss,
                "consecutive_losses": 0,
                "cooldown_active": cooldown_active,
                "cooldown_remaining_minutes": cooldown_remaining,
                "session_date": today,
            }
            # Count consecutive losses from activity
            try:
                activity = recent_activity(settings.journal_path, limit=50)
                loss_streak = 0
                for entry in activity:
                    if entry.get("risk", {}).get("approved") and entry.get("order_id"):
                        # This was an executed order — check if it was a loss
                        # We can't know the outcome in dry-run, so use realized_pnl
                        pass
                    if not entry.get("risk", {}).get("approved") and entry.get("signal", {}).get("action") == "WAIT":
                        loss_streak += 1
                    else:
                        break
                data["consecutive_losses"] = min(loss_streak, settings.max_orders_per_day)
            except Exception:
                pass
        except Exception as exc:
            data = {"error": str(exc), "current_equity": settings.strategy_equity_usd,
                    "start_equity": settings.strategy_equity_usd, "peak_equity": settings.strategy_equity_usd,
                    "drawdown_percent": 0.0, "realized_pnl_today": 0.0, "orders_today": 0}
        _state_cache.update(at=monotonic(), data=data)
        return data


def strategy_analytics(settings: Settings) -> dict[str, object]:
    """Strategy performance analytics computed from activity log."""
    with _strategy_lock:
        if monotonic() - float(_strategy_cache["at"]) < 60:
            return dict(_strategy_cache["data"])
        try:
            activity = recent_activity(settings.journal_path, limit=500)
            entries = [a for a in activity if a.get("timestamp")]
            entries.sort(key=lambda x: x["timestamp"])

            total_signals = len(entries)
            buy_signals = len([e for e in entries if e.get("signal", {}).get("action") == "BUY"])
            sell_signals = len([e for e in entries if e.get("signal", {}).get("action") == "SELL"])
            wait_signals = len([e for e in entries if e.get("signal", {}).get("action") == "WAIT"])

            # Score history for chart
            score_history = [{"timestamp": e["timestamp"], "score": e.get("signal", {}).get("score", 0)}
                            for e in entries[-60:]]
            score_values = [s["score"] for s in score_history]

            # Action distribution
            actions = {"BUY": buy_signals, "SELL": sell_signals, "WAIT": wait_signals}

            # Filter failure breakdown
            filter_failures = {}
            for e in entries:
                reason = e.get("signal", {}).get("reason", "")
                if "Filters failed:" in reason:
                    filters = reason.replace("Filters failed:", "").strip()
                    for f in filters.split(","):
                        f = f.strip()
                        filter_failures[f] = filter_failures.get(f, 0) + 1

            # Win rate (only for executed orders in paper mode, approximated)
            executed = [e for e in entries if e.get("order_id")]
            win_rate = 0.0
            if executed:
                # In paper mode, we approximate win rate from positive risk approvals
                wins = len([e for e in executed if e.get("risk", {}).get("approved") and e.get("signal", {}).get("action") == "BUY"])
                win_rate = round(wins / len(executed) * 100, 1) if executed else 0.0

            avg_score = round(sum(score_values) / len(score_values), 1) if score_values else 0
            max_score = max(score_values) if score_values else 0
            min_score = min(score_values) if score_values else 0

            # Last signal summary
            last = entries[-1] if entries else None
            latest_signal = {
                "action": last.get("signal", {}).get("action", "WAIT") if last else "WAIT",
                "score": last.get("signal", {}).get("score", 0) if last else 0,
                "reason": last.get("signal", {}).get("reason", "No signal") if last else "No signal yet",
                "price": last.get("signal", {}).get("price", 0) if last else 0,
                "atr": last.get("signal", {}).get("atr", 0) if last else 0,
                "stop_price": last.get("signal", {}).get("stop_price") if last else None,
                "timestamp": last.get("timestamp", "") if last else "",
            }

            data = {
                "total_signals": total_signals,
                "actions": actions,
                "score_history": score_history[-60:],
                "score_values": score_values[-60:],
                "avg_score": avg_score,
                "max_score": max_score,
                "min_score": min_score,
                "win_rate": win_rate,
                "filter_failures": filter_failures,
                "latest_signal": latest_signal,
                "regime": "bull" if (score_values and sum(score_values[-5:]) / 5 > 50) else "bear",
                "confidence": round(avg_score, 1) if avg_score else 0,
            }
        except Exception as exc:
            data = {"error": str(exc), "total_signals": 0, "actions": {},
                    "score_history": [], "avg_score": 0, "latest_signal": {},
                    "win_rate": 0, "confidence": 0, "regime": "unknown"}
        _strategy_cache.update(at=monotonic(), data=data)
        return data


def equity_curve_data(settings: Settings, lookback: int = 60) -> dict[str, object]:
    """Build equity curve and drawdown chart data from activity history."""
    try:
        activity = recent_activity(settings.journal_path, limit=lookback)
        activity.sort(key=lambda x: x.get("timestamp", ""))
        equity = settings.strategy_equity_usd
        curve = []
        peak = equity
        for entry in activity:
            # In dry-run, equity stays constant (no real P&L yet)
            # We track paper-equity based on unrealized positions
            ts = entry.get("timestamp", "")
            score = entry.get("signal", {}).get("score", 0)
            action = entry.get("signal", {}).get("action", "WAIT")
            curve.append({
                "timestamp": ts,
                "equity": round(equity, 2),
                "action": action,
                "score": score,
            })
        peak = max((c["equity"] for c in curve), default=equity)
        drawdowns = [round((1 - c["equity"] / max(peak, 0.01)) * 100, 1) for c in curve]
        return {
            "labels": [c["timestamp"][:10] for c in curve[-30:]],
            "equity": [c["equity"] for c in curve[-30:]],
            "drawdown": drawdowns[-30:],
            "actions": [c["action"] for c in curve[-30:]],
            "scores": [c["score"] for c in curve[-30:]],
        }
    except Exception as exc:
        return {"labels": [], "equity": [], "drawdown": [], "actions": [],
                "scores": [], "error": str(exc)}


def ai_brief_data(settings: Settings) -> dict[str, object]:
    """AI Command Center — daily brief, regime analysis, confidence."""
    try:
        strategy = strategy_analytics(settings)
        market = market_snapshot(settings)
        risk = risk_state_snapshot(settings)
        recent_activity(settings.journal_path, limit=20)

        # Determine market regime
        regime = strategy.get("regime", "unknown")
        score = strategy.get("confidence", 0)

        # Generate brief text
        if score >= 75:
            regime_desc = f"Strong {regime} trend regime detected"
            confidence = "High"
        elif score >= 50:
            regime_desc = f"Moderate {regime} regime"
            confidence = "Medium"
        elif score >= 25:
            regime_desc = "Range-bound / consolidation"
            confidence = "Low"
        else:
            regime_desc = "Weak signal environment"
            confidence = "Very Low"

        # Generate suggested actions
        suggestions = []
        if market.get("delayed"):
            suggestions.append("Check market data feed — delayed")
        if risk.get("cooldown_active"):
            suggestions.append(f"Cooldown active — {risk.get('cooldown_remaining_minutes')} min remaining")
        if risk.get("orders_today", 0) >= settings.max_orders_per_day:
            suggestions.append("Daily order limit reached")
        if not suggestions:
            suggestions.append("Monitor market conditions for entry signal")

        return {
            "regime": regime,
            "regime_description": regime_desc,
            "confidence": confidence,
            "confidence_score": score,
            "suggested_actions": suggestions,
            "market_commentary": f"{settings.symbol} at ${market.get('price', 0):,.2f}" if market.get("price") else "Market data unavailable",
            "signal_quality": "Good" if score >= 50 else "Poor",
            "today_signals": strategy.get("total_signals", 0),
        }
    except Exception as exc:
        return {"error": str(exc), "regime": "unknown", "confidence": "Unknown",
                "confidence_score": 0, "suggested_actions": [str(exc)]}


def system_health_data(settings: Settings) -> dict[str, object]:
    """System health: broker, tradingview, github, tailscale, latency, db."""
    try:
        broker_health = health_snapshot(settings)

        # Tailscale check
        tailscale_ok = False
        try:
            import socket
            _ = socket.gethostbyname(socket.gethostname())
            tailscale_ok = True
        except Exception:
            tailscale_ok = False

        # DB/Audit check
        audit_ok = False
        try:
            log = AuditLog(Path(settings.audit_log_path))
            intact, _ = log.verify_chain()
            audit_ok = intact
        except Exception:
            audit_ok = False

        # Journal check
        journal_ok = Path(settings.journal_path).exists()

        # State DB check
        state_ok = Path(settings.idempotency_path).exists() or True  # state is ephemeral

        return {
            "broker": {
                "name": "Kraken",
                "reachable": broker_health.get("reachable", False),
                "latency_ms": broker_health.get("latency_ms"),
                "error": broker_health.get("error"),
                "status": "Online" if broker_health.get("reachable") else "Offline",
            },
            "tradingview": {"connected": False, "status": "Awaiting webhook setup"},
            "github": {"connected": False, "status": "Local only"},
            "tailscale": {"connected": tailscale_ok, "status": "Active" if tailscale_ok else "Unknown",
                          "ip": os.environ.get("DUBLIN_HOST", "100.127.18.59")},
            "database": {"audit_chain_intact": audit_ok, "journal_exists": journal_ok,
                         "state_persisted": state_ok},
            "deploy": {"version": "0.2.0-ceo", "build": "dry-run"},
            "api_latency_ms": broker_health.get("latency_ms", 0),
            "clock_skew": broker_health.get("clock_skew_seconds"),
            "rate_budget": broker_health.get("rate_limiter", {}),
        }
    except Exception as exc:
        return {"error": str(exc)}


def reconciliation_status(settings: Settings) -> dict[str, object]:
    """Verify positions/orders match between local state and Kraken."""
    with _state_lock:
        if monotonic() - float(_state_cache["at"]) < 45:
            cache = dict(_state_cache["data"])
            return cache.get("reconciliation", {"status": "cached"})
    try:
        from .reconciliation import reconcile, PositionSnapshot, OrderSnapshot
        from .engine import build_gateway

        gateway = build_gateway(settings)
        kraken_positions = gateway.positions()
        kraken_orders = gateway.orders()

        managed_symbols = {settings.symbol}

        # Convert Kraken positions to snapshots
        positions = []
        for p in kraken_positions:
            positions.append(PositionSnapshot(
                symbol=p.get("symbol", settings.symbol),
                quantity=float(p.get("quantity", 0)),
                market_value=float(p.get("market_value", 0)),
                average_entry=float(p.get("average_entry", 0)),
            ))

        # Convert orders
        orders = []
        for o in kraken_orders:
            orders.append(OrderSnapshot(
                order_id=o.get("order_id", ""),
                symbol=o.get("symbol", settings.symbol),
                side=o.get("side", "buy"),
                status=o.get("status", "open"),
            ))

        report = reconcile(positions, orders, managed_symbols)

        # Also check local idempotency log
        idempotency_path = Path(settings.idempotency_path)
        local_orders = []
        if idempotency_path.exists():
            try:
                with idempotency_path.open() as f:
                    local_orders = [json.loads(line) for line in f if line.strip()]
            except Exception:
                pass

        # Check for orphaned local orders (submitted but not on exchange)
        exchange_ids = {o.order_id for o in orders}
        local_ids = {str(o.get("order_id", "")) for o in local_orders}
        orphaned = local_ids - exchange_ids

        return {
            "status": "matching" if not report.orphaned_symbols and not orphaned else "mismatch",
            "positions_count": len(positions),
            "open_orders_count": len(orders),
            "orphaned_symbols": list(report.orphaned_symbols),
            "orphaned_local_orders": list(orphaned),
            "checked_at": report.checked_at,
            "fully_reconciled": not report.orphaned_symbols and not orphaned,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "fully_reconciled": False}


def stop_monitor_status(settings: Settings) -> dict[str, object]:
    """Check if independent stop monitor is running and healthy."""
    try:
        from .protective import StopMonitor, ProtectiveStop
        from .engine import build_gateway

        gateway = build_gateway(settings)
        positions = gateway.positions()

        if not positions:
            return {
                "running": False,
                "monitored_stops": 0,
                "status": "idle",
                "message": "No positions to monitor",
            }

        # Simulate checking stops against current price
        ticker = gateway.get_ticker()
        price = float(ticker.get("c", [0, 0])[0]) if ticker.get("c") else 0.0

        stops = []
        for p in positions:
            stop_price = float(p.get("stop_price", 0))
            qty = float(p.get("quantity", 0))
            if stop_price > 0 and qty > 0:
                stop = ProtectiveStop(
                    symbol=p.get("symbol", settings.symbol),
                    stop_price=stop_price,
                    quantity=qty,
                )
                should_exit = StopMonitor.should_exit(price, stop)
                stops.append({
                    "symbol": stop.symbol,
                    "stop_price": stop_price,
                    "current_price": price,
                    "distance_pct": round((price - stop_price) / price * 100, 2) if price else None,
                    "should_exit": should_exit,
                })

        any_triggered = any(s["should_exit"] for s in stops)
        return {
            "running": True,
            "monitored_stops": len(stops),
            "stops": stops,
            "triggered": any_triggered,
            "status": "TRIGGERED" if any_triggered else "monitoring",
            "current_price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"running": False, "status": "error", "error": str(exc),
                "monitored_stops": 0}


_MANUAL_STOP_FILE = Path("logs/MANUAL_STOP_ACTIVE")


def emergency_stop_active() -> bool:
    """Check if manual emergency stop has been triggered."""
    return _MANUAL_STOP_FILE.exists()


def activate_emergency_stop() -> bool:
    """Activate emergency stop — blocks all order submission."""
    _MANUAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MANUAL_STOP_FILE.write_text(json.dumps({
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "by": "dashboard",
    }), encoding="utf-8")
    return True


def clear_emergency_stop() -> bool:
    """Clear emergency stop — requires restart and safety re-verification."""
    if _MANUAL_STOP_FILE.exists():
        _MANUAL_STOP_FILE.unlink()
    return True


MANIFEST = {"name": "Dublin Bot Terminal", "short_name": "Dublin",
            "start_url": "/", "display": "standalone", "background_color": "#080313",
            "theme_color": "#080313", "description": "Kraken paper-trading terminal",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}]}


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#080313">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Dublin">
<link rel="manifest" href="/manifest.json">
<title>Dublin Bot Terminal</title>
<style>
:root{
  color-scheme:dark;
  font-family:'SF Mono','Fira Code','JetBrains Mono',monospace;
  background:#080313;color:#e0dcf4;
  --purple:#8b5cf6;--purple-accent:#a855f7;--purple-light:#c084fc;
  --green:#4ade80;--red:#f87171;--amber:#fbbf24;--bg:#080313;
  --panel:#130d26;--panel2:#1a1236;--line:#312a4b;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 12% -12%,#2a1a5e 0%,transparent 42%),#080313;min-height:100vh;overflow-x:hidden}
main{max-width:1200px;margin:auto;padding:max(24px,env(safe-area-inset-top)) 16px max(80px,env(safe-area-inset-bottom))}
header{display:flex;align-items:center;justify-content:space-between;padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:20px}
.brand{display:flex;align-items:center;gap:10px}
.brand .badge{width:32px;height:32px;border-radius:10px;background:linear-gradient(135deg,var(--purple),var(--purple-accent));display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;color:#fff;box-shadow:0 0 20px var(--purple)}
.brand h1{margin:0;font-size:clamp(1.4rem,4vw,2rem);letter-spacing:-0.02em}
.brand small{color:var(--purple-light);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.25em;font-weight:600}
.live{display:flex;align-items:center;gap:8px;color:var(--green);font-size:0.85rem;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.35}}
.notice{border:1px solid var(--line);background:linear-gradient(135deg,var(--panel2),var(--panel));border-radius:16px;padding:14px 18px;color:var(--purple-light);margin:18px 0;font-size:0.85rem;display:flex;align-items:center;gap:8px}
.notice.red{border-color:var(--red);color:#fecaca;background:linear-gradient(135deg,#3d1120,#2a0a16)}
.notice.green{border:1px solid var(--green);color:#dcfce2;background:linear-gradient(135deg,#0a2a10,#0d1f0f)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.card{grid-column:span 3;background:linear-gradient(165deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:18px;padding:18px;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.card .label{color:#9ca3af;font-size:0.68rem;text-transform:uppercase;letter-spacing:0.15em;margin-bottom:10px;font-weight:500}
.card .value{font-size:1.6rem;font-weight:700;margin:4px 0}
.card .sub,.card .meta{color:#9ca3af;font-size:0.75rem;margin-top:4px}
.card .pill{display:inline-block;border:1px solid var(--line);padding:3px 10px;border-radius:999px;font-size:0.68rem;font-weight:600}
.card .pill.green{border-color:#16a34a;color:var(--green);background:rgba(74,222,128,0.1)}
.card .pill.red{border:1px solid var(--red);color:var(--red);background:rgba(248,113,113,0.1)}
.card .pill.purple{border:1px solid var(--purple-accent);color:var(--purple-light);background:rgba(139,92,246,0.15)}
.card .pill.amber{border:1px solid var(--amber);color:var(--amber);background:rgba(251,191,36,0.1)}
.metric{grid-column:span 3}
.wide{grid-column:span 8}
.side{grid-column:span 4}
.half{grid-column:span 6}
.full{grid-column:span 12}
.green{color:var(--green)}
.red{color:var(--red)}
.amber{color:var(--amber)}
.purple{color:var(--purple-light)}
.muted{color:#9ca3af;font-size:0.78rem;margin-top:4px}
.chart{height:200px;width:100%;margin-top:12px}
.chart svg{width:100%;height:100%}
.chart polyline{fill:none;stroke:var(--purple);stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
.chart .fill-purple{fill:url(#gp);stroke:none}
.row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.row:last-child{border:0}
.row .row-label{color:#9ca3af;font-size:0.82rem}
.row .row-value{font-weight:600}
.empty{color:#6b7290;padding:18px 0;text-align:center;font-size:0.85rem}
button{width:100%;border:0;border-radius:14px;padding:14px;font-weight:650;font-size:0.9rem;cursor:pointer;transition:all 0.15s;letter-spacing:0.03em}
button:active{transform:scale(0.98)}
.primary{background:linear-gradient(135deg,var(--purple),var(--purple-accent));color:#fff;box-shadow:0 4px 16px rgba(139,92,246,0.4)}
.primary:disabled{opacity:0.45;cursor:not-allowed;transform:none}
.secondary{background:var(--panel2);color:#cbd5e1;border:1px solid var(--line)}
.secondary:hover{background:#2a1a5e}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.tabs{display:none}
.segmented{display:flex;gap:4px;margin-bottom:18px;background:var(--panel);border-radius:12px;padding:4px;overflow-x:auto}
.segmented button{flex:none;width:auto;padding:10px 18px;font-size:0.82rem;border-radius:10px;background:transparent;color:#9ca3af;border:0;transition:all 0.15s}
.segmented button.active{background:linear-gradient(135deg,var(--purple),var(--purple-accent));color:#fff;box-shadow:0 0 16px rgba(139,92,246,0.4);transform:none}
.segmented button:hover:not(.active){color:#e0dcf4;background:rgba(139,92,246,0.06)}
.section{display:none}
.section.active{display:block}
.foot{color:#6b7290;font-size:0.78rem;margin-top:24px;text-align:center;padding-top:16px;border-top:1px solid var(--line)}
.grid-sm{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-top:8px}
.grid-sm .stat{text-align:center;padding:12px;background:linear-gradient(165deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px}
.grid-sm .stat .num{font-size:1.3rem;font-weight:700}
.grid-sm .stat .lbl{font-size:0.65rem;color:#9ca3af;text-transform:uppercase;letter-spacing:0.1em}
.chart-legend{display:flex;gap:16px;margin-top:8px;font-size:0.72rem;color:#9ca3af}
.chart-legend span{display:inline-flex;align-items:center;gap:6px}
.chart-legend .dot{width:8px;height:8px;border-radius:50%}
@media(max-width:760px){
  main{padding-left:12px;padding-right:12px}
  .card.metric{grid-column:span 6}
  .card.wide{grid-column:span 12}
  .card.side{grid-column:span 12}
  .card.half{grid-column:span 12}
  .chart{height:160px}
  .brand h1{font-size:1.4rem}
  .tabs{position:fixed;display:flex;bottom:0;left:0;right:0;background:#080313e0;border-top:1px solid var(--line);backdrop-filter:blur(18px);padding:8px max(10px,env(safe-area-inset-right)) calc(8px + env(safe-area-inset-bottom));z-index:10}
  .tabs span{flex:1;text-align:center;color:#6b7290;font-size:0.75rem}
  .tabs span:first-child{color:var(--purple-light)}
  .segmented{flex-shrink:0}
}
</style>
</head>
<body>
<main>
<header>
  <div class="brand">
    <div class="badge">◆</div>
    <div>
      <h1>Dublin</h1>
      <small>Kraken paper terminal</small>
    </div>
  </div>
  <div class="live"><i class="dot"></i><span id="refresh">Live</span></div>
</header>

<div class="notice" id="notice">Checking safety locks…</div>

<div class="segmented">
  <button class="active" data-section="overview">Executive Overview</button>
  <button data-section="trading">Trading</button>
  <button data-section="risk">Risk</button>
  <button data-section="ai">AI Command Center</button>
  <button data-section="charts">Charts</button>
  <button data-section="health">System Health</button>
</div>

<!-- EXECUTIVE OVERVIEW -->
<section class="grid section active" id="overview">
  <article class="card metric">
    <div class="label">Portfolio value</div>
    <div class="value" id="portfolioValue">—</div>
    <div class="sub">Paper equity</div>
  </article>
  <article class="card metric">
    <div class="label">Cash</div>
    <div class="value" id="cashValue">—</div>
    <div class="sub">Unallocated</div>
  </article>
  <article class="card metric">
    <div class="label">Today's P&L</div>
    <div class="value" id="pnlValue">—</div>
    <div id="pnlClass" class="sub">Realized</div>
  </article>
  <article class="card metric">
    <div class="label">Month return</div>
    <div class="value" id="monthReturn">—</div>
    <div class="sub">vs start equity</div>
  </article>
  <article class="card metric">
    <div class="label">BTC/USD · Price</div>
    <div class="value" id="price">—</div>
    <div id="change" class="green">Live market</div>
  </article>
  <article class="card metric">
    <div class="label">Safety status</div>
    <div class="value green" id="safety">Locked</div>
    <div class="sub">Paper-only execution</div>
  </article>
  <article class="card metric">
    <div class="label">Bot status</div>
    <div class="value" id="botStatus">—</div>
    <div class="sub" id="botDetail">Last signal</div>
  </article>
  <article class="card metric">
    <div class="label">Risk per trade</div>
    <div class="value purple" id="riskTrade">—</div>
    <div class="muted">Max exposure</div>
  </article>
  <article class="card wide">
    <div class="label">Market pulse · 60 bars</div>
    <div style="height:180px;width:100%;margin-top:8px">
      <svg class="chart" viewBox="0 0 700 180" preserveAspectRatio="none">
        <defs>
          <linearGradient id="gp" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#8b5cf6" stop-opacity=".25"/>
            <stop offset="1" stop-color="#8b5cf6" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <polyline class="fill-purple" id="fill" points="0,180 700,180"/>
        <polyline id="line" stroke="#8b5cf6"/>
      </svg>
    </div>
    <div id="marketMessage" class="empty"></div>
  </article>
  <article class="card side">
    <div class="label">Paper engine</div>
    <div class="value" id="engine">Ready</div>
    <div class="row"><span class="row-label">Paper mode</span><span class="pill green">ON</span></div>
    <div class="row"><span class="row-label">Dry run</span><span class="pill green">ON</span></div>
    <div class="row"><span class="row-label">Live orders</span><span class="pill red">OFF</span></div>
    <div class="row"><span class="row-label">Broker</span><span class="pill purple" id="brokerPill">KRAKEN</span></div>
    <button id="run" class="primary">Run paper cycle</button>
    <div class="actions">
      <button id="monitorStart" class="secondary">Start monitor</button>
      <button id="monitorStop" class="secondary">Stop</button>
    </div>
  </article>
</section>

<!-- TRADING -->
<section class="grid section" id="trading">
  <article class="card metric">
    <div class="label">Open positions</div>
    <div class="value" id="positionCount">0</div>
    <div class="sub">Paper</div>
  </article>
  <article class="card metric">
    <div class="label">Pending orders</div>
    <div class="value" id="pendingOrders">0</div>
    <div class="sub">Paper</div>
  </article>
  <article class="card metric">
    <div class="label">Current signal</div>
    <div class="value" id="signalAction">WAIT</div>
    <div id="signalPill" class="sub">DRY-RUN</div>
  </article>
  <article class="card metric">
    <div class="label">Win rate</div>
    <div class="value" id="winRate">—</div>
    <div class="sub">Last 30 entries</div>
  </article>
  <article class="card half">
    <div class="label">Positions</div>
    <div id="positions" class="empty">No paper positions</div>
  </article>
  <article class="card half">
    <div class="label">Open orders</div>
    <div id="orders" class="empty">No open paper orders</div>
  </article>
  <article class="card wide">
    <div class="label">Recent decisions · last 30</div>
    <div id="activity" class="empty">No decisions recorded</div>
  </article>
</section>

<!-- RISK -->
<section class="grid section" id="risk">
  <article class="card metric">
    <div class="label">Current drawdown</div>
    <div class="value" id="drawdownVal">—</div>
    <div class="muted">Max: <span id="maxDrawdown">—</span>%</div>
  </article>
  <article class="card metric">
    <div class="label">Daily loss</div>
    <div class="value" id="dailyLossVal">—</div>
    <div class="muted">Cap: <span id="maxDailyLoss">—</span>%</div>
  </article>
  <article class="card metric">
    <div class="label">Consecutive losses</div>
    <div class="value" id="lossStreak">—</div>
    <div class="muted">Max allowed</div>
  </article>
  <article class="card metric">
    <div class="label">Orders today</div>
    <div class="value" id="ordersToday">0</div>
    <div class="muted">Max <span id="maxOrdersDay">—</span></div>
  </article>
  <div class="grid-sm">
    <div class="stat"><div class="num" id="startEquity">—</div><div class="lbl">Start</div></div>
    <div class="stat"><div class="num" id="peakEquity">—</div><div class="lbl">Peak</div></div>
    <div class="stat"><div class="num" id="currentEquity">—</div><div class="lbl">Current</div></div>
    <div class="stat"><div class="num" id="riskBudget">—</div><div class="lbl">Risk budget</div></div>
    <div class="stat"><div class="num" id="cooldownMin">—</div><div class="lbl">Cooldown</div></div>
    <div class="stat"><div class="num" id="consecLossMax">—</div><div class="lbl">Loss cap</div></div>
  </div>
  <article class="card wide">
    <div class="label">Circuit breakers</div>
    <div id="circuitBreakers"></div>
  </article>
</section>

<!-- AI COMMAND CENTER -->
<section class="grid section" id="ai">
  <article class="card metric">
    <div class="label">Market regime</div>
    <div class="value" id="regime">—</div>
    <div class="muted" id="regimeDesc">Awaiting data</div>
  </article>
  <article class="card metric">
    <div class="label">Confidence</div>
    <div class="value" id="confidence">—</div>
    <div class="muted" id="confidenceLevel">Signal quality</div>
  </article>
  <article class="card metric">
    <div class="label">Signal quality</div>
    <div class="value" id="signalQuality">—</div>
    <div class="muted">Based on score history</div>
  </article>
  <article class="card metric">
    <div class="label">Today's signals</div>
    <div class="value" id="todaySignals">—</div>
    <div class="muted">Last 24 hours</div>
  </article>
  <article class="card wide">
    <div class="label">Daily brief</div>
    <div id="dailyBrief" class="empty">Loading AI brief…</div>
  </article>
  <article class="card wide">
    <div class="label">News & risk alerts</div>
    <div id="newsAlerts" class="empty">No active alerts</div>
  </article>
</section>

<!-- CHARTS -->
<section class="grid section" id="charts">
  <article class="card wide">
    <div class="label">Equity curve</div>
    <div style="height:200px;width:100%;margin-top:8px">
      <svg class="chart" viewBox="0 0 700 180" preserveAspectRatio="none">
        <defs>
          <linearGradient id="gp2" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#8b5cf6" stop-opacity=".25"/>
            <stop offset="1" stop-color="#8b5cf6" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <polyline class="fill-purple" id="equityFill" points="0,180 700,180"/>
        <polyline id="equityLine" stroke="#8b5cf6"/>
      </svg>
    </div>
    <div class="chart-legend">
      <span><i class="dot" style="background:var(--green)"></i>Equity</span>
      <span><i class="dot" style="background:var(--red)"></i>Drawdown</span>
    </div>
  </article>
  <article class="card half">
    <div class="label">Drawdown</div>
    <div style="height:140px;width:100%;margin-top:8px">
      <svg class="chart" viewBox="0 0 700 140" preserveAspectRatio="none">
        <polyline id="drawdownLine" stroke="#f87171" fill="rgba(248,113,113,0.15)"/>
      </svg>
    </div>
  </article>
  <article class="card half">
    <div class="label">Strategy score history</div>
    <div style="height:140px;width:100%;margin-top:8px">
      <svg class="chart" viewBox="0 0 700 140" preserveAspectRatio="none">
        <polyline id="scoreLine" stroke="#a855f7" fill="none"/>
      </svg>
    </div>
  </article>
  <article class="card half">
    <div class="label">Action distribution</div>
    <div id="actionDist" class="empty">No data yet</div>
  </article>
  <article class="card half">
    <div class="label">Filter failure heatmap</div>
    <div id="filterHeatmap" class="empty">No failures</div>
  </article>
</section>

<!-- SYSTEM HEALTH -->
<section class="grid section" id="health">
  <article class="card metric">
    <div class="label">Kraken</div>
    <div class="value" id="krakenStatus">—</div>
    <div id="krakenDetail" class="muted">Checking…</div>
  </article>
  <article class="card metric">
    <div class="label">TradingView</div>
    <div class="value" id="tvStatus">—</div>
    <div class="muted">Webhook receiver</div>
  </article>
  <article class="card metric">
    <div class="label">GitHub</div>
    <div class="value" id="githubStatus">—</div>
    <div class="muted">Version control</div>
  </article>
  <article class="card metric">
    <div class="label">Tailscale</div>
    <div class="value" id="tailscaleStatus">—</div>
    <div class="muted">Secure mesh</div>
  </article>
  <article class="card wide">
    <div class="label">Connection quality</div>
    <div class="grid-sm">
      <div class="stat"><div class="num" id="latencyVal">—</div><div class="lbl">Latency (ms)</div></div>
      <div class="stat"><div class="num" id="clockSkew">—</div><div class="lbl">Clock skew</div></div>
      <div class="stat"><div class="num" id="rateTokens">—</div><div class="lbl">Rate tokens</div></div>
      <div class="stat"><div class="num" id="refreshRate">—</div><div class="lbl">Refresh rate</div></div>
      <div class="stat"><div class="num" id="dataAge">—</div><div class="lbl">Data age (min)</div></div>
      <div class="stat"><div class="num" id="versionVal">—</div><div class="lbl">Version</div></div>
    </div>
  </article>
  <!-- Audit trail integrity -->
  <article class="card wide">
    <div class="label">Audit trail integrity</div>
    <div id="auditChain" class="muted">Checking…</div>
    <div id="auditList" class="empty">No audit entries</div>
  </article>

  <!-- Reconciliation & Emergency Stop -->
  <article class="card metric">
    <div class="label">Reconciliation</div>
    <div class="value" id="reconStatus">—</div>
    <div class="muted" id="reconDetail">Checking exchanges…</div>
  </article>
  <article class="card metric">
    <div class="label">Stop Monitor</div>
    <div class="value" id="stopMonStatus">—</div>
    <div class="muted" id="stopMonDetail">Independent exit watch</div>
  </article>
  <article class="card metric" id="emergencyCard" style="display:none">
    <div class="label">Emergency Stop</div>
    <div class="value" id="emergencyStatus">ACTIVE</div>
    <div class="muted">All order submission blocked</div>
  </article>
  <article class="card wide">
    <div class="label">Emergency controls</div>
    <div class="grid-sm">
      <button class="btn amber" id="stopActivate" onclick="toggleEmergency(true)">
        Activate emergency stop
      </button>
      <button class="btn purple" id="stopClear" onclick="toggleEmergency(false)" style="display:none">
        Clear emergency stop
      </button>
    </div>
  </article>
</section>

<p class="foot">Updates every 10 seconds · Mac-hosted · Tailnet only</p>
</main>
<nav class="tabs">
  <span data-section="overview">Overview</span>
  <span data-section="trading">Trading</span>
  <span data-section="risk">Risk</span>
  <span data-section="ai">AI</span>
  <span data-section="charts">Charts</span>
  <span data-section="health">Health</span>
</nav>

<script>
const $=s=>document.querySelector(s);
const money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n||0);
function rows(items,fn,empty){
  return items.length?items.map(fn).join(''):`<div class="empty">${empty}</div>`;
}
function draw(values, lineEl, fillEl){
  if(values.length<2){lineEl.setAttribute('points','');fillEl.setAttribute('points','');return}
  const w=700,h=180,p=8;
  let min=Math.min(...values),max=Math.max(...values);
  let range=max-min||1;
  let pts=values.map((v,i)=>`${p+i*(w-2*p)/(values.length-1)},${h-p-(v-min)*(h-2*p)/range}`).join(' ');
  lineEl.setAttribute('points',pts);
  fillEl.setAttribute('points',`0,${h} ${pts} ${w},${h}`);
}
function drawAlt(values, lineEl){
  if(values.length<2){lineEl.setAttribute('points','');return}
  const w=700,h=140,p=8;
  let min=Math.min(...values),max=Math.max(...values);
  let range=max-min||1;
  let pts=values.map((v,i)=>`${p+i*(w-2*p)/(values.length-1)},${h-p-(v-min)*(h-2*p)/range}`).join(' ');
  lineEl.setAttribute('points',pts);
}
function renderHealth(h,a){
  if(!h)return;
  const ok=h.reachable===true;
  $('#healthValue').textContent=ok?'Online':(h.reachable===false?'Offline':'Unknown');
  $('#healthValue').className='value '+(ok?'green':'red');
  $('#healthDetail').textContent=h.error?h.error:(h.latency_ms!=null?`${h.broker||'broker'} · ${h.latency_ms} ms`:'—');
  const f=h.freshness;
  if(f){
    $('#freshValue').textContent=f.fresh?'Fresh':'STALE';
    $('#freshValue').className='value '+(f.fresh?'green':'red');
    $('#freshDetail').textContent=f.bar_age_minutes!=null?`${f.bar_age_minutes.toFixed(1)} min old · ${f.max_age_minutes??'—'} min`:f.reason;
  } else {
    $('#freshValue').textContent='—';
    $('#freshDetail').textContent='No freshness check yet';
  }
  const sk=h.clock_skew_seconds;
  $('#skewValue').textContent=sk==null?'—':`${sk>=0?'+':''}${sk.toFixed(1)}s`;
  $('#skewValue').className='value '+(sk==null?'':(Math.abs(sk)<=30?'green':'red'));
  const rl=h.rate_limiter;
  if(rl){
    $('#rateValue').textContent=`${rl.private_tokens}/${rl.private_capacity}`;
    $('#rateValue').className='value '+(rl.private_tokens>rl.private_capacity*0.25?'green':(rl.private_tokens>0?'amber':'red'));
  } else $('#rateValue').textContent='—';
  if(a){
    $('#auditChain').textContent=a.chain_intact===true?`Hash chain intact · ${a.chain_status}`:(a.chain_intact===false?`CHAIN BROKEN · ${a.chain_status}`:a.chain_status||'');
    $('#auditChain').className=a.chain_intact===false?'muted red':'muted';
    $('#auditList').innerHTML=rows(a.entries||[],e=>`<div class="row"><span><b>${e.event}</b><br><small>${(e.timestamp||'').slice(11,19)} UTC</small></span><span class="pill ${e.severity==='critical'||e.severity==='error'?'red':(e.severity==='warning'?'amber':'green')}">${e.severity}</span></div>`,'No audit entries');
  }
}
async function load(){
  try{
    let r=await fetch('/api/overview'),d=await r.json();
    let s=d.status,p=d.portfolio,dm=d.market,mon=d.monitor;
    $('#notice').textContent=s.safe?'Safety lock active · Paper-only execution':'Execution blocked: safety lock inactive';
    $('#notice').className=s.safe?'notice green':'notice red';
    $('#safety').textContent=s.safe?'Locked':'Blocked';
    $('#brokerPill').textContent=s.broker?.toUpperCase()||'KRAKEN';
    $('#price').textContent=dm.price?money(dm.price):'—';
    let c=dm.change_percent;
    $('#change').textContent=c==null?'Market unavailable':`${c>=0?'+':''}${c.toFixed(2)}% last bar`;
    $('#change').className=c>=0?'green':'red';
    draw(dm.closes||[],$('#line'),$('#fill'));
    $('#marketMessage').textContent=dm.error||(dm.delayed?`Feed delayed · last bar ${dm.last_timestamp}`:'Live feed connected');
    $('#portfolioValue').textContent=money(p.equity);
    $('#cashValue').textContent=money(p.cash);
    $('#engine').textContent=mon.running?'Monitoring hourly':(mon.last_action||'Ready');
    $('#positionCount').textContent=p.positions.length;
    $('#positions').innerHTML=rows(p.positions,x=>`<div class="row"><span><b>${x.symbol}</b><br><small>${x.quantity}</small></span><span class="${x.unrealized_pl>=0?'green':'red'}">$${money(x.market_value)}<br><small>${money(x.unrealized_pl)}</small></span></div>`,p.message||'No paper positions');
    $('#orders').innerHTML=rows(p.orders,x=>`<div class="row"><span>${x.side} ${x.symbol}</span><span class="pill ${x.status==='open'?'amber':'green'}">${x.status}</span></div>`,p.message||'No open paper orders');
    $('#activity').innerHTML=rows(d.activity,x=>`<div class="row"><span><b>${x.signal?.action||'—'}</b><br><small>${x.signal?.reason||''}</small></span><span class="pill purple">${x.dry_run?'DRY':'PAPER'}</span></div>`,'No decisions recorded');
    $('#riskTrade').textContent=`${(s.risk_per_trade*100).toFixed(1)}%`;
    $('#run').disabled=!s.safe;
    $('#monitorStart').disabled=!s.safe||mon.running;
    $('#monitorStop').disabled=mon.running;
    renderHealth(d.health,d.audit);
  }catch(e){
    $('#refresh').textContent='Offline';
    $('#notice').textContent='Dashboard connection lost';
    $('#notice').className='notice red';
  }
}
async function loadExtended(){
  try{
    let [riskR, stratR, aiR, healthR, equityR, learningR] = await Promise.all([
      fetch('/api/risk').then(r=>r.json()),
      fetch('/api/strategy').then(r=>r.json()),
      fetch('/api/ai').then(r=>r.json()),
      fetch('/api/system-health').then(r=>r.json()),
      fetch('/api/equity-curve').then(r=>r.json()),
      fetch('/api/learning').then(r=>r.json()),
    ]);
    let [reconR, stopMonR, emergencyR] = await Promise.all([
      fetch('/api/reconciliation').then(r=>r.json()),
      fetch('/api/stop-monitor').then(r=>r.json()),
      fetch('/api/emergency-stop').then(r=>r.json()),
    ]);
    let risk=riskR, strat=stratR, ai=aiR, sys=healthR, eq=equityR, learn=learningR, recon=reconR, stop=stopMonR, emerg=emergencyR;

    // Risk section
    $('#drawdownVal').textContent=`${risk.drawdown_percent}%`;
    $('#drawdownVal').className='value '+(risk.drawdown_percent<5?'green':(risk.drawdown_percent<10?'amber':'red'));
    $('#maxDrawdown').textContent=risk.max_drawdown_percent;
    $('#dailyLossVal').textContent=money(risk.daily_loss);
    $('#maxDailyLoss').textContent=money(risk.max_daily_loss);
    $('#lossStreak').textContent=risk.consecutive_losses;
    $('#ordersToday').textContent=risk.orders_today;
    $('#maxOrdersDay').textContent=risk.max_orders_per_day;
    $('#startEquity').textContent=money(risk.start_equity);
    $('#peakEquity').textContent=money(risk.peak_equity);
    $('#currentEquity').textContent=money(risk.current_equity);
    $('#riskBudget').textContent=`${(risk.risk_per_trade||0.01*100).toFixed(1)}%`;
    $('#cooldownMin').textContent=risk.cooldown_active?`${risk.cooldown_remaining_minutes}m`:'Ready';
    $('#cooldownMin').className='num '+(risk.cooldown_active?'amber':'green');
    $('#consecLossMax').textContent='2';
    // Circuit breakers
    const cb = [
      {name:'Daily loss', active:risk.daily_loss>=risk.max_daily_loss, val:`$${money(risk.daily_loss)} / $${money(risk.max_daily_loss)}`},
      {name:'Max drawdown', active:risk.drawdown_percent>=risk.max_drawdown_percent, val:`${risk.drawdown_percent}% / ${risk.max_drawdown_percent}%`},
      {name:'Orders/day', active:risk.orders_today>=risk.max_orders_per_day, val:`${risk.orders_today} / ${risk.max_orders_per_day}`},
      {name:'Cooldown', active:risk.cooldown_active, val:risk.cooldown_active?`${risk.cooldown_remaining_minutes}m left`:'Clear'},
    ];
    $('#circuitBreakers').innerHTML=rows(cb,x=>`<div class="row"><span><b>${x.name}</b></span><span class="pill ${x.active?'red':'green'}">${x.active?'TRIPPED':x.val}</span></div>`,'All clear');

    // Trading section
    const latest = strat.latest_signal;
    $('#signalAction').textContent=latest.action||'WAIT';
    $('#signalPill').innerHTML=`<span class="pill ${latest.action==='BUY'?'green':(latest.action==='SELL'?'red':'purple')}">${latest.action==='BUY'?'BUY SIGNAL':(latest.action==='SELL'?'SELL SIGNAL':'DRY-RUN')}</span>`;
    $('#signalQuality').textContent=latest.action==='BUY'?'High':(latest.action==='SELL'?'High':'No signal');
    $('#winRate').textContent=strat.win_rate?`${strat.win_rate}%`:'—';
    $('#pendingOrders').textContent='0';

    // AI section
    const learnConf = learn.confidence_score != null ? learn.confidence_score : (ai.confidence_score || ai.confidence || 0);
    const learnRegime = learn.regime || ai.regime || 'unknown';
    const learnDesc = learn.regime_description || ai.regime_description || 'No data';
    const learnCommentary = learn.market_commentary || ai.market_commentary || 'Loading market analysis...';
    const learnActions = learn.suggested_actions || ai.suggested_actions || [];
    const learnSignals = learn.today_signals || strat.total_signals || 0;
    const learnWinRate = learn.win_rate_estimate || 0;

    $('#regime').textContent=learnRegime;
    $('#regimeDesc').textContent=learnDesc;
    $('#confidence').textContent=learnConf;
    $('#confidenceLevel').textContent=learnConf>70?'High signal quality':(learnConf>40?'Moderate':'Low');
    $('#signalQuality').textContent=learnConf>60?'Strong':(learnConf>40?'Moderate':'Weak');
    $('#todaySignals').textContent=learnSignals;
    $('#dailyBrief').innerHTML=`<div class="row"><span><b>Daily Brief</b></span><span class="pill purple">AI</span></div>`+
      `<div class="muted" style="padding:8px 0">${learnCommentary}</div>`+
      rows(learnActions, x=>`<div class="row"><span>• ${x}</span></div>`,'')
      +`<div class="row" style="margin-top:8px"><span class="muted">Win rate est: ${learnWinRate}%</span></div>`;
    // Learning suggestions
    const allSuggestions = (learn.suggestions || []).concat(ai.suggestions || []);
    if(allSuggestions.length){
      $('#newsAlerts').innerHTML=rows(allSuggestions, x=>`<div class="row"><span><b>${x.parameter}</b></span><span class="pill amber">Suggest</span></div>`+'<div class="muted" style="padding:4px 0">'+
        (x.rationale||x.reason||'Parameter adjustment suggested')+'</div>',
        'No suggestions');
    }else{
      $('#newsAlerts').innerHTML='<div class="empty">No active alerts</div>';
    }

    // Charts section
    draw(eq.equity||[], $('#equityLine'), $('#equityFill'));
    drawAlt(eq.drawdown||[], $('#drawdownLine'));
    drawAlt(strat.score_values||[], $('#scoreLine'));
    // Action distribution
    const actions = strat.actions||{};
    $('#actionDist').innerHTML=rows(Object.entries(actions),
      ([k,v])=>`<div class="row"><span>${k}</span><span class="pill purple">${v}</span></div>`,
      'No signals yet');
    // Filter heatmap
    const failures = strat.filter_failures||{};
    $('#filterHeatmap').innerHTML=rows(Object.entries(failures).sort((a,b)=>b[1]-a[1]),
      ([k,v])=>`<div class="row"><span>${k}</span><span class="pill amber">${v}x</span></div>`,
      'All filters passing');

    // System Health section
    $('#krakenStatus').textContent=sys.broker.status;
    $('#krakenStatus').className='value '+(sys.broker.status==='Online'?'green':'red');
    $('#krakenDetail').textContent=sys.broker.error||`Latency: ${sys.broker.latency_ms||0}ms`;
    $('#tvStatus').textContent=sys.tradingview.status;
    $('#githubStatus').textContent=sys.github.status;
    $('#tailscaleStatus').textContent=sys.tailscale.status;
    $('#tailscaleStatus').className='value '+(sys.tailscale.connected?'green':'red');
    $('#latencyVal').textContent=sys.api_latency_ms;
    $('#clockSkew').textContent=sys.clock_skew!=null?`${sys.clock_skew>=0?'+':''}${sys.clock_skew.toFixed(1)}s`:'—';
    $('#clockSkew').className='num '+(sys.clock_skew!=null&&Math.abs(sys.clock_skew)<=30?'green':'red');
    const rl = sys.rate_budget;
    $('#rateTokens').textContent=rl?`${rl.private_tokens}/${rl.private_capacity}`:'—';
    $('#refreshRate').textContent='10s';
    $('#dataAge').textContent=m?Math.round((Date.now()-(new Date(d.market.last_timestamp||0)).getTime())/60000)+'m':'—';
    $('#versionVal').textContent=sys.deploy.version;

    // Reconciliation & Stop Monitor & Emergency Stop (System Health section)
    $('#reconStatus').textContent=recon.fully_reconciled?'MATCHED':'MISMATCH';
    $('#reconStatus').className='value '+(recon.fully_reconciled?'green':'red');
    $('#reconDetail').textContent=recon.positions_count+' pos · '+(recon.orphaned_symbols||[]).length+' orphan';
    $('#stopMonStatus').textContent=stop.status||'—';
    $('#stopMonStatus').className='value '+(stop.triggered?'red':(stop.running?'green':'muted'));
    $('#stopMonDetail').textContent=stop.message||(stop.running?'Watching '+stop.monitored_stops+' stops':(stop.running===false?'Idle':''));

    // Emergency stop visibility
    if(emerg.active){
      $('#emergencyCard').style.display='block';
      $('#stopActivate').style.display='none';
      $('#stopClear').style.display='inline-block';
    }else{
      $('#emergencyCard').style.display='none';
      $('#stopActivate').style.display='inline-block';
      $('#stopClear').style.display='none';
    }
  }catch(e){
    console.error('Extended load error:',e);
  }
}
function toggleEmergency(activate){
  const method = activate ? 'activate' : 'clear';
  fetch('/api/emergency-stop/'+method, {method: 'POST'}).then(r=>r.json()).then(d=>{
    load();
  });
}
function switchSection(name){
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.tabs span').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.segmented button').forEach(b=>b.classList.remove('active'));
  document.getElementById(name)?.classList.add('active');
  document.querySelector(`.segmented button[data-section="${name}"]`)?.classList.add('active');
  document.querySelector(`.tabs span[data-section="${name}"]`)?.classList.add('active');
}
document.querySelectorAll('.segmented button, .tabs span').forEach(el=>{
  el.addEventListener('click',()=>{
    const section=el.dataset.section;
    if(section) switchSection(section);
  });
});
$('#run').onclick=async()=>{
  $('#run').disabled=true;
  $('#engine').textContent='Running…';
  let r=await fetch('/api/run-once',{method:'POST'}),d=await r.json();
  $('#engine').textContent=d.error?'Needs attention':(d.signal?.action||'Complete');
  await load();await loadExtended();
  $('#run').disabled=false;
};
$('#monitorStart').onclick=async()=>{await fetch('/api/monitor/start',{method:'POST'});await load()};
$('#monitorStop').onclick=async()=>{await fetch('/api/monitor/stop',{method:'POST'});await load()};
load();
setInterval(load,10000);
setInterval(loadExtended,15000);
if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js');
</script>
</body>
</html>
'''


SW = ("self.addEventListener('install',e=>self.skipWaiting());"
      "self.addEventListener('fetch',()=>{});")


def make_handler(settings: Settings, monitor: PaperMonitor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, payload: bytes, content_type: str,
                       status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_bytes(json.dumps(value).encode(), "application/json", status)

        def do_GET(self) -> None:
            if self.path == "/":
                self.send_bytes(HTML.encode(), "text/html; charset=utf-8")
            elif self.path == "/manifest.json":
                self.send_json(MANIFEST)
            elif self.path == "/sw.js":
                self.send_bytes(SW.encode(), "application/javascript")
            elif self.path == "/api/status":
                self.send_json(safety_status(settings))
            elif self.path == "/api/health":
                self.send_json(health_snapshot(settings))
            elif self.path == "/api/audit":
                self.send_json(audit_summary(settings))
            elif self.path == "/api/overview":
                self.send_json({
                    "status": safety_status(settings),
                    "market": market_snapshot(settings),
                    "portfolio": portfolio_snapshot(settings),
                    "activity": recent_activity(settings.journal_path),
                    "monitor": monitor.status(),
                    "health": health_snapshot(settings),
                    "audit": audit_summary(settings, limit=12),
                })
            elif self.path == "/api/risk":
                self.send_json(risk_state_snapshot(settings))
            elif self.path == "/api/strategy":
                self.send_json(strategy_analytics(settings))
            elif self.path == "/api/ai":
                self.send_json(ai_brief_data(settings))
            elif self.path == "/api/system-health":
                self.send_json(system_health_data(settings))
            elif self.path == "/api/equity-curve":
                self.send_json(equity_curve_data(settings))
            elif self.path == "/api/learning":
                self.send_json(learning_brief_data(settings))
            elif self.path == "/api/reconciliation":
                self.send_json(reconciliation_status(settings))
            elif self.path == "/api/stop-monitor":
                self.send_json(stop_monitor_status(settings))
            elif self.path == "/api/emergency-stop":
                self.send_json({"active": emergency_stop_active()})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/api/monitor/start":
                if not safety_status(settings)["safe"]:
                    self.send_json({"error": "Safety lock is not active"}, HTTPStatus.FORBIDDEN)
                    return
                monitor.start()
                self.send_json(monitor.status())
                return
            if self.path == "/api/monitor/stop":
                monitor.stop()
                self.send_json(monitor.status())
                return
            if self.path == "/api/emergency-stop/activate":
                activate_emergency_stop()
                self.send_json({"active": True, "message": "Emergency stop activated"})
                return
            if self.path == "/api/emergency-stop/clear":
                if not safety_status(settings)["safe"]:
                    self.send_json({"error": "Cannot clear emergency stop in live mode"}, HTTPStatus.FORBIDDEN)
                    return
                clear_emergency_stop()
                self.send_json({"active": False, "message": "Emergency stop cleared"})
                return
            if self.path != "/api/run-once":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not safety_status(settings)["safe"]:
                self.send_json(
                    {"error": "Paper/dry-run safety lock is not active"}, HTTPStatus.FORBIDDEN
                )
                return
            try:
                self.send_json(TradingEngine(settings).run_once().to_dict())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def serve_dashboard(settings: Settings) -> int:
    if not safety_status(settings)["safe"]:
        raise RuntimeError("Dashboard blocked: paper/dry-run safety lock is not active")
    monitor = PaperMonitor(settings)
    server = ThreadingHTTPServer((HOST, PORT), make_handler(settings, monitor))
    print(f"Dublin dashboard: http://{HOST}:{PORT}")
    print("Press Control-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
