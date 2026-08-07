"""
run_backtest.py — fetch history from Binance Futures and backtest the strategy.

Usage:
    python -m trend_sweep_bot.run_backtest                 # uses config.yaml / defaults
    python -m trend_sweep_bot.run_backtest --symbol ETH/USDT --days 60
"""
from __future__ import annotations
import argparse
import csv
import os

from .config import Config
from . import data_feed
from .backtest import Backtester
from .metrics import print_report
from .trade_logger import get_logger


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    if args.symbol:
        cfg.symbol = args.symbol
    if args.days:
        cfg.backtest_days = args.days

    log = get_logger("trend_sweep_bot.backtest")
    log.info("Fetching %s history (%d days) from %s ...", cfg.symbol, cfg.backtest_days, cfg.exchange)
    ex = data_feed.make_exchange(cfg, signed=False)
    c5 = data_feed.history(ex, cfg.symbol, cfg.tf_entry, cfg.backtest_days)
    c4h = data_feed.history(ex, cfg.symbol, cfg.tf_trend, cfg.backtest_days + 10)
    c1d = data_feed.history(ex, cfg.symbol, cfg.tf_daily, cfg.backtest_days + 10)
    log.info("Loaded %d x5m, %d x4h, %d x1d candles", len(c5), len(c4h), len(c1d))
    if len(c5) < 100:
        log.error("not enough 5m data; aborting")
        return

    os.makedirs(args.out, exist_ok=True)
    trades_csv = os.path.join(args.out, f"trades_{cfg.symbol.replace('/', '')}.csv")
    bt = Backtester(cfg, log=log)
    res = bt.run(c5, c4h, c1d, trades_csv=trades_csv)

    print_report(res["metrics"], title=f"BACKTEST {cfg.symbol} ({cfg.backtest_days}d, {cfg.tf_entry})")
    # write the equity curve
    eq_path = os.path.join(args.out, f"equity_{cfg.symbol.replace('/', '')}.csv")
    with open(eq_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "equity"])
        for ts, eq in res["equity_curve"]:
            w.writerow([ts, round(eq, 2)])
    log.info("Trades -> %s   Equity -> %s", trades_csv, eq_path)


if __name__ == "__main__":
    main()
