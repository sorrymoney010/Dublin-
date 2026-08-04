from __future__ import annotations

import argparse
import json

from .config import Settings
from .engine import TradingEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dublin-bot")
    parser.add_argument("command", choices=["doctor", "run-once"])
    return parser


def doctor(settings: Settings) -> int:
    result = {
        "paper_trading": settings.paper_trading,
        "dry_run": settings.dry_run,
        "live_allowed": settings.allow_live_trading,
        "credentials_present": settings.has_credentials,
        "symbol": settings.symbol,
        "strategy_equity_usd": settings.strategy_equity_usd,
    }
    print(json.dumps(result, indent=2))
    if not settings.paper_trading:
        print("WARNING: live endpoint selected")
    if not settings.has_credentials:
        print("Paper order submission is unavailable until local credentials are configured.")
    return 0


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
