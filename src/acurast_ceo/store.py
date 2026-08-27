"""Acurast CEO — local SQLite store.

Holds the time-series the Sentinel and dashboard need:
- processor status history (heartbeats, battery, temp, reputation)
- KPI snapshots
- alerts
- phone inventory (Phase Zero benchmark records)
- deployment ledger (x402 deployments we run or sell)

Stdlib ``sqlite3`` only — no extra dependency. Schema is created on first use.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import KpiSnapshot, PhoneInventory, ProcessorStatus


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = __import__("threading").Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS processor_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                address TEXT NOT NULL,
                last_heartbeat_ts REAL,
                attested INTEGER,
                battery_pct REAL,
                battery_health TEXT,
                temperature REAL,
                network_type TEXT,
                ssid TEXT,
                reputation REAL,
                processor_version TEXT,
                deployment_status TEXT,
                is_core INTEGER,
                online INTEGER
            );
            CREATE TABLE IF NOT EXISTS kpi_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                addressed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS inventory (
                device_id TEXT PRIMARY KEY,
                purchase_cost REAL,
                cpu TEXT,
                ram_gb REAL,
                android_version TEXT,
                core_eligible INTEGER,
                benchmark_score REAL,
                uptime_pct REAL,
                acu_earned REAL,
                usd_per_day REAL,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS deployment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,            -- run | sell | x402
                job_hash TEXT,
                runtime TEXT,
                reward_acu REAL,
                usdc_price REAL,
                cost_usd REAL,
                revenue_usd REAL,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_proc_ts ON processor_status(address, ts);
            CREATE INDEX IF NOT EXISTS idx_kpi_ts ON kpi_snapshot(ts);
            """
        )
        self._conn.commit()


    # ── Processor status ──
    def record_status(self, status: ProcessorStatus) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO processor_status
               (ts, address, last_heartbeat_ts, attested, battery_pct, battery_health,
                temperature, network_type, ssid, reputation, processor_version,
                deployment_status, is_core, online)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _ts(),
                    status.address,
                    status.last_heartbeat_ts,
                    int(status.attested),
                    status.battery_pct,
                    status.battery_health,
                    status.temperature,
                    status.network_type,
                    status.ssid,
                    status.reputation,
                    status.processor_version,
                    status.deployment_status,
                    int(status.is_core),
                    int(status.online),
                ),
            )
            self._conn.commit()

    def latest_status(self, address: str) -> Optional[ProcessorStatus]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM processor_status WHERE address=? ORDER BY ts DESC LIMIT 1",
                (address,),
            ).fetchone()
        return self._row_to_status(row) if row else None

    def all_latest(self) -> list[ProcessorStatus]:
        # latest row per address
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.* FROM processor_status s
               JOIN (SELECT address, MAX(ts) AS mt FROM processor_status GROUP BY address) m
               ON s.address=m.address AND s.ts=m.mt"""
            ).fetchall()
        return [self._row_to_status(r) for r in rows]

    def count_online(self) -> int:
        return sum(1 for s in self.all_latest() if s.online)

    @staticmethod
    def _row_to_status(row: sqlite3.Row) -> ProcessorStatus:
        return ProcessorStatus(
            address=row["address"],
            last_heartbeat_ts=row["last_heartbeat_ts"] or 0.0,
            attested=bool(row["attested"]),
            battery_pct=row["battery_pct"] or 0.0,
            battery_health=row["battery_health"] or "",
            temperature=row["temperature"] or 0.0,
            network_type=row["network_type"] or "",
            ssid=row["ssid"] or "",
            reputation=row["reputation"] or 0.5,
            processor_version=row["processor_version"] or "",
            deployment_status=row["deployment_status"] or "",
            is_core=bool(row["is_core"]),
            online=bool(row["online"]),
        )

    # ── KPI snapshots ──
    def record_kpi(self, snap: KpiSnapshot) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kpi_snapshot (ts, payload) VALUES (?,?)",
                (snap.timestamp, json.dumps(snap.__dict__)),
            )
            self._conn.commit()

    def latest_kpi(self) -> Optional[KpiSnapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM kpi_snapshot ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return KpiSnapshot(**json.loads(row["payload"]))

    def kpi_history(self, limit: int = 200) -> list[KpiSnapshot]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM kpi_snapshot ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [KpiSnapshot(**json.loads(r["payload"])) for r in rows]

    # ── Alerts ──
    def record_alert(self, kind: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO alert (ts, kind, message) VALUES (?,?,?)",
                (_ts(), kind, message),
            )
            self._conn.commit()

    def open_alerts(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alert WHERE addressed=0 ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close_alert(self, alert_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE alert SET addressed=1 WHERE id=?", (alert_id,))
            self._conn.commit()

    # ── Inventory ──
    def upsert_inventory(self, inv: PhoneInventory) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO inventory
               (device_id, purchase_cost, cpu, ram_gb, android_version, core_eligible,
                benchmark_score, uptime_pct, acu_earned, usd_per_day, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
               purchase_cost=excluded.purchase_cost, cpu=excluded.cpu,
               ram_gb=excluded.ram_gb, android_version=excluded.android_version,
               core_eligible=excluded.core_eligible, benchmark_score=excluded.benchmark_score,
               uptime_pct=excluded.uptime_pct, acu_earned=excluded.acu_earned,
               usd_per_day=excluded.usd_per_day, notes=excluded.notes""",
                (
                    inv.device_id,
                    inv.purchase_cost,
                    inv.cpu,
                    inv.ram_gb,
                    inv.android_version,
                    None if inv.core_eligible is None else int(inv.core_eligible),
                    inv.benchmark_score,
                    inv.uptime_pct,
                    inv.acu_earned,
                    inv.usd_per_day,
                    inv.notes,
                ),
            )
            self._conn.commit()

    def get_inventory(self) -> list[PhoneInventory]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM inventory ORDER BY device_id").fetchall()
        return [
            PhoneInventory(
                device_id=r["device_id"],
                purchase_cost=r["purchase_cost"],
                cpu=r["cpu"],
                ram_gb=r["ram_gb"],
                android_version=r["android_version"],
                core_eligible=None if r["core_eligible"] is None else bool(r["core_eligible"]),
                benchmark_score=r["benchmark_score"],
                uptime_pct=r["uptime_pct"],
                acu_earned=r["acu_earned"],
                usd_per_day=r["usd_per_day"],
                notes=r["notes"],
            )
            for r in rows
        ]

    # ── Deployments ──
    def record_deployment(
        self,
        kind: str,
        job_hash: str = "",
        runtime: str = "",
        reward_acu: float = 0.0,
        usdc_price: float = 0.0,
        cost_usd: float = 0.0,
        revenue_usd: float = 0.0,
        note: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO deployment
               (ts, kind, job_hash, runtime, reward_acu, usdc_price, cost_usd, revenue_usd, note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
                (_ts(), kind, job_hash, runtime, reward_acu, usdc_price, cost_usd, revenue_usd, note),
            )
            self._conn.commit()

    def deployments(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM deployment ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_store(db_path: str | Path) -> Store:
    return Store(db_path)
