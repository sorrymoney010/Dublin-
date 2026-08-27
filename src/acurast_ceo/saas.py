"""Monitor-as-a-Service deployer (Tier 1 revenue engine).

Given a paying customer's endpoint, scaffold a *scoped* Acurast project and
deploy it with the funded canary wallet. The monitor script (deployments/monitor/
monitor.js) is shared; only the env vars (TARGET_URL / ALERT_WEBHOOK) differ per
customer. This is the "sell monitoring, not mining" loop.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import load_settings
from .orders import MonitorOrder, OrderStore


def _deploy_dir(settings) -> Path:
    d = getattr(settings, "deploy_dir", None) or "_acurast_deploy"
    p = Path(d)
    if not p.is_absolute():
        # resolve relative to repo root (where .env with the mnemonic lives)
        p = Path(__file__).resolve().parent.parent.parent / d
    return p


def add_monitor(order: MonitorOrder, store: OrderStore, settings=None) -> MonitorOrder:
    """Scaffold + deploy a customer's monitor. Returns the order with deploy id."""
    settings = settings or load_settings()
    base = _deploy_dir(settings)
    if not (base / ".env").exists():
        store.update_status(order.customer, order.target_url, "failed",
                             note="funded deploy dir /.env (ACURAST_MNEMONIC) not found")
        return order

    # Scaffold a customer-specific project dir inside the funded deploy workspace.
    proj = base / "customers" / order.customer.replace(" ", "_").replace("/", "_")
    proj.mkdir(parents=True, exist_ok=True)
    script_src = Path(__file__).resolve().parent / "deployments" / "monitor" / "monitor.js"
    shutil.copy(script_src, proj / "monitor.js")

    cfg = {
        "projects": {
            "monitor": {
                "projectName": f"monitor-{order.customer}",
                "fileUrl": "monitor.js",
                "network": "canary",
                "runtime": "NodeJS",
                "onlyAttestedDevices": True,
                "enableDevtools": True,
                "assignmentStrategy": {"type": "Single"},
                "execution": {"type": "onetime", "maxExecutionTimeInMs": 60000},
                "maxAllowedStartDelayInMs": 10000,
                "usageLimit": {"maxMemory": 0, "maxNetworkRequests": 0, "maxStorage": 0},
                "numberOfReplicas": 1,
                "requiredModules": [],
                "minProcessorReputation": 0,
                "maxCostPerExecution": 100000000000,
                "includeEnvironmentVariables": ["TARGET_URL", "ALERT_WEBHOOK", "CHECK_INTERVAL_MS"],
                "processorWhitelist": [],
                "mutability": "Immutable",
            }
        }
    }
    (proj / "acurast.json").write_text(json.dumps(cfg, indent=2))

    # The Acurast CLI reads `.env` (with ACURAST_MNEMONIC) from the cwd, so copy
    # the FUNDED wallet's .env into the customer project dir before deploying.
    shutil.copy(base / ".env", proj / ".env")

    # Append customer env vars to the copied .env (so the mnemonic is preserved
    # and the customer's TARGET_URL/ALERT_WEBHOOK are present).
    env_lines = (
        f"\nTARGET_URL={order.target_url}\n"
        f"ALERT_WEBHOOK={order.alert_webhook}\n"
        f"CHECK_INTERVAL_MS={order.check_interval_ms}\n"
    )
    with open(proj / ".env", "a") as f:
        f.write(env_lines)

    # Deploy non-interactively from the customer project dir (has .env + config).
    proc = subprocess.run(
        ["acurast", "deploy", "-n"],
        cwd=str(proj), capture_output=True, text=True,
        env={**__import__("os").environ},
    )
    out = proc.stdout + proc.stderr
    dep_id = _parse_deployment_id(out)
    cid = _parse_cid(out)
    if dep_id:
        order.deployment_id = dep_id
        order.cid = cid
        order.status = "deployed"
        store.add(order)
    else:
        order.status = "failed"
        order.note = out.strip()[-500:]
        store.add(order)
    return order


def _parse_deployment_id(out: str) -> Optional[int]:
    import re
    m = re.search(r"Deployment registered \(ID:\s*(\d+)\)", out)
    return int(m.group(1)) if m else None


def _parse_cid(out: str) -> Optional[str]:
    for tok in out.replace("\n", " ").split():
        if tok.startswith("ipfs://"):
            return tok
    return None
