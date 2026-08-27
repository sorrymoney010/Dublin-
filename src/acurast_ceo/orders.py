"""Monitor-as-a-Service order records (Tier 1 revenue engine).

Each order = one paying customer endpoint we monitor on Acurast. We persist
who is paying, what we watch, the price, and the live deployment id so the
Sentinel can reconcile revenue against actual deployments.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class MonitorOrder:
    customer: str
    target_url: str
    alert_webhook: str
    price_usd: float = 9.0
    check_interval_ms: int = 60000
    deployment_id: Optional[int] = None
    cid: Optional[str] = None
    status: str = "pending"          # pending | deployed | failed | cancelled
    created_at: str = ""
    note: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_orders (
    customer         TEXT NOT NULL,
    target_url       TEXT NOT NULL,
    alert_webhook    TEXT NOT NULL,
    price_usd        REAL NOT NULL,
    check_interval_ms INTEGER NOT NULL,
    deployment_id    INTEGER,
    cid              TEXT,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    note             TEXT
);
"""


class OrderStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def add(self, o: MonitorOrder) -> None:
        self._conn.execute(
            """INSERT INTO monitor_orders
               (customer, target_url, alert_webhook, price_usd, check_interval_ms,
                deployment_id, cid, status, created_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (o.customer, o.target_url, o.alert_webhook, o.price_usd, o.check_interval_ms,
             o.deployment_id, o.cid, o.status, o.created_at, o.note),
        )
        self._conn.commit()

    def update_status(self, customer: str, target_url: str, status: str,
                      deployment_id: Optional[int] = None, cid: Optional[str] = None,
                      note: str = "") -> None:
        self._conn.execute(
            """UPDATE monitor_orders SET status=?, deployment_id=COALESCE(?,deployment_id),
               cid=COALESCE(?,cid), note=? WHERE customer=? AND target_url=?""",
            (status, deployment_id, cid, note, customer, target_url),
        )
        self._conn.commit()

    def list(self) -> list[MonitorOrder]:
        rows = self._conn.execute("SELECT * FROM monitor_orders ORDER BY created_at DESC").fetchall()
        return [MonitorOrder(**dict(r)) for r in rows]

    def monthly_revenue(self) -> float:
        rows = self._conn.execute(
            "SELECT COALESCE(SUM(price_usd),0) FROM monitor_orders WHERE status='deployed'"
        ).fetchone()
        return float(rows[0])
