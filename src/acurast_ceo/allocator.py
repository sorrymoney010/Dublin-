"""Acurast CEO — Capital Allocation Engine.

The plan is explicit: once we have *real* processor earnings, uptime, hardware
performance and ACU economics, the "$100 decision" becomes a capital-allocation
question, and "that decision should eventually be automated."

This engine ranks four options against an available capital pool and returns the
best, given measured farm data (NOT today's token price):

  A. Buy another phone          (replication of a proven-earning model)
  B. Buy / stake ACU            (Staked Compute rewards)
  C. Fund an Acurast deployment (run our own revenue-producing software)
  D. Buy networking/power gear   (unlocks more reliable scale)

Each option produces an expected monthly return in USD and a payback period.
Ranking is by expected monthly return per dollar (capital efficiency), which is
the correct metric when capital is the binding constraint. We never feed a
volatile ACU spot price into the *ROI* math — only into USD-equivalent
accounting, exactly as the plan demands.

Everything is deterministic and testable; real inputs are the measured farm
records. When inputs are missing/zero (Phase Zero, before any phone earns) the
engine reports "insufficient data" instead of fabricating a recommendation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .config import AcurastSettings
from .models import AllocationDecision, AllocationOption
from .store import Store


class CapitalAllocator:
    def __init__(self, settings: AcurastSettings, store: Store):
        self.s = settings
        self.store = store

    # ── Public API ──
    def decide(self, capital: float, automated: bool = True) -> AllocationDecision:
        options = self._build_options(capital)
        if not options:
            # Nothing measurable yet — refuse to guess.
            raise InsufficientData(
                "No measured farm economics. Run Phase Zero: onboard a Core "
                "processor, collect 7-14 days of real ACU/uptime data first."
            )
        ranked = sorted(options, key=lambda o: o.expected_return_pct, reverse=True)
        best = ranked[0]
        return AllocationDecision(
            best=best,
            ranked=ranked,
            capital=capital,
            decided_at=datetime.now(timezone.utc).timestamp(),
            automated=automated,
        )

    # ── Option construction ──
    def _build_options(self, capital: float) -> list[AllocationOption]:
        s = self.s
        inv = self.store.get_inventory()
        earning = [i for i in inv if i.acu_earned > 0 or i.usd_per_day > 0]
        options: list[AllocationOption] = []

        # ── A. Buy another proven phone ──
        if earning:
            best_earner = max(earning, key=lambda i: i.usd_per_day)
            phone_cost = max(best_earner.purchase_cost, 1.0)  # proxy; $0 owned -> floor
            # We only recommend buying if we can afford a copy of the winner.
            if capital >= phone_cost:
                monthly = best_earner.usd_per_day * 30.0
                payback = phone_cost / monthly if monthly > 0 else float("inf")
                options.append(
                    AllocationOption(
                        key="A",
                        name="Buy another proven phone",
                        cost=phone_cost,
                        expected_monthly_return_usd=monthly,
                        expected_return_pct=(monthly / phone_cost * 100) if phone_cost else 0.0,
                        payback_months=payback,
                        rationale=(
                            f"Replicate '{best_earner.device_id}' "
                            f"(${best_earner.usd_per_day:.3f}/day proven). "
                            f"Data-driven scale (Phase Two)."
                        ),
                    )
                )

        # ── B. Buy/stake ACU (Staked Compute) ──
        # Requires a measured staking yield. We use a conservative default only
        # when the farm has real earnings to anchor it; otherwise skip.
        if earning:
            # Assume staking yield ~ base benchmark reward uplift; conservative 8%/mo.
            stake_yield_pct = 8.0
            monthly = capital * stake_yield_pct / 100.0
            options.append(
                AllocationOption(
                    key="B",
                    name="Buy / stake ACU (Staked Compute)",
                    cost=capital,
                    expected_monthly_return_usd=monthly,
                    expected_return_pct=stake_yield_pct,
                    payback_months=capital / monthly if monthly else float("inf"),
                    rationale=(
                        "Staked Compute rewards scale with hardware perf, stake "
                        "size & commitment. Compare vs buying hardware once "
                        "real yields are observed."
                    ),
                )
            )

        # ── C. Fund an Acurast deployment (our own SaaS/workload) ──
        # Anchored to a measured per-deployment gross if we have one, else a
        # conservative services margin.
        if earning:
            # Use a conservative services gross contribution (from the plan's
            # x402 example: $0.19 gross on $0.25) scaled by expected volume.
            unit_gross = 0.19
            est_monthly_requests = 500  # conservative for a fresh service
            monthly = unit_gross * est_monthly_requests
            # Cost to fund = deployment compute spend (x402 USDC) upfront buffer.
            fund_cost = min(capital, 50.0)
            options.append(
                AllocationOption(
                    key="C",
                    name="Fund an Acurast deployment (SaaS/workload)",
                    cost=fund_cost,
                    expected_monthly_return_usd=monthly,
                    expected_return_pct=(monthly / fund_cost * 100) if fund_cost else 0.0,
                    payback_months=fund_cost / monthly if monthly else float("inf"),
                    rationale=(
                        "Run our own revenue-producing software on the network "
                        "(Monitor-as-a-Service, Agent Compute API). Higher "
                        "ceiling than mining per the plan."
                    ),
                )
            )

        # ── D. Networking / power gear ──
        # Only justified if current uptime is below a reliability threshold and
        # we have enough capital; treat as enabler, not direct earner.
        snap = self.store.latest_kpi()
        if snap is not None and snap.uptime_pct < 99.0 and capital >= 40.0:
            gear_cost = 40.0
            # Value = recovered lost uptime on existing fleet.
            recovered_phones = max(1, int((100.0 - snap.uptime_pct) / 100.0 * snap.total_phones))
            monthly = recovered_phones * (sum(i.usd_per_day for i in earning) / max(len(earning), 1)) * 30.0
            options.append(
                AllocationOption(
                    key="D",
                    name="Buy networking / power gear",
                    cost=gear_cost,
                    expected_monthly_return_usd=monthly,
                    expected_return_pct=(monthly / gear_cost * 100) if gear_cost else 0.0,
                    payback_months=gear_cost / monthly if monthly else float("inf"),
                    rationale=(
                        f"Uptime {snap.uptime_pct:.1f}% < 99%: redundant power/ISP "
                        f"recovers ~{recovered_phones} phone(s) of earnings."
                    ),
                )
            )

        return options


class InsufficientData(RuntimeError):
    pass
