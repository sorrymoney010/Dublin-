from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from .risk import SessionState


class StateStore:
    """Durable daily risk state stored locally and excluded from Git."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, equity: float) -> SessionState:
        today = date.today().isoformat()
        if not self.path.exists():
            return SessionState(equity, equity, equity)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return SessionState(equity, equity, equity)
        if data.get("session_date") != today:
            return SessionState(equity, equity, equity)
        last_order = data.get("last_order_at")
        return SessionState(
            start_equity=float(data.get("start_equity", equity)),
            peak_equity=max(float(data.get("peak_equity", equity)), equity),
            current_equity=equity,
            realized_pnl_today=float(data.get("realized_pnl_today", 0.0)),
            orders_today=int(data.get("orders_today", 0)),
            last_order_at=datetime.fromisoformat(last_order) if last_order else None,
        )

    def save(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload["session_date"] = date.today().isoformat()
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        payload["last_order_at"] = state.last_order_at.isoformat() if state.last_order_at else None
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
