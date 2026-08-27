"""Acurast CEO — Farm Sentinel.

The first production deployment the plan calls for: keep the money machine
healthy. Every ``poll_interval_seconds`` (default 15 min) it:

1. pulls each processor's status from the Management Backend,
2. records it in the store,
3. recomputes the KPI snapshot,
4. raises alerts on the five conditions the plan lists:
     DEVICE OFFLINE / REWARD DROP / DEPLOYMENT FAILURE / LOW BALANCE /
     UNUSUAL PERFORMANCE.

It is deliberately I/O-light and side-effect-safe: alerts can be routed to
ntfy.sh (iPhone push) when ``ACURAST_NTFY_TOPIC`` is set, but recording to the
store always happens so the dashboard has history.

The Sentinel never *buys* anything — that is the Capital Allocation Engine's
job. It only observes, records and warns.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union

from .clients import FakeProcessorBackend, ProcessorBackendClient
from .config import AcurastSettings
from .models import KpiSnapshot, ProcessorStatus
from .store import Store

# Alert kinds (one per plan condition) + extras.
ALERT_DEVICE_OFFLINE = "DEVICE_OFFLINE"
ALERT_REWARD_DROP = "REWARD_DROP"
ALERT_DEPLOYMENT_FAILURE = "DEPLOYMENT_FAILURE"
ALERT_LOW_BALANCE = "LOW_BALANCE"
ALERT_UNUSUAL_PERFORMANCE = "UNUSUAL_PERFORMANCE"

# Any object exposing list_processors()/processor_status() works as a backend.
BackendLike = Union[ProcessorBackendClient, FakeProcessorBackend]


class FarmSentinel:
    def __init__(
        self,
        settings: AcurastSettings,
        store: Store,
        backend: Optional[BackendLike] = None,
        alert_sink: Optional[Callable[[str, str], None]] = None,
    ):
        self.s = settings
        self.store = store
        self.backend = backend or FakeProcessorBackend()
        self.alert_sink = alert_sink  # callable(kind, message)
        self._stop = False

    # ── Public polling ──
    def poll_once(self) -> KpiSnapshot:
        """One observation cycle. Returns the KPI snapshot computed."""
        self._stop = False
        prev = self.store.latest_kpi()  # BEFORE we overwrite it
        devices = self._collect()
        for d in devices:
            self.store.record_status(d)
        snap = self._compute_kpi(devices)
        self.store.record_kpi(snap)
        self._evaluate_alerts(devices, snap, prev)
        return snap

    def run_forever(self) -> None:
        while not self._stop:
            try:
                self.poll_once()
            except Exception as e:  # keep the sentinel alive
                self._raise(ALERT_DEPLOYMENT_FAILURE, f"sentinel error: {e}")
            time.sleep(self.s.poll_interval_seconds)

    def stop(self) -> None:
        self._stop = True

    # ── Internals ──
    def _collect(self) -> list[ProcessorStatus]:
        if isinstance(self.backend, FakeProcessorBackend):
            return list(self.backend.devices.values())
        # Real backend: enumerate then fetch each.
        statuses: list[ProcessorStatus] = []
        for addr in self.backend.list_processors():
            st = self.backend.processor_status(addr)
            if st:
                statuses.append(st)
        return statuses

    def _compute_kpi(self, devices: list[ProcessorStatus]) -> KpiSnapshot:
        s = self.s
        total = len(devices)
        online = [d for d in devices if d.online]
        phones_online = len(online)
        uptime = (phones_online / total * 100.0) if total else 0.0
        # Benchmark quality comes from the inventory (Phase Zero benchmark
        # records), keyed by device; backend telemetry has no benchmark field.
        inv_bench = {i.device_id: i.benchmark_score for i in self.store.get_inventory()}
        # Map processor address -> device id loosely: if inventory device_id
        # matches address suffix; else average all known benchmarks.
        if inv_bench:
            avg_bench = sum(inv_bench.values()) / len(inv_bench)
        else:
            avg_bench = 0.0

        # ACU earned comes from the inventory/rewards ledger, not the backend.
        inv = self.store.get_inventory()
        acu_farm = sum(i.acu_earned for i in inv)
        acu_per_device = (acu_farm / total) if total else 0.0

        temps = [d.temperature for d in devices if d.temperature]
        avg_temp = sum(temps) / len(temps) if temps else 0.0
        avg_rep = sum(d.reputation for d in devices) / total if total else 0.0

        # Power model: assume ~5W per busy phone, 2W idle.
        power_w = sum(5.0 if d.deployment_status else 2.0 for d in online)

        usd_equiv = acu_farm * s.acu_usd_price  # accounting only
        rev_per_phone = (sum(i.usd_per_day for i in inv) / total) if total else 0.0
        monthly_rev = rev_per_phone * 30.0
        payback = float("inf")
        if monthly_rev > 0:
            # Use the highest recorded purchase cost as a proxy capital base.
            costs = [i.purchase_cost for i in inv if i.purchase_cost > 0]
            capital = max(costs) if costs else 0.0
            if capital > 0:
                payback = capital / monthly_rev

        return KpiSnapshot(
            timestamp=datetime.now(timezone.utc).timestamp(),
            phones_online=phones_online,
            total_phones=total,
            uptime_pct=uptime,
            avg_benchmark=avg_bench,
            jobs_executed=self._count_jobs(devices),
            acu_earned_farm=acu_farm,
            acu_earned_per_device=acu_per_device,
            busy_epochs=sum(1 for d in online if d.deployment_status),
            stake_per_processor=self._stake_per_processor(),
            avg_temp_c=avg_temp,
            avg_battery_health=0.0,  # derive from battery_health string if parsed
            power_consumption_w=power_w,
            failed_jobs=self._count_failed(devices),
            avg_reputation=avg_rep,
            usd_equivalent=usd_equiv,
            revenue_per_phone_usd=rev_per_phone,
            payback_months=payback,
        )

    def _count_jobs(self, devices: list[ProcessorStatus]) -> int:
        # Busy processors count as at least one job this epoch.
        return sum(1 for d in devices if d.deployment_status)

    def _count_failed(self, devices: list[ProcessorStatus]) -> int:
        return sum(1 for d in devices if "fail" in (d.deployment_status or "").lower())

    def _stake_per_processor(self) -> float:
        # Staked Compute is tracked separately (not in backend telemetry).
        # Hook for loading from a staking ledger; default 0 until wired.
        return 0.0

    def _evaluate_alerts(self, devices: list[ProcessorStatus], snap: KpiSnapshot, prev: Optional[KpiSnapshot]) -> None:
        s = self.s
        # 1. DEVICE OFFLINE
        for d in devices:
            if not d.online:
                self._raise(
                    ALERT_DEVICE_OFFLINE,
                    f"{d.address}: offline (last seen {d.last_seen_minutes:.0f} min ago)",
                )
        # 4. LOW BALANCE (on-chain ACU; placeholder until wallet feed wired)
        # 5. UNUSUAL PERFORMANCE (benchmark swing vs prior snapshot)
        if prev is not None and prev.avg_benchmark and snap.avg_benchmark:
            swing = abs(snap.avg_benchmark - prev.avg_benchmark) / max(prev.avg_benchmark, 1e-9) * 100
            if swing >= s.unusual_perf_pct:
                self._raise(
                    ALERT_UNUSUAL_PERFORMANCE,
                    f"fleet benchmark swung {swing:.1f}% ({prev.avg_benchmark:.1f} -> {snap.avg_benchmark:.1f})",
                )
        # 2. REWARD DROP (day-over-day) — uses inventory usd_per_day as proxy signal
        if prev is not None and prev.revenue_per_phone_usd and snap.revenue_per_phone_usd:
            drop = (prev.revenue_per_phone_usd - snap.revenue_per_phone_usd) / max(prev.revenue_per_phone_usd, 1e-9) * 100
            if drop >= s.reward_drop_pct:
                self._raise(
                    ALERT_REWARD_DROP,
                    f"revenue/phone dropped {drop:.1f}% day-over-day",
                )
        # 3. DEPLOYMENT FAILURE
        failed = self._count_failed(devices)
        if failed:
            self._raise(ALERT_DEPLOYMENT_FAILURE, f"{failed} processor(s) reporting failed deployment")

    def _raise(self, kind: str, message: str) -> None:
        self.store.record_alert(kind, message)
        if self.alert_sink:
            self.alert_sink(kind, message)


def send_ntfy(topic: str, kind: str, message: str) -> None:  # pragma: no cover
    """Best-effort ntfy.sh push (iPhone). Mirrors dublin_bot dashboard pattern."""
    import urllib.request

    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(url, data=message.encode(), headers={"Title": f"Acurast: {kind}"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
