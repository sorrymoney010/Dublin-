# Dublin Trading OS — V3 Audit

Branch: `upgrade/coo-live-v3`

## Executive finding

The `main` branch contains a strong paper-first Kraken Spot engine, but it was not a complete live-trading implementation. V3 upgrades the architecture without changing the safe defaults.

## Critical findings from main

1. **Normal live execution was unreachable.** `TradingEngine` constructed `KrakenGateway` without enabling its final `allow_order_submission` gate.
2. **Exit scope could include unrelated wallet holdings.** The legacy `close_position()` derives exposure from the full Kraken base-asset balance. V3 introduces a Dublin-managed position ledger and quantity-scoped exits.
3. **Protective stop logic existed but was not wired into the normal engine cycle.** This remains a merge blocker until the stop path is integrated and tested end-to-end.
4. **Reconciliation was informational rather than authoritative.** A live bot must halt on a shortage between managed quantity and exchange quantity rather than silently continue.
5. **Account-equity fallback is unsafe for live mode.** On a private API failure, the current gateway can fall back to configured strategy equity. In live mode, account/risk data failure must fail closed.
6. **Daily P&L / drawdown accounting is not truly strategy-specific.** Capping account equity with `min(account_equity, strategy_equity_usd)` can mask losses when the Kraken account balance is larger than the bot budget. Live risk accounting needs a Dublin-specific capital ledger.
7. **Market orders have no explicit slippage ceiling.** Spread/liquidity gates reduce risk but do not bound execution slippage after decision time.
8. **The README and safety documentation describe the older paper-only architecture and must be updated before release.**

## V3 changes already implemented

- Added `LIVE_EXECUTION_ARMED` as an explicit final execution gate.
- Added `Settings.live_ready` and stricter live-mode consistency checks.
- Replaced rigid all-filters entry logic with a weighted COO decision score.
- Added volatility-band scoring and configurable COO entry/exit thresholds.
- Added a persistent Dublin-managed position ledger.
- Added quantity-scoped Kraken exits so Dublin does not intentionally liquidate unrelated spot holdings.
- Engine position state is based on Dublin-managed exposure rather than arbitrary wallet BTC.
- Existing default configuration remains paper + dry-run + live-disabled.

## Merge blockers

Do not merge V3 to `main` for unattended real-money operation until all are complete:

- Wire and test the protective stop path in the engine/supervisor.
- Make live account-equity and reconciliation failures fail closed.
- Add a strategy-specific realized/unrealized P&L ledger.
- Add fill reconciliation: managed quantity must be created from confirmed executed volume, not requested volume.
- Add partial-fill handling on entry and exit.
- Add explicit slippage / price-protection policy for marketable orders.
- Extend offline tests for live arming, managed-position shortages, unrelated wallet holdings, partial fills, stop exits, and ambiguous order recovery.
- Update dashboard so live state is visible without exposing secrets and so it does not silently refuse to run solely because live mode is configured.
- Update README / RUNBOOK / SAFETY docs and require a staged canary rollout before unattended operation.

## Recommended release sequence

1. Paper replay with V3 scoring.
2. Kraken read-only shadow mode using real market/account data.
3. Live canary with the minimum valid Kraken notional and one managed symbol.
4. Confirm fills, reconciliation, stop behavior, restart recovery, and audit-chain integrity.
5. Increase the strategy budget only after measured results and failure-free operation.

No strategy guarantees profitability. The purpose of this audit is to make execution, risk accounting, and failure behavior explicit and testable.
