"""
Apex 25K with INTRADAY trailing (the cheaper Apex plan). Harsher than EOD: the
trailing floor follows the running equity's peak AFTER EACH TRADE, so a mid-day
dip below the trailing floor breaches even if the day later recovers.

Trade-level approximation using per-trade cash (ignores intra-trade unrealized
swings, so this is optimistic vs Apex's true mark-to-market intraday trail).
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from collections import defaultdict
import pandas as pd
from bt_lucid_10y import run

TARGET, DD, LOCK_CAP, HORIZON = 1500.0, 1000.0, 100.0, 200


def trade_series(events):
    trades = [(pd.Timestamp(e["ts"]).tz_convert("UTC").value, e["cash"]) for e in events if e["kind"] == "close"]
    trades.sort()
    days = [pd.Timestamp(t, tz="UTC").tz_convert("America/New_York").date() for t, _ in trades]
    return days, np.array([c for _, c in trades])


def sim_intraday(cash, i0):
    cum = 0.0; peak = 0.0; n = 0
    for k in range(i0, len(cash)):
        cum += cash[k]; n += 1
        if cum >= TARGET:
            return "pass", n
        peak = max(peak, cum)
        if cum <= min(peak - DD, LOCK_CAP):   # checked after EVERY trade
            return "fail", n
        if n >= HORIZON:
            break
    return "undecided", n


def main():
    events = run()
    _, base = trade_series(events)     # per-trade cash at $200 risk
    print(f"\n=== APEX 25K INTRADAY trailing (cheaper plan): +$1,500 / $1,000 trail ===")
    print(f"  {'risk/trade':>10} {'pass%':>7} {'fail%':>7}")
    for R in (75, 100, 125, 150, 200):
        cash = base * (R / 200.0)
        out = defaultdict(int)
        for i0 in range(len(cash)):
            o, _ = sim_intraday(cash, i0)
            out[o] += 1
        n = sum(out.values())
        print(f"  {('$'+str(R)):>10} {100*out['pass']/n:>6.0f}% {100*out['fail']/n:>6.0f}%")
    print("\n  (compare: EOD-trail plan passed 98% at $100 risk)")


if __name__ == "__main__":
    main()
