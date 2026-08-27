"""Acurast CEO — command-line entrypoint.

Usage (after ``pip install -e .`` or running from repo root with the venv):

  python -m acurast_ceo.cli seed-demo     # populate an offline demo farm + inventory
  python -m acurast_ceo.cli poll          # one Sentinel observation cycle
  python -m acurast_ceo.cli allocate 100  # $100 capital-allocation recommendation
  python -m acurast_ceo.cli dashboard     # serve the live KPI dashboard on :8891
  python -m acurast_ceo.cli quote         # x402 Deploy Agent price (needs network)

All commands are read-only against Acurast except `quote` (which only GETs a
price) and real-backend polling. Nothing buys, stakes, or deploys without an
explicit, separate instruction.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .allocator import CapitalAllocator, InsufficientData
from .clients import FakeDeployAgent, FakeProcessorBackend, ProcessorBackendClient
from .config import load_settings
from .dashboard import run_dashboard
from .models import PhoneInventory, ProcessorStatus
from .sentinel import FarmSentinel
from .store import open_store


def _setup(seed: bool = False):
    s = load_settings()
    s.ensure_data_dir()
    store = open_store(s.db_file)
    if seed:
        _seed_demo(store, s)
    # Default to the offline fake backend so everything runs without an
    # Acurast account (Phase Zero: use old phones first). Only use the real
    # Processor Management Backend when the operator explicitly points at one
    # (non-default URL via ACURAST_MGMT_BACKEND_URL).
    if s.mgmt_backend_url and s.mgmt_backend_url != "http://localhost:8080":
        backend: Any = ProcessorBackendClient(s.mgmt_backend_url, s.mgmt_api_key)
    else:
        backend = FakeProcessorBackend()
    # Pull fake devices from the seeded store so a demo farm is observable.
    if isinstance(backend, FakeProcessorBackend) and store.all_latest():
        for st in store.all_latest():
            backend.add(st)
    return s, store, backend


def _seed_demo(store, s):
    """Create an offline demo farm: 3 phones (2 Core online, 1 Lite offline) + inventory."""
    now = time.time()
    demo = [
        ProcessorStatus(address="5CoreA1XpQ9…onboarded", last_heartbeat_ts=now - 60,
                        attested=True, battery_pct=92, battery_health="good",
                        temperature=31.5, network_type="wifi", ssid="farm-ap",
                        reputation=0.87, processor_version="android 13 / 3.2.1",
                        deployment_status="", is_core=True, online=True),
        ProcessorStatus(address="5CoreB2LmK7…onboarded", last_heartbeat_ts=now - 120,
                        attested=True, battery_pct=88, battery_health="good",
                        temperature=33.0, network_type="wifi", ssid="farm-ap",
                        reputation=0.83, processor_version="android 13 / 3.2.1",
                        deployment_status="monitor-as-a-service", is_core=True, online=True),
        ProcessorStatus(address="5LiteC3RzW0…onboarded", last_heartbeat_ts=now - 9000,
                        attested=False, battery_pct=40, battery_health="fair",
                        temperature=0.0, network_type="", ssid="",
                        reputation=0.5, processor_version="android 12 / 3.1.0",
                        deployment_status="", is_core=False, online=False),
    ]
    for d in demo:
        store.record_status(d)
    inv = [
        PhoneInventory(device_id="Phone 01", purchase_cost=0.0, cpu="Snapdragon 8 Gen1",
                       ram_gb=8, android_version="13", core_eligible=True,
                       benchmark_score=842, uptime_pct=99.2, acu_earned=3.41, usd_per_day=0.41),
        PhoneInventory(device_id="Phone 02", purchase_cost=0.0, cpu="Dimensity 9000",
                       ram_gb=12, android_version="13", core_eligible=True,
                       benchmark_score=910, uptime_pct=98.7, acu_earned=3.92, usd_per_day=0.47),
        PhoneInventory(device_id="Phone 03", purchase_cost=0.0, cpu="old midrange",
                       ram_gb=4, android_version="12", core_eligible=False,
                       benchmark_score=410, uptime_pct=61.0, acu_earned=0.30, usd_per_day=0.04),
    ]
    for i in inv:
        store.upsert_inventory(i)


def cmd_seed_demo():
    s, store, _ = _setup(seed=True)
    print("Seeded demo farm (3 phones, 3 inventory rows) into", s.db_file)


def cmd_poll():
    s, store, backend = _setup(seed=True)
    sent = FarmSentinel(s, store, backend)
    snap = sent.poll_once()
    print(f"Poll OK — online {snap.phones_online}/{snap.total_phones}, "
          f"ACU(farm)={snap.acu_earned_farm:.3f}, $/phone/day={snap.revenue_per_phone_usd:.3f}, "
          f"payback={snap.payback_months:.1f}mo")
    for a in store.open_alerts():
        print(f"  ALERT [{a['kind']}] {a['message']}")


def cmd_allocate(cap: float):
    s, store, _ = _setup(seed=True)
    alloc = CapitalAllocator(s, store)
    try:
        dec = alloc.decide(cap)
    except InsufficientData as e:
        print(f"Cannot allocate: {e}")
        return
    print(f"\nCapital: ${cap:.2f}  (automated ranking by capital efficiency)")
    print("Rank  Option                              Cost     $/mo     Ret%   Payback")
    for i, o in enumerate(dec.ranked, 1):
        pb = "∞" if o.payback_months == float("inf") else f"{o.payback_months:.1f}mo"
        print(f"  {i}    {o.name[:34]:<34} ${o.cost:>6.0f}  ${o.expected_monthly_return_usd:>6.2f}  "
              f"{o.expected_return_pct:>5.1f}%  {pb}")
    best = dec.best
    print(f"\n→ RECOMMENDED: [{best.key}] {best.name} — {best.rationale}")


def cmd_dashboard():
    s, store, _ = _setup(seed=True)
    run_dashboard(store, s)


def cmd_quote():
    s, _store, _ = _setup()
    from .clients import DeployAgentClient
    a = DeployAgentClient(s.deploy_agent_url)
    try:
        q = a.quote("ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2", "NodeJS", 1)
        print(f"Deploy Agent quote: {q.usdc_price} USDC (Base) for {q.runtime}, "
              f"reward {q.reward_acu} ACU (picoACU={int(q.reward_acu * a.PICO)})")
        print("  -> POST this spec to /deploy; server returns an x402 402 challenge.")
        print("     Sign the USDC payment with an x402 client (Coinbase `awal`) to deploy.")
    except Exception as e:
        print(f"Quote failed (live deploy.acu.run): {e}")


def cmd_deploy(app: str):
    """Bundle an on-processor app, upload to IPFS, print the x402 pay command.

    Steps performed here:
      1. locate src/acurast_ceo/deployments/<app>/<app>.js
      2. `acurast deploy --only-upload` to pin it to IPFS and get the CID
    The printed `awal` command is what actually pays USDC on Base — it needs
    the agentic wallet, so we emit it for you to run (or wire into a backend).
    """
    import json
    import shutil
    import subprocess

    reward_acu = 0.0771  # default per-execution reward
    pico = int(reward_acu * 1_000_000_000_000)

    base = Path(__file__).resolve().parent / "deployments" / app
    script = base / f"{app}.js"
    if not script.exists():
        raise SystemExit(f"No deployment app '{app}'. Look in src/acurast_ceo/deployments/")
    acurast = shutil.which("acurast")
    if not acurast:
        raise SystemExit("Acurast CLI not found on PATH (npm i -g @acurast/cli).")

    print(f"Uploading {script} to IPFS via Acurast CLI...")
    env = dict(__import__("os").environ)
    if not env.get("ACURAST_MNEMONIC"):
        print("\n⚠️  ACURAST_MNEMONIC (your deployer wallet) is not set, so the CLI "
              "cannot pin to IPFS. The live deploy agent REQUIRES a pre-pinned "
              "ipfs:// CID (it does not upload scripts itself).\n")
        print("To deploy for real, either:")
        print("  (a) export ACURAST_MNEMONIC=<your deployer mnemonic> and re-run, OR")
        print("  (b) pin the script yourself (e.g. `acurast deploy --only-upload` "
              "with the mnemonic, or any IPFS pinning service) and note the CID.\n")
        print("Then pay in USDC on Base with the Coinbase agentic wallet:")
        fake_cid = "ipfs://QmYOURPINNEDCID"
        spec = {
            "script": fake_cid, "reward": pico, "runtime": "NodeJS", "slots": 1,
            "allowOnlyVerifiedSources": True,
            "schedule": {"interval": 900000, "duration": 60000, "maxStartDelay": 10000},
        }
        print(f"npx awal@latest x402 pay \"https://deploy.acu.run/deploy\" -X POST -d '{json.dumps(spec)}'")
        print("\nEnv vars per app (set in acurast.json): monitor -> TARGET_URL/ALERT_WEBHOOK; "
              "scanner -> SOURCES/FORWARD_WEBHOOK.")
        return

    # Run the real pin. If the account is unfunded, the CLI prints a faucet
    # link with the deployer address — surface it instead of failing silently.
    proc = subprocess.run([acurast, "deploy", "--only-upload"],
                          cwd=str(base), capture_output=True, text=True, env=env)
    out = proc.stdout + proc.stderr
    import re
    addr = re.search(r"address=([0-9A-Za-z]+)", out)
    if "balance is 0" in out or "Visit" in out and addr:
        print(out.strip())
        if addr:
            print(f"\n→ Claim free testnet (cACU) at: https://faucet.acurast.com?address={addr.group(1)}")
            print("  (canary faucet needs a human captcha — open the link, solve it, re-run this command)")
        return
    cid = None
    for tok in out.replace("\n", " ").split():
        if tok.startswith("ipfs://"):
            cid = tok
            break
    if not cid:
        print("--- acurast output ---")
        print(out)
        raise SystemExit("Could not parse IPFS CID from acurast output.")
    print(f"Pinned: {cid}")

    spec = {
        "script": cid,
        "reward": pico,
        "runtime": "NodeJS",
        "slots": 1,
        "allowOnlyVerifiedSources": True,
        "schedule": {
            "interval": 900000,    # 15 min
            "duration": 60000,     # 60s max exec
            "maxStartDelay": 10000,
        },
    }
    print("\n--- Run this to pay in USDC on Base (needs Coinbase `awal`) ---")
    print(f"npx awal@latest x402 pay \"https://deploy.acu.run/deploy\" -X POST -d '{json.dumps(spec)}'")
    print("\nSet env vars per app via acurast.json before upload (TARGET_URL / ALERT_WEBHOOK for monitor, SOURCES / FORWARD_WEBHOOK for scanner).")


def cmd_monitor_add(customer: str, url: str, webhook: str, price: float = 9.0):
    """Tier 1: take a paying customer, deploy a scoped monitor on Acurast."""
    from .orders import MonitorOrder, OrderStore
    from .saas import add_monitor

    s, store, _ = _setup()
    ostore = OrderStore(str(s.db_file))
    order = MonitorOrder(customer=customer, target_url=url, alert_webhook=webhook, price_usd=price)
    print(f"Deploying monitor for '{customer}' -> {url} (${price}/mo) ...")
    res = add_monitor(order, ostore, s)
    if res.status == "deployed":
        print(f"  ✅ deployed  deployment_id={res.deployment_id}  cid={res.cid}")
        print(f"  monthly recurring revenue now: ${ostore.monthly_revenue():.2f}")
    else:
        print(f"  ❌ {res.status}: {res.note}")


def cmd_monitor_ls():
    from .orders import OrderStore
    s, _store, _ = _setup()
    ostore = OrderStore(str(s.db_file))
    rows = ostore.list()
    if not rows:
        print("no monitor orders yet")
        return
    print(f"{'customer':<20} {'status':<10} {'price':<8} {'deploy_id':<12} url")
    for o in rows:
        print(f"{o.customer:<20} {o.status:<10} ${o.price_usd:<6} {str(o.deployment_id):<12} {o.target_url}")
    print(f"\nMonthly recurring revenue (deployed): ${ostore.monthly_revenue():.2f}")


def cmd_gateway():
    """Tier 2: serve the x402 compute API on :8892."""
    import asyncio
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    from .config import load_settings
    s = load_settings()
    port = int(getattr(s, "gateway_port", 8892))
    from .gateway import build_app
    app = build_app()
    print(f"Serving Acurast Compute API on http://localhost:{port} "
          f"(x402 402 challenges; set ACURAST_AWAL_ENABLED=1 to auto-pay)")
    config = Config()
    config.bind = [f"localhost:{port}"]
    asyncio.run(serve(app, config))


def main(argv=None):
    import sys

    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "dashboard"
    if cmd == "seed-demo":
        cmd_seed_demo()
    elif cmd == "poll":
        cmd_poll()
    elif cmd == "allocate":
        cap = float(args[1]) if len(args) > 1 else 100.0
        cmd_allocate(cap)
    elif cmd == "dashboard":
        cmd_dashboard()
    elif cmd == "quote":
        cmd_quote()
    elif cmd == "deploy":
        app = args[1] if len(args) > 1 else "monitor"
        cmd_deploy(app)
    elif cmd == "monitor-add":
        customer = args[1] if len(args) > 1 else "demo"
        url = args[2] if len(args) > 2 else "https://example.com"
        webhook = args[3] if len(args) > 3 else "https://example.com/webhook"
        price = float(args[4]) if len(args) > 4 else 9.0
        cmd_monitor_add(customer, url, webhook, price)
    elif cmd == "monitor-ls":
        cmd_monitor_ls()
    elif cmd == "gateway":
        cmd_gateway()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
