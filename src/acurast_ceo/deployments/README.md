# Acurast deployment apps (run ON the processors)

These are the workloads that execute *on* your Acurast Core phones, using the
official Acurast processor runtime API (`print`, `httpGET`, `httpPOST`,
`_STD_.env`, `_STD_.job.*`, `_STD_.chains.*`, etc.). They are the engine-3/4/5
workloads from the strategy: keep phones busy with *useful* jobs, and sell the
output.

## Scripts
- `monitor/monitor.js`      — Engine 4 Product A: Website/API Monitor-as-a-Service.
                              Polls a customer URL, posts failure alerts to a
                              webhook (Telegram/ntfy/your backend).
- `scanner/scanner.js`      — Engine 3/2: Opportunity Scanner. Fetches sources,
                              scores leads, forwards to the Wealth Operator.

Both are plain `NodeJS` runtime scripts (no build step). They read config from
environment variables (set in `acurast.json` -> `_STD_.env` at deploy time).

## How a deploy reaches the phones
The Acurast CLI bundles a script, uploads it to IPFS, and registers the job.
The x402 Deploy Agent (`deploy.acu.run`) lets an agent/backend pay in USDC on
Base — no ACU account needed:

    # 1) pin the script to IPFS, get the CID
    acurast deploy --only-upload          # -> ipfs://Qm...

    # 2) quote + pay in USDC on Base (needs Coinbase `awal` agentic wallet)
    npx awal@latest x402 pay "https://deploy.acu.run/deploy" -X POST -d '{
      "script": "ipfs://<CID>",
      "reward": <picoACU>,            # 1 ACU = 1e12 picoACU
      "runtime": "NodeJS",
      "slots": 1,
      "allowOnlyVerifiedSources": true,
      "schedule": { "interval": 900000, "duration": 60000, "maxStartDelay": 10000 }
    }'

The `python -m acurast_ceo.cli deploy <app>` command in this package performs
step 1 (IPFS upload via the Acurast CLI) and prints the exact step-2 `awal`
command with the real CID and reward filled in.

## Environment variables (per deployment, via acurast.json)
monitor.js:
  TARGET_URL        URL or list (comma-separated) to monitor
  ALERT_WEBHOOK     POST endpoint receiving {url,status,ts} on failure
  CHECK_INTERVAL_MS how often to poll (default 60000)
scanner.js:
  SOURCES           comma-separated source URLs
  FORWARD_WEBHOOK   endpoint receiving scored opportunities
  MIN_SCORE         minimum score to forward (default 0.3)
