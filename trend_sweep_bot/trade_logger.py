"""
trade_logger.py — CSV trade ledger + structured event logging.

Every entry, exit (stop/target/time), partial, and PnL is written to a CSV and echoed to a logger.
"""
from __future__ import annotations
import csv
import logging
import os
from datetime import datetime, timezone

TRADE_FIELDS = ["closed_at", "symbol", "side", "entry", "exit", "qty", "stop",
                "tp1", "tp2", "pnl", "r_multiple", "reason", "bars_held"]


def get_logger(name="trend_sweep_bot", logfile=None, level=logging.INFO):
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if logfile:
        os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)
        fh = logging.FileHandler(logfile)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


class TradeCSV:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(TRADE_FIELDS)

    def write(self, rec: dict):
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                datetime.fromtimestamp(rec.get("closed_at", 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                rec.get("symbol", ""), rec.get("side", ""), rec.get("entry", ""),
                rec.get("exit", ""), rec.get("qty", ""), rec.get("stop", ""),
                rec.get("tp1", ""), rec.get("tp2", ""), rec.get("pnl", ""),
                rec.get("r_multiple", ""), rec.get("reason", ""), rec.get("bars_held", ""),
            ])
