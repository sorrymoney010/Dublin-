"""Acurast CEO — configuration.

All settings load from ``ACURAST_``-prefixed environment variables (and ``.env``).
This keeps the module drop-in compatible with the existing trading-OS ``.env``
conventions (see ``dublin_bot.config.Settings``).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Verified Acurast surfaces (see docs.acurast.com). The Processor Management
# Backend REST API is the documented fleet-telemetry endpoint; the Deploy Agent
# is the x402/USDC-on-Base HTTP rail. We keep these as defaults so the code is
# grounded in the real network rather than guesswork.
DEFAULT_MGMT_BACKEND = "http://localhost:8080"  # self-hosted; swap for your backend URL
DEFAULT_DEPLOY_AGENT = "https://deploy.acu.run"
DEFAULT_NETWORK = "mainnet"  # or "canary"


class AcurastSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACURAST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Fleet telemetry source (Processor Management Backend) ──
    mgmt_backend_url: str = DEFAULT_MGMT_BACKEND
    mgmt_api_key: str = ""  # X-Api-Key for telemetry reads (fail-closed backend)
    manager_address: str = ""  # your Acurast Hub manager (SS58) address

    # ── Deploy Agent (x402 / USDC on Base) ──
    deploy_agent_url: str = DEFAULT_DEPLOY_AGENT
    network: str = DEFAULT_NETWORK

    # ── Local data ──
    db_path: str = Field(default="acurast_ceo.db")
    rewards_ledger: str = Field(default="rewards_ledger.csv")  # per-device ACU earned (from Hub)
    data_dir: str = Field(default="acurast_data")

    # ── Funded deploy workspace (holds ACURAST_MNEMONIC; canary testnet) ──
    deploy_dir: str = Field(default="_acurast_deploy")

    # ── x402 API gateway (Tier 2) ──
    gateway_port: int = 8892
    awal_enabled: bool = Field(default=False)  # set ACURAST_AWAL_ENABLED=1 with a funded awal wallet

    # ── Sentinel behaviour ──
    poll_interval_seconds: int = 900  # every 15 minutes, per the plan
    offline_threshold_seconds: int = 5400  # 90 min (3 missed heartbeats @ 30 min)
    low_balance_acu: float = 0.5  # alert when a processor's on-chain ACU balance drops below
    reward_drop_pct: float = 30.0  # % day-over-day drop in ACU earned -> alert
    unusual_perf_pct: float = 40.0  # % benchmark swing -> alert

    # ── Accounting ──
    acu_usd_price: float = 0.12  # volatile; only for USD-equivalent accounting, NEVER ROI math
    power_cost_per_kwh: float = 0.18
    network_cost_per_month: float = 0.0

    # ── Push alerts ──
    ntfy_topic: str = ""  # optional ntfy.sh topic for iPhone push

    @property
    def db_file(self) -> Path:
        return Path(self.data_dir) / self.db_path

    @property
    def ledger_file(self) -> Path:
        return Path(self.data_dir) / self.rewards_ledger

    def ensure_data_dir(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings() -> AcurastSettings:
    return AcurastSettings()
