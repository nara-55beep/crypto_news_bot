"""
cli.py — run a backtest from the command line (no server needed).

Examples:
    python -m backend.cli backtest BTC/USDT
    python -m backend.cli backtest BTC/USDT ETH/USDT --days 180 --mode breakout
"""

from __future__ import annotations

import argparse
import json

from backend.backtest.engine import run_backtest
from backend.config import settings
from backend.utils.logger import setup_logging


def main() -> None:
    setup_logging(settings.log_dir)
    parser = argparse.ArgumentParser(description="crypto-trading-bot CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="run a backtest")
    bt.add_argument("symbols", nargs="+", help="e.g. BTC/USDT ETH/USDT")
    bt.add_argument("--days", type=int, default=settings.backtest_days)
    bt.add_argument("--mode", choices=["pullback", "breakout"], default=settings.entry_mode)

    args = parser.parse_args()
    if args.cmd == "backtest":
        settings.backtest_days = args.days
        settings.entry_mode = args.mode
        for sym in args.symbols:
            res = run_backtest(settings, sym)
            print(f"\n===== {sym} =====")
            print(json.dumps(res.get("metrics", res), indent=2))


if __name__ == "__main__":
    main()
