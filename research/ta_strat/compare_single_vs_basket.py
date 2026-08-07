"""
Why the basket instead of just the best single strategy?
Compare NQ_VWAP3 alone vs the full 5-strategy basket on the metrics that decide a
prop eval: max drawdown, losing months, and Lucid pass rate/speed.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from collections import defaultdict
import pandas as pd
from bt_lucid_10y import run

TARGET, DD, LOCK_CAP, HORIZON = 3000.0, 2000.0, 0.0, 200


def daily(events):
    by = defaultdict(float)
    for e in events:
        by[pd.Timestamp(e["ts"]).tz_convert("America/New_York").date()] += e["cash"]
    days = sorted(by)
    return days, np.array([by[d] for d in days])


def stats(events, label):
    days, pnl = daily(events)
    bal = 50000.0; peak = bal; mdd = 0.0
    monthly = defaultdict(float)
    for d, p in zip(days, pnl):
        bal += p; peak = max(peak, bal); mdd = max(mdd, peak - bal)
        monthly[(d.year, d.month)] += p
    negm = sum(1 for v in monthly.values() if v < 0)
    # eval: start every trading day
    p_ = pnl
    outc = defaultdict(int); dd_days = []
    for i0 in range(len(p_)):
        cum = 0.0; pk = 0.0; res = "undecided"; nd = 0
        for k in range(i0, min(i0 + HORIZON, len(p_))):
            cum += p_[k]; nd = k - i0 + 1
            if cum >= TARGET:
                res = "pass"; break
            pk = max(pk, cum)
            if cum <= min(pk - DD, LOCK_CAP):
                res = "fail"; break
        outc[res] += 1
        if res == "pass":
            dd_days.append(nd)
    n = sum(outc.values())
    total = bal - 50000.0
    print(f"\n{label}")
    print(f"  10y net: ${total:>+12,.0f}   max drawdown: -${mdd:>8,.0f}   losing months: {negm}/{len(monthly)}")
    print(f"  Lucid 50K eval (start every day): {100*outc['pass']/n:>3.0f}% pass, "
          f"{100*outc['fail']/n:>2.0f}% fail, median {int(np.median(dd_days)) if dd_days else -1} days to pass")


def main():
    events = run()      # full 10y, all components
    basket = events
    nq_only = [e for e in events if e.get("key") == "NQ_VWAP3"]
    stats(nq_only, "NQ 3m VWAP fade ALONE (the single best strategy):")
    stats(basket, "FULL 5-STRATEGY BASKET:")


if __name__ == "__main__":
    main()
