# Acurast deployer wallet (CANARY / testnet — free cACU)

Generated locally via `acurast init` (no mainnet funds, no exchange needed).

- Deployer address: `5DFv6VvxSWz1d5UjMRn5Py27oVizmefuqBCRukVZRv7FsCFL`
- Mnemonic: in `.env` (`ACURAST_MNEMONIC`) — DO NOT commit.
- Faucet (claim cACU, needs human captcha):
  https://faucet.acurast.com?address=5DFv6VvxSWz1d5UjMRn5Py27oVizmefuqBCRukVZRv7FsCFL

## FIRST REAL DEPLOY — DONE ✅
Deployed `monitor.js` to a live canary processor:

- Script CID: `ipfs://QmXGMcC2TWF4fSFnA6HpKKgX4Mqwf3pSBm1F5FiWyYbKWt`
- Deployment ID: **380553**
- Flow: `acurast deploy -n`  (non-interactive; auto-accepted suggested price
  0.0026 cACU/exec). Registered → matched → acknowledged 1/1 → env vars set →
  execution scheduled.
- Hub: https://hub.acurast.com/job-detail/acurast-5DFv6VvxSWz1d5UjMRn5Py27oVizmefuqBCRukVZRv7FsCFL-380553
- DevTools URL (printed at deploy; view key time-limited):
  https://devtools.acurast.com/deployment/380553#viewKey=3278b53c6d265c0de458aa3ace558a37909c7dfc50fd3c8ee8f523c6148d853b

## Commands
- Redeploy / new exec: `acurast deploy -n`   (from this dir; .env has mnemonic)
- List: `acurast deployments --network canary`
- View: `acurast deployments 380553`
- New view key: `acurast devtools 380553`  (NOTE: CLI 0.11.0 devtools subcmd
  hits a mainnet endpoint and 404s on canary — use the URL printed at deploy.)

## Notes
- `runtime: "NodeJS"` = single-file script (no webpack bundle needed).
- `network: "canary"` so it's free; switch to `mainnet` only with real ACU.
- The x402 `deploy.acu.run` path (USDC on Base) is the separate mainnet/pay-per-use
  rail; it still needs a pre-pinned ipfs:// CID, which `acurast deploy` produces.
