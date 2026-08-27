"""Tests for the Acurast CEO module.

Everything runs offline: the Sentinel and Allocation Engine are exercised
against the in-memory SQLite store and the FakeProcessorBackend / FakeDeployAgent,
so no Acurast account or network is required. This proves the money machine's
control layer works before any real phone is bought.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import timezone
from pathlib import Path

import pytest

from acurast_ceo.allocator import CapitalAllocator, InsufficientData
from acurast_ceo.clients import FakeDeployAgent, FakeProcessorBackend
from acurast_ceo.config import AcurastSettings
from acurast_ceo.dashboard import render_dashboard
from acurast_ceo.models import PhoneInventory, ProcessorStatus
from acurast_ceo.sentinel import FarmSentinel, ALERT_DEVICE_OFFLINE
from acurast_ceo.store import Store

TIME_NOW = 1_700_000_000.0  # arbitrary fixed epoch for deterministic tests


@pytest.fixture
def settings(tmp_path) -> AcurastSettings:
    return AcurastSettings(
        data_dir=str(tmp_path),
        db_path="test.db",
        acu_usd_price=0.12,
        poll_interval_seconds=1,
        offline_threshold_seconds=5400,
    )


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _seed(store: Store, online=True):
    now = TIME_NOW
    store.record_status(
        ProcessorStatus(
            address="5CoreA…", last_heartbeat_ts=now - 60 if online else now - 9000,
            attested=True, battery_pct=90, battery_health="good",
            temperature=31.0, network_type="wifi", ssid="ap",
            reputation=0.85, processor_version="android 13", deployment_status="", 
            is_core=True, online=online,
        )
    )
    store.upsert_inventory(
        PhoneInventory(device_id="Phone 01", purchase_cost=0.0, cpu="SD8g1", ram_gb=8,
                       android_version="13", core_eligible=True, benchmark_score=842,
                       uptime_pct=99.2, acu_earned=3.41, usd_per_day=0.41)
    )


def test_store_records_and_reads(store):
    _seed(store)
    assert store.count_online() == 1
    inv = store.get_inventory()
    assert inv[0].device_id == "Phone 01"
    assert inv[0].usd_per_day == 0.41


def test_sentinel_poll_online_no_false_alert(store, settings):
    _seed(store, online=True)
    backend = FakeProcessorBackend(devices=list(store.all_latest()))
    sent = FarmSentinel(settings, store, backend)
    snap = sent.poll_once()
    assert snap.phones_online == 1
    assert snap.total_phones == 1
    assert snap.acu_earned_farm == 3.41
    assert store.open_alerts() == []  # healthy -> no alerts


def test_sentinel_detects_offline_and_alerts(store, settings):
    _seed(store, online=False)
    backend = FakeProcessorBackend(devices=list(store.all_latest()))
    sent = FarmSentinel(settings, store, backend)
    sent.poll_once()
    alerts = store.open_alerts()
    assert any(a["kind"] == ALERT_DEVICE_OFFLINE for a in alerts)


def test_sentinel_unusual_performance_alert(store, settings):
    _seed(store, online=True)
    # first snapshot
    backend = FakeProcessorBackend(devices=list(store.all_latest()))
    sent = FarmSentinel(settings, store, backend)
    sent.poll_once()
    # simulate a big benchmark swing via inventory
    inv = store.get_inventory()[0]
    inv.benchmark_score = 200.0
    store.upsert_inventory(inv)
    sent.poll_once()
    kinds = {a["kind"] for a in store.open_alerts()}
    assert "UNUSUAL_PERFORMANCE" in kinds


def test_allocator_recommends_with_data(store, settings):
    _seed(store, online=True)
    store.record_kpi(_make_snapshot(uptime=99.5, total=1, usd_per_day=0.41))
    alloc = CapitalAllocator(settings, store)
    dec = alloc.decide(100.0)
    assert dec.best is not None
    assert dec.best.key in {"A", "B", "C", "D"}
    # Option A should be present and recommend buying a proven phone
    keys = {o.key for o in dec.ranked}
    assert "A" in keys and "C" in keys


def test_allocator_refuses_without_data(store, settings):
    alloc = CapitalAllocator(settings, store)
    with pytest.raises(InsufficientData):
        alloc.decide(100.0)


def test_deploy_agent_fake_quote_and_pay():
    agent = FakeDeployAgent(base_usdc=0.08)
    q = agent.quote("ipfs://QmTest", "NodeJS", 1)
    assert q.usdc_price == 0.08
    r = agent.pay("ipfs://QmTest", "NodeJS", 1)
    assert r.ok and r.job_hash.startswith("0xFAKE")


def test_dashboard_renders(store, settings):
    _seed(store, online=True)
    backend = FakeProcessorBackend(devices=list(store.all_latest()))
    FarmSentinel(settings, store, backend).poll_once()
    html = render_dashboard(store, settings)
    assert "Acurast CEO" in html
    assert "Phone 01" in html
    assert "DEVICE OFFLINE" not in html  # healthy farm


def _make_snapshot(uptime=99.0, total=1, usd_per_day=0.41):
    from acurast_ceo.models import KpiSnapshot

    return KpiSnapshot(
        timestamp=TIME_NOW, phones_online=total, total_phones=total,
        uptime_pct=uptime, avg_benchmark=842, jobs_executed=1, acu_earned_farm=3.41,
        acu_earned_per_device=3.41, busy_epochs=0, stake_per_processor=0.0,
        avg_temp_c=31.0, avg_battery_health=0.0, power_consumption_w=5.0,
        failed_jobs=0, avg_reputation=0.85, usd_equivalent=0.41, 
        revenue_per_phone_usd=usd_per_day, payback_months=0.0,
    )


# ── Tier 1: Monitor-as-a-Service orders ──
def test_order_store_records_and_revenue(tmp_path):
    from acurast_ceo.orders import MonitorOrder, OrderStore

    os_ = OrderStore(str(tmp_path / "o.db"))
    o = MonitorOrder(customer="acme", target_url="https://acme.com",
                     alert_webhook="https://hooks.acme.com/x", price_usd=15.0,
                     status="deployed", deployment_id=380553)
    os_.add(o)
    os_.add(MonitorOrder(customer="beta", target_url="https://beta.io",
                         alert_webhook="https://h.io", price_usd=9.0, status="pending"))
    rows = os_.list()
    assert len(rows) == 2
    assert os_.monthly_revenue() == 15.0  # only deployed counts


# ── Tier 2: x402 compute gateway ──
def test_gateway_health_and_quote():
    from acurast_ceo.gateway import build_app, MARGINS
    from acurast_ceo.clients import FakeDeployAgent

    app = build_app(agent=FakeDeployAgent(base_usdc=0.024), pay_fn=lambda spec: {"ok": True})
    from fastapi.testclient import TestClient

    c = TestClient(app)
    assert c.get("/health").json()["status"] == "ok"
    q = c.post("/quote/research").json()
    assert q["asset"] == "USDC"
    assert q["sell_usdc"] == round(q["compute_usdc"] + MARGINS["research"], 4)


def test_gateway_returns_402_challenge_without_awal():
    from acurast_ceo.gateway import build_app
    from acurast_ceo.clients import FakeDeployAgent
    from fastapi.testclient import TestClient

    # pay_fn raises (no wallet) -> endpoint returns 402 Payment Required
    def no_pay(spec):
        raise RuntimeError("awal not enabled")

    app = build_app(agent=FakeDeployAgent(), pay_fn=no_pay)
    c = TestClient(app)
    r = c.post("/research", json={"query": "find roofing leads"})
    assert r.status_code == 402
    body = r.json()
    assert body["detail"]["error"] == "Payment Required"
    assert body["detail"]["x402"]["product"] == "research"
    assert body["detail"]["x402"]["asset"] == "USDC"

