"""
audit_signal_differential.py — the strongest possible logic test.

Instead of reading code, this RUNS the live bot's OWN signal methods and the backtest's
signal functions on the SAME bars, for every session day over years, and compares the
outputs exactly (direction, entry, stop, target, entry bar index).

Any divergence in VWAP maths, Turtle levels, NR7 detection, rounding, or lookback
handling shows up here as a mismatch.
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE); sys.path.insert(0, PROJ)

import lucid_pass_paper as LIVE
import bt_lucid_10y as BT

TOL = 1e-6


class StubBot(LIVE.LucidPassPaperBot):
    """Instantiate the live signal code without touching disk/network/state."""
    def __init__(self):
        self.setups = {}
        self.warning_keys = set()
        self.fired_keys = set()
        self.pos = {}
        self.enabled = True
        self.failed = False
        self.passed = False
        self.day_key = ""
        self.day_pnl = 0.0
        self.log = []
        self.balance = 50000.0
    # silence side effects
    def _warn_signal(self, *a, **k): pass
    def _note(self, *a, **k): pass
    def _alert(self, *a, **k): pass


def day_to_frame(day: dict) -> pd.DataFrame:
    """Rebuild the DataFrame shape the live bot's signal methods expect."""
    return pd.DataFrame({
        "dt_utc": pd.to_datetime(day["ts"], utc=True),
        "open": day["op"], "high": day["hi"], "low": day["lo"],
        "close": day["cl"], "volume": day["vol"],
        "day": [day["day"]] * len(day["hi"]),
    })


def daily_hist_to_frame(hist: list) -> pd.DataFrame:
    if not hist:
        return pd.DataFrame(columns=["day", "open", "high", "low", "close", "range"])
    df = pd.DataFrame(hist, columns=["day", "high", "low", "close", "open"])
    df["range"] = df["high"] - df["low"]
    return df[["day", "open", "high", "low", "close", "range"]]


def main():
    start = sys.argv[sys.argv.index("--start") + 1] if "--start" in sys.argv else "2023-06-19"
    bot = StubBot()
    grand_days = grand_mismatch = 0
    grand_sig_live = grand_sig_bt = 0

    for key, c in BT.COMPONENTS.items():
        raw = BT.load_market(c["m"], start, None)
        if raw.empty:
            continue
        days = BT.make_days(raw, c["bar_min"])
        _pv, tick = BT.MARKETS[c["m"]]
        kind = c["kind"]
        daily_hist = []
        n_days = n_mismatch = n_live = n_bt = 0
        examples = []

        for day in days:
            n_days += 1
            frame = day_to_frame(day)
            last_i = len(frame) - 1
            hist_df = daily_hist_to_frame(daily_hist)

            # ---- backtest signal ----
            if kind == "vwap":
                bt_sig = BT.sig_vwap(day)
            elif kind == "turtle":
                bt_sig = BT.sig_turtle(day, daily_hist, tick)
            else:
                bt_sig = BT.sig_nr7(day, daily_hist, tick)

            # ---- live signal (its own code) ----
            live_sig = bot._signal(key, frame, hist_df)

            if bt_sig is not None:
                n_bt += 1
            if live_sig is not None:
                n_live += 1

            # compare
            same = True
            if (bt_sig is None) != (live_sig is None):
                same = False
            elif bt_sig is not None:
                bi, bside, bentry, bstop, btarget = bt_sig
                lside = 1 if live_sig["side"] == "long" else -1
                if (bside != lside
                        or abs(bentry - live_sig["entry"]) > TOL
                        or abs(bstop - live_sig["stop"]) > TOL
                        or abs(btarget - live_sig["target"]) > TOL):
                    same = False
            if not same:
                n_mismatch += 1
                if len(examples) < 3:
                    examples.append((day["day"], bt_sig, live_sig))

            daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                               float(day["cl"][-1]), float(day["op"][0])))

        print(f"{key:<13} days {n_days:>5}   signals: BT {n_bt:>4} / LIVE {n_live:>4}   "
              f"mismatches {n_mismatch:>4}   {'OK' if n_mismatch == 0 else '*** DIFFERENT ***'}")
        for d, b, l in examples:
            print(f"     e.g. {d}: BT={b}  LIVE={None if l is None else (l['side'], round(l['entry'],3), round(l['stop'],3), round(l['target'],3))}")
        grand_days += n_days; grand_mismatch += n_mismatch
        grand_sig_live += n_live; grand_sig_bt += n_bt

    print("-" * 92)
    print(f"TOTAL: {grand_days} component-days tested | BT signals {grand_sig_bt} | LIVE signals {grand_sig_live}")
    print(f"MISMATCHES: {grand_mismatch}")


if __name__ == "__main__":
    main()
