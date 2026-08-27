"""Acurast CEO — Acurast network clients.

Two surfaces are exercised:

1. **Processor Management Backend** (REST) — the documented self-hosted fleet
   telemetry endpoint (github.com/Acurast/acurast-processor-management-backend).
   Exposes per-processor status, history and telemetry. Reads require
   ``X-Api-Key`` (fail-closed). Used by the Sentinel.

2. **Deploy Agent** (x402 / USDC on Base, ``deploy.acu.run``) — the
   agent-facing rail. We implement ``quote`` (GET price) and ``pay`` (POST x402
   payment + job spec). Real HTTP via ``requests``; offline behaviour via the
   injected ``http`` transport so tests need no network.

A ``FakeProcessorBackend`` is provided so the whole CEO stack runs offline
(end-to-end demo with no Acurast account yet) — exactly the "use old phones
first, don't buy yet" posture.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

from .models import DeployQuote, DeployResult, ProcessorStatus


# ── Transport plug (so we can fake HTTP in tests) ──
# Signature: (method, url, json_body, headers) -> (status_code, body_dict, response_headers_dict)
HttpFn = Callable[[str, str, Optional[dict], Optional[dict]], tuple[int, dict, dict]]


def _real_http(method: str, url: str, json_body=None, headers=None):  # pragma: no cover
    import base64

    data = json.dumps(json_body).encode() if json_body is not None else None
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.status
            raw = resp.read().decode("utf-8", "replace")
            resp_headers = {k.lower(): v for k, v in dict(resp.headers).items()}
    except urllib.error.HTTPError as e:
        code = e.code
        raw = e.read().decode("utf-8", "replace")
        resp_headers = {k.lower(): v for k, v in dict(e.headers).items()}
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {"text": raw}
    # x402 challenge (if any) rides in the `payment-required` header as
    # base64-encoded JSON; decode it onto the body so callers can read pricing.
    chall = resp_headers.get("payment-required")
    if chall and isinstance(body, dict):
        try:
            decoded = json.loads(base64.b64decode(chall).decode("utf-8", "replace"))
            body["x402"] = decoded
        except Exception:
            pass
    return code, body, resp_headers


class ProcessorBackendClient:
    """Talks to a self-hosted Acurast Processor Management Backend."""

    def __init__(self, base_url: str, api_key: str = "", http: Optional[HttpFn] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = http or _real_http

    def _get(self, path: str, headers: Optional[dict] = None) -> tuple[int, dict]:
        h = dict(headers or {})
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        code, body, _ = self._http("GET", f"{self.base_url}{path}", None, h)
        return code, body

    def processor_status(self, address: str) -> Optional[ProcessorStatus]:
        code, body = self._get(f"/processor/api/{address}/status")
        if code != 200 or "processorStatus" not in body:
            return None
        return _status_from_body(address, body["processorStatus"])

    def telemetry(self, address: Optional[str] = None) -> list[dict]:
        path = f"/processor/api/{address}/telemetry" if address else "/processor/api/telemetry"
        code, body = self._get(path)
        if code != 200:
            return []
        return body.get("reports", [])

    def list_processors(self) -> list[str]:
        code, body = self._get("/processor/api/telemetry")
        if code != 200:
            return []
        return [r.get("processorAddress", "") for r in body.get("reports", [])]


def _status_from_body(address: str, s: dict) -> ProcessorStatus:
    battery = s.get("battery", {}) or {}
    temps = s.get("temperatures", {}) or {}
    last_hb = float(s.get("lastHeartbeat", 0) or 0)
    now = __import__("time").time()
    online = (now - last_hb) < 5400 if last_hb else False
    return ProcessorStatus(
        address=address,
        last_heartbeat_ts=last_hb,
        attested=bool(s.get("attested", False)),
        battery_pct=float(battery.get("percentage", 0) or 0),
        battery_health=str(battery.get("health", "") or ""),
        temperature=float(temps.get("battery", 0) or temps.get("ambient", 0) or 0),
        network_type=str(s.get("networkType", "") or ""),
        ssid=str(s.get("ssid", "") or ""),
        reputation=float(s.get("reputation", 0.5) or 0.5),
        processor_version=str(s.get("processorVersion", "") or ""),
        deployment_status=str(s.get("deploymentStatus", "") or ""),
        is_core=bool(s.get("isCore", False)),
        online=online,
    )


class FakeProcessorBackend:
    """Offline fleet for demos/tests. Seed with statuses; returns them on poll."""

    def __init__(self, devices: Optional[list[ProcessorStatus]] = None):
        self.devices: dict[str, ProcessorStatus] = {}
        for d in devices or []:
            self.devices[d.address] = d

    def add(self, status: ProcessorStatus) -> None:
        self.devices[status.address] = status

    def processor_status(self, address: str) -> Optional[ProcessorStatus]:
        return self.devices.get(address)

    def telemetry(self, address: Optional[str] = None) -> list[dict]:
        if address:
            d = self.devices.get(address)
            return [_fake_telemetry(d)] if d else []
        return [_fake_telemetry(d) for d in self.devices.values()]

    def list_processors(self) -> list[str]:
        return list(self.devices.keys())


def _fake_telemetry(d: ProcessorStatus) -> dict:
    return {
        "processorAddress": d.address,
        "battery": {"percentage": d.battery_pct, "health": d.battery_health},
        "temperatures": {"battery": d.temperature},
        "networkType": d.network_type,
        "ssid": d.ssid,
        "lastHeartbeat": d.last_heartbeat_ts,
        "attested": d.attested,
        "reputation": d.reputation,
        "processorVersion": d.processor_version,
        "isCore": d.is_core,
    }


# ── x402 Deploy Agent ──
class DeployAgentClient:
    """Acurast Deploy Agent: x402 / USDC on Base (real contract).

    Endpoint:  POST https://deploy.acu.run/deploy
    Reward:    picoACU (1 ACU = 10**12 picoACU)
    Pricing:   server replies 402 "Payment Required" with an x402 challenge
               (accepts[] amount in picoUSDC on Base, chain 8453). The actual
               USDC payment is signed by an x402 client (Coinbase `awal` /
               `@x402/sdk`), not by this Python library — so `pay()` POSTs the
               job spec and surfaces the 402 challenge for an x402 signer.
    """

    PICO = 1_000_000_000_000  # 10**12 picoACU per ACU

    def __init__(self, base_url: str = "https://deploy.acu.run", http: Optional[HttpFn] = None):
        self.base_url = base_url.rstrip("/")
        self._http = http or _real_http

    def _spec(self, ipfs_cid: str, runtime: str, replicas: int, reward_acu: float) -> dict:
        return {
            "script": ipfs_cid,
            "allowedSources": None,
            "allowOnlyVerifiedSources": True,
            "memory": 0,
            "networkRequests": 0,
            "storage": 0,
            "requiredModules": [],
            "assignmentStrategy": "Single",
            "slots": replicas,
            "reward": int(reward_acu * self.PICO),  # ACU -> picoACU
            "minReputation": 0,
            "runtime": runtime,
        }

    def quote(self, ipfs_cid: str, runtime: str = "NodeJS", replicas: int = 1,
              reward_acu: float = 0.0771) -> DeployQuote:
        """POST the job spec; parse the x402 402 challenge for the USDC price."""
        spec = self._spec(ipfs_cid, runtime, replicas, reward_acu)
        code, raw_body, _ = self._http("POST", f"{self.base_url}/deploy", spec,
                                    {"Content-Type": "application/json"})
        if code != 402:
            raise RuntimeError(f"deploy agent returned {code}, expected 402 x402 challenge: {raw_body}")
        body: dict = raw_body if isinstance(raw_body, dict) else {}
        # x402 price lives in the decoded `payment-required` header challenge.
        chall = body.get("x402") or {}
        accepts_list = chall.get("accepts") or body.get("accepts") or [{}]
        accepts: dict = accepts_list[0] if accepts_list else {}
        amount_pico = int(accepts.get("amount", 0))
        # Acurast/Base USDC is 6 decimals.
        return DeployQuote(
            runtime=runtime,
            reward_acu=reward_acu,
            usdc_price=amount_pico / 1_000_000.0,
            ipfs_cid=ipfs_cid,
            raw=body,
        )

    def pay(self, ipfs_cid: str, runtime: str = "NodeJS", replicas: int = 1,
            reward_acu: float = 0.0, x402_payload: Optional[dict] = None) -> DeployResult:
        """POST the job spec. Returns 200 (success) or 402 (x402 challenge to sign).

        A 402 response carries the x402 payment challenge in ``raw``; sign it
        with an x402 client (Coinbase ``awal``) to complete the USDC payment.
        """
        spec = self._spec(ipfs_cid, runtime, replicas, reward_acu)
        code, body, _ = self._http("POST", f"{self.base_url}/deploy", spec,
                                {"Content-Type": "application/json"})
        if code == 200:
            return DeployResult(
                ok=True,
                job_hash=str(body.get("acurastJobHash", body.get("hash", "")) or ""),
                status=code,
                message=str(body.get("message", "deployed")),
                raw=body,
            )
        if code == 402:
            # Awaiting x402 payment — surface the challenge.
            return DeployResult(ok=False, status=402, message="x402 payment required", raw=body)
        return DeployResult(ok=False, status=code, message=str(body), raw=body)


class FakeDeployAgent:
    """Offline x402 agent: deterministic quote + fake job hash.

    Mirrors the real contract: reward is expressed in ACU (converted to
    picoACU internally) and `quote` returns a USDC price.
    """

    def __init__(self, base_usdc: float = 0.08):
        self.base_usdc = base_usdc
        self.deployed: list[dict] = []

    def quote(self, ipfs_cid: str = "ipfs://QmTest", runtime: str = "NodeJS", replicas: int = 1,
              reward_acu: float = 0.0771) -> DeployQuote:
        return DeployQuote(
            runtime=runtime,
            reward_acu=reward_acu,
            usdc_price=round(self.base_usdc * replicas, 4),
            ipfs_cid=ipfs_cid,
            raw={"fake": True},
        )

    def pay(self, ipfs_cid: str, runtime: str = "NodeJS", replicas: int = 1,
            reward_acu: float = 0.0, x402_payload=None) -> DeployResult:
        job_hash = f"0xFAKE{abs(hash(ipfs_cid + str(replicas))) & 0xffffffff:08x}"
        self.deployed.append({"ipfs": ipfs_cid, "runtime": runtime, "replicas": replicas})
        return DeployResult(ok=True, job_hash=job_hash, status=200, message="deployed (fake)")
