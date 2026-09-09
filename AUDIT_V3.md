# Dublin Trading OS — V3 Audit

Branch: `upgrade/coo-live-v3`

## Executive finding

The `main` branch contains a strong paper-first Kraken Spot engine, but it was not a complete live-trading implementation. V3 upgrades the architecture without changing the safe defaults.

## Critical findings from main

1. **Normal live execution was unreachable.** `TradingEngine` constructed `KrakenGateway` without enabling its final `allow_order_submission` gate.
2. **Exit scope could include unrelated wallet holdings.** The legacy `close_position()` derives exposure from the full Kraken base-asset balance. V3 introduces a Dublin-managed position ledger and quantity-scoped exits.
3. **Protective stop logic existed but was not wired into the normal engine cycle.** The engine now checks the persisted stop before candle/entry gates; independent supervision remains a release blocker.
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

- Add independent stop supervision or exchange-native protection; the integrated engine stop only runs when a cycle is invoked.
- Validate stop availability during private-account outages: the engine deliberately blocks all orders when account data cannot be verified.
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

## Follow-up enforcement audit (2026-09-09)

Implemented and tested offline:

- Evaluate `StopMonitor` before candle fetching, freshness, market quality, or COO entry evaluation. At or below the stop, a managed sell bypasses entry-only gates and daily entry limits. Safety locks, authoritative balance checks, and account-equity validation still apply.
- Reject non-finite, negative, malformed, missing, or unavailable required account data in live mode, including configured live mode with the final execution arm off. An absent base-asset key in an otherwise valid balance response means zero holdings.
- Block on any managed-position shortage, recheck the balance immediately before live submission, and retain the managed ledger when an exit fails.
- Reject corrupt, invalid, or wrong-symbol managed ledgers instead of treating them as an empty position.
- Isolate paper reconciliation from real wallet balances. Simulated exposure does not imply an exchange holding.
- Correct the outdated exit-test mock, isolate each test's runtime files, and prohibit real HTTP in the test suite.

Remaining blockers are intentionally unresolved in this patch: confirmed fills and partial fills, ambiguous submission/recovery, strategy-specific P&L, slippage protection, independent stop supervision, and staged operational validation. An AddOrder acknowledgement is still not proof of a completed fill; the current ledger lifecycle must be replaced before unattended trading. The engine's stop is a cycle-time check, not a continuously running or exchange-hosted stop. Account outages halt orders, so they can also prevent a protective exit. Do not merge or enable unattended real-money operation on the strength of offline test results.

Validation of this follow-up: Python 3.11, `ruff check .` clean, full offline suite **204 passed**, explicit safety-lock suite **15 passed**, and `git diff --check` clean. These checks use fake Kraken responses and a test-wide HTTP prohibition. No real orders or operational live configuration were used. GitHub CI must also pass for the pushed commit before review is complete.
