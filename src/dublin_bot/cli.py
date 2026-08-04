from __future__ import annotations

import argparse
import json

from .config import Settings
from .engine import TradingEngine
from .gateway import build_gateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dublin-bot")
    parser.add_argument("command", choices=["doctor", "run-once"])
    return parser


def doctor(settings: Settings) -> int:
    result: dict[str, object] = {
        "exchange": settings.exchange,
        "paper_trading": settings.paper_trading,
        "dry_run": settings.dry_run,
        "live_allowed": settings.allow_live_trading,
        "live_execution_enabled": settings.live_execution_enabled,
        "credentials_present": settings.has_credentials,
        "symbol": settings.symbol,
        "strategy_equity_usd": settings.strategy_equity_usd,
    }
    exit_code = 0
    try:
        result["gateway"] = build_gateway(settings).diagnostic()
        gateway = result["gateway"]
        if isinstance(gateway, dict) and gateway.get("withdraw_permission_detected") is True:
            result["safety_error"] = "Remove withdrawal permission from the Kraken API key"
            exit_code = 2
    except Exception as exc:
        result["gateway_error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings()
    if args.command == "doctor":
        return doctor(settings)
    record = TradingEngine(settings).run_once()
    print(json.dumps(record.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
