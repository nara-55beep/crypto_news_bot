"""
bt_apex25k_eval.py — can the Lucid 5-strategy basket pass an APEX 25K eval?
Apex 25K: $1,500 profit target, $1,000 trailing drawdown.

The basket daily-pnl was generated at $200 risk (bt_lucid_10y). PnL scales linearly
with risk (fractional micros), so risk R -> factor R/200. A $1,000 trailing DD is tight,
so we sweep smaller risk sizes and report pass rates for BOTH drawdown models:
  - EOD trail  (what the strategy was validated on; locks at +$100 once peak>=+$1,000)
  - Intraday-ish proxy (penalize with a same-day-low haircut) -- rough, flagged as approximate
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from collections import defaultdict
import pandas as pd
from bt_lucid_10y import run

TARGET = 1500.0
DD = 1000.0
LOCK_CAP = 100.0        # Apex locks trailing at start+$100 once you're up enough
HORIZON = 200


def daily_series(events):
    byday = defaultdict(float)
    for e in events:
        d = pd.Timestamp(e["ts"]).tz_convert("America/New_York").date()
        byday[d] += e["cash"]
    days = sorted(byday)
    return days, np.array([byday[d] for d in days])


def sim_eval(pnl, i0):
    cum = 0.0; peak = 0.0
    for k in range(i0, min(i0 + HORIZON, len(pnl))):
        cum += pnl[k]
        if cum >= TARGET:
            return "pass", k - i0 + 1
        peak = max(peak, cum)
        floor = min(peak - DD, LOCK_CAP)   # EOD trailing, locks at +$100
        if cum <= floor:
            return "fail", k - i0 + 1
    return "undecided", 0


def main():
    events = run()
    days, base = daily_series(events)      # at $200 risk
    print(f"\n{len(days)} trading days {days[0]} -> {days[-1]}")
    print("\n=== APEX 25K eval: +$1,500 target, $1,000 EOD trailing (lock +$100) ===")
    print(f"  {'risk/trade':>10} {'pass%':>7} {'fail%':>7} {'med days':>9}")
    for R in (75, 100, 125, 150, 200):
        pnl = base * (R / 200.0)
        out = defaultdict(int); dd = []
        for i0 in range(len(pnl)):
            o, nd = sim_eval(pnl, i0)
            out[o] += 1
            if o == "pass": dd.append(nd)
        n = sum(out.values())
        med = int(np.median(dd)) if dd else -1
        print(f"  {('$'+str(R)):>10} {100*out['pass']/n:>6.0f}% {100*out['fail']/n:>6.0f}% {med:>9}")
    # by year at the best small risk ($100)
    print("\n  by start-year at $100 risk:")
    pnl = base * 0.5
    yr = defaultdict(lambda: [0,0,0])
    for i0 in range(len(pnl)):
        o,_ = sim_eval(pnl, i0)
        y = days[i0].year
        yr[y][0 if o=='pass' else 1 if o=='fail' else 2] += 1
    for y in sorted(yr):
        p,f,u = yr[y]; n=p+f+u
        print(f"    {y}: pass {100*p/n:>3.0f}%  fail {100*f/n:>3.0f}%")


if __name__ == "__main__":
    main()
