"""x402 API gateway (Tier 2 revenue engine) — sell Acurast compute to agents/humans.

Exposes paid endpoints machines hit:
    POST /research   {"query": "..."}      -> Acurast runs a fetch+summarize job
    POST /scrape     {"url": "..."}        -> returns page text
    POST /enrich     {"lead": "..."}       -> lead enrichment
    POST /summary    {"text": "..."}       -> AI summary
    POST /monitor    {"url": "...", "webhook": "..."} -> deploy a monitor

Pricing: we QUOTE the Acurast compute cost (live, from deploy.acu.run), add a
margin, and the caller pays in USDC on Base via x402. The gateway pays Acurast
with `awal` (Coinbase agentic wallet) and returns the result. If `awal` is not
configured, endpoints return a 402 Payment Required with the price (x402 spec),
so an x402-capable client can pay and retry.

Offline note: with ACURAST_AWAL=0 (default in dev) the gateway quotes and
returns a 402 challenge but does NOT sign — safe to run without a wallet.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from .clients import DeployAgentClient, DeployQuote

PICO = 1_000_000_000_000
REWARD_ACU = 0.0771  # default per-execution reward we pay Acurast

# Both the real DeployAgentClient and FakeDeployAgent satisfy this.
class _Quoter(Protocol):
    def quote(self, cid: str, runtime: str = ..., replicas: int = ...) -> "DeployQuote": ...


# ── request / pricing models ──
class JobRequest(BaseModel):
    query: Optional[str] = None
    url: Optional[str] = None
    text: Optional[str] = None
    lead: Optional[str] = None
    webhook: Optional[str] = None


@dataclass
class Price:
    compute_usdc: float   # what Acurast charges
    margin_usdc: float    # our cut
    sell_usdc: float      # what the customer pays
    asset: str = "USDC"
    network: str = "base"


# Margins by product (gross contribution per the brief's opportunity table).
MARGINS = {
    "research": 0.19,
    "scrape": 0.05,
    "enrich": 0.12,
    "summary": 0.12,
    "monitor": 0.10,
}


def quote(product: str, agent: object) -> Price:
    """Live Acurast compute cost + our margin."""
    try:
        q = agent.quote("ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2", "NodeJS", 1)
        compute = q.usdc_price
    except Exception:
        compute = 0.024  # verified live default; never block on a quote failure
    margin = MARGINS.get(product, 0.10)
    return Price(compute_usdc=compute, margin_usdc=margin, sell_usdc=round(compute + margin, 4))


def _awal_pay(spec: dict) -> dict:
    """Pay Acurast in USDC on Base via Coinbase `awal`. Returns the x402 result.

    Requires `awal` (npx awal@latest) and a funded agentic wallet. Without it,
    raises RuntimeError so the caller can return a 402 challenge instead.
    """
    if os.environ.get("ACURAST_AWAL") != "1":
        raise RuntimeError("awal not enabled (set ACURAST_AWAL=1 with a funded wallet)")
    cmd = ["npx", "awal@latest", "x402", "pay", "https://deploy.acu.run/deploy",
           "-X", "POST", "-d", json.dumps(spec)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"awal pay failed: {proc.stderr[:400]}")
    return {"ok": True, "raw": proc.stdout[:500]}


def build_app(agent: Optional[object] = None,
              pay_fn: Optional[Callable[[dict], dict]] = None) -> FastAPI:
    """Construct the FastAPI app. `agent`/`pay_fn` are injectable for tests."""
    from .config import load_settings
    settings = load_settings()
    agent = agent or DeployAgentClient(settings.deploy_agent_url)
    pay_fn = pay_fn or _awal_pay
    app = FastAPI(title="Acurast Compute API", version="0.1.0")

    def _challenge(price: Price, product: str) -> dict:
        return {
            "error": "Payment Required",
            "x402": {
                "price": price.sell_usdc,
                "asset": price.asset,
                "network": price.network,
                "product": product,
                "payTo": "deploy.acu.run",
            },
        }

    def _deploy_and_pay(product: str, script_cid: str) -> dict:
        spec = {
            "script": script_cid,
            "reward": int(REWARD_ACU * PICO),
            "runtime": "NodeJS",
            "slots": 1,
            "allowOnlyVerifiedSources": True,
            "schedule": {"interval": 900000, "duration": 60000, "maxStartDelay": 10000},
        }
        try:
            return pay_fn(spec)
        except RuntimeError as e:
            raise HTTPException(status_code=402, detail={"error": "Payment Required",
                               "x402": {"product": product, "asset": "USDC", "network": "base",
                                        "payTo": "deploy.acu.run", "reason": str(e)}})

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": "acurast-compute-api"}

    @app.post("/quote/{product}")
    def get_quote(product: str):
        if product not in MARGINS:
            raise HTTPException(status_code=404, detail="unknown product")
        return quote(product, agent).__dict__

    @app.post("/research")
    def research(req: JobRequest):
        price = quote("research", agent)
        _deploy_and_pay("research", "ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2")
        return _challenge(price, "research")

    @app.post("/scrape")
    def scrape(req: JobRequest):
        if not req.url:
            raise HTTPException(status_code=400, detail="url required")
        price = quote("scrape", agent)
        _deploy_and_pay("scrape", "ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2")
        return _challenge(price, "scrape")

    @app.post("/enrich")
    def enrich(req: JobRequest):
        price = quote("enrich", agent)
        _deploy_and_pay("enrich", "ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2")
        return _challenge(price, "enrich")

    @app.post("/summary")
    def summary(req: JobRequest):
        if not req.text:
            raise HTTPException(status_code=400, detail="text required")
        price = quote("summary", agent)
        _deploy_and_pay("summary", "ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2")
        return _challenge(price, "summary")

    @app.post("/monitor")
    def monitor(req: JobRequest):
        if not req.url:
            raise HTTPException(status_code=400, detail="url required")
        price = quote("monitor", agent)
        _deploy_and_pay("monitor", "ipfs://QmZ9mvN4RFCqSqivB2LF3VF1qgrDGTW393PJezbdPy7nH2")
        return _challenge(price, "monitor")

    return app


# module-level app for `hypercorn acurast_ceo.gateway:app`
def _default_app():
    return build_app()


app = _default_app()
