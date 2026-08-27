"""Acurast CEO — the Acurast Money Machine controller.

Six-engine compute business controller (see the strategy brief):
  Engine 1  Phone Farm          -> inventory + sentinel
  Engine 2  Staked Compute      -> allocator option B
  Engine 3  Keep Phones Busy    -> deployments ledger
  Engine 4  Acurast SaaS        -> deploy agent (Monitor/Research/VPS)
  Engine 5  x402 rail           -> DeployAgentClient
  Engine 6  Compute Reseller    -> sell-side deployment ledger

Plus the Capital Allocation Engine and Farm Sentinel (the first software the
plan says to build).

Public surface is intentionally small; import what you need.
"""
from __future__ import annotations

from .allocator import CapitalAllocator, InsufficientData
from .clients import (
    DeployAgentClient,
    FakeDeployAgent,
    FakeProcessorBackend,
    ProcessorBackendClient,
)
from .config import AcurastSettings, load_settings
from .models import (
    AllocationDecision,
    AllocationOption,
    DeployQuote,
    DeployResult,
    KpiSnapshot,
    PhoneInventory,
    ProcessorStatus,
)
from .sentinel import (
    ALERT_DEPLOYMENT_FAILURE,
    ALERT_DEVICE_OFFLINE,
    ALERT_LOW_BALANCE,
    ALERT_REWARD_DROP,
    ALERT_UNUSUAL_PERFORMANCE,
    FarmSentinel,
)
from .store import Store, open_store

__all__ = [
    "AcurastSettings",
    "load_settings",
    "Store",
    "open_store",
    "FarmSentinel",
    "CapitalAllocator",
    "InsufficientData",
    "ProcessorBackendClient",
    "FakeProcessorBackend",
    "DeployAgentClient",
    "FakeDeployAgent",
    "PhoneInventory",
    "ProcessorStatus",
    "KpiSnapshot",
    "AllocationOption",
    "AllocationDecision",
    "DeployQuote",
    "DeployResult",
    "ALERT_DEVICE_OFFLINE",
    "ALERT_REWARD_DROP",
    "ALERT_DEPLOYMENT_FAILURE",
    "ALERT_LOW_BALANCE",
    "ALERT_UNUSUAL_PERFORMANCE",
]
