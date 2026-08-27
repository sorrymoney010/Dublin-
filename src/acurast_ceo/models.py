"""Acurast CEO — domain models.

Pure data structures + small helper functions. No I/O here so they are easy
to construct in tests and in the sentinel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Inventory (Phase Zero: benchmark what you own, don't guess) ──
class PhoneInventory(BaseModel):
    """Acurast's "benchmark what you own first" record, per the plan's hardware rule."""

    device_id: str  # e.g. "Phone 01"
    purchase_cost: float = 0.0  # $0 for phones you already own
    cpu: str = ""
    ram_gb: float = 0.0
    android_version: str = ""
    core_eligible: Optional[bool] = None  # Android 12+, 64-bit aarch64, locked bootloader
    benchmark_score: float = 0.0
    uptime_pct: float = 0.0
    acu_earned: float = 0.0
    usd_per_day: float = 0.0
    notes: str = ""


# ── Fleet telemetry (from Processor Management Backend) ──
@dataclass
class ProcessorStatus:
    address: str
    last_heartbeat_ts: float = 0.0
    attested: bool = False
    battery_pct: float = 0.0
    battery_health: str = ""
    temperature: float = 0.0
    network_type: str = ""
    ssid: str = ""
    reputation: float = 0.5  # Hub default 0.5
    processor_version: str = ""
    deployment_status: str = ""
    is_core: bool = False
    online: bool = False

    @property
    def last_seen_minutes(self) -> float:
        if not self.last_heartbeat_ts:
            return float("inf")
        return (datetime.now(timezone.utc).timestamp() - self.last_heartbeat_ts) / 60.0


# ── KPI snapshot (the dashboard table from the plan) ──
@dataclass
class KpiSnapshot:
    timestamp: float
    phones_online: int
    total_phones: int
    uptime_pct: float
    avg_benchmark: float
    jobs_executed: int
    acu_earned_farm: float
    acu_earned_per_device: float
    busy_epochs: int
    stake_per_processor: float
    avg_temp_c: float
    avg_battery_health: float
    power_consumption_w: float
    failed_jobs: int
    avg_reputation: float
    usd_equivalent: float
    revenue_per_phone_usd: float
    payback_months: float
    alerts: list[str] = field(default_factory=list)


# ── Capital allocation option (the $100 decision) ──
@dataclass
class AllocationOption:
    key: str  # A | B | C | D
    name: str
    cost: float
    expected_monthly_return_usd: float
    expected_return_pct: float  # monthly
    payback_months: float
    rationale: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "cost": round(self.cost, 2),
            "expected_monthly_return_usd": round(self.expected_monthly_return_usd, 4),
            "expected_return_pct": round(self.expected_return_pct, 3),
            "payback_months": round(self.payback_months, 2),
            "rationale": self.rationale,
        }


@dataclass
class AllocationDecision:
    best: AllocationOption
    ranked: list[AllocationOption]
    capital: float
    decided_at: float
    automated: bool

    def as_dict(self) -> dict:
        return {
            "capital": self.capital,
            "automated": self.automated,
            "decided_at": self.decided_at,
            "best": self.best.as_dict(),
            "ranked": [o.as_dict() for o in self.ranked],
        }


# ── x402 Deploy Agent quote/order ──
@dataclass
class DeployQuote:
    runtime: str  # NodeJS | NodeJSWithBundle | Shell
    reward_acu: float  # execution reward in ACU
    usdc_price: float  # x402 price in USDC on Base
    ipfs_cid: str = ""
    estimated_seconds: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class DeployResult:
    ok: bool
    job_hash: str = ""
    status: int = 0
    message: str = ""
    raw: dict = field(default_factory=dict)
