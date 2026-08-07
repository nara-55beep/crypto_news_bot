"""
run_live.py — start the live/testnet trader.

    set BINANCE_API_KEY / BINANCE_API_SECRET in the environment first, then:
    python -m trend_sweep_bot.run_live                 # testnet (config default)
    python -m trend_sweep_bot.run_live --symbol ETH/USDT

To go to REAL money set `testnet: false` in config.yaml — do that only after testnet proves out.
"""
from __future__ import annotations
import argparse

from .config import Config
from .live import LiveTrader
from .trade_logger import get_logger


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--poll", type=int, default=20)
    args = ap.parse_args()

    cfg = Config.load(args.config)
    if args.symbol:
        cfg.symbol = args.symbol
    log = get_logger("trend_sweep_bot.live")
    if not cfg.api_key:
        log.error("No API key. Set BINANCE_API_KEY / BINANCE_API_SECRET in the environment.")
        return
    if not cfg.testnet:
        log.warning("REAL-MONEY MODE (testnet=false). Ctrl-C now if that's not intended.")
    LiveTrader(cfg).run(poll_sec=args.poll)


if __name__ == "__main__":
    main()
