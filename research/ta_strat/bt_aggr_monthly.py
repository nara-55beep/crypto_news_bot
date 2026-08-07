"""
bt_aggr_monthly.py — the AGGRESSIVE NR7 portfolio over 3 years, $50k, $400 risk/trade.
= NR7 breakout (ES+NQ+CL)  +  NQ mean-reversion (VWAP-2sigma fade, Turtle-Soup, 80-20).
The NQ-MR adds the frequency that makes it pass Apex fast. Reports overall + per-strategy split
+ month-by-month trade counts. (NQ-MR is in-sample/overfit-suspect — see the note.)
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut
from bt_ict_sm_tf import resample
from apex_strats2 import nr7_orb, vwap_fade, turtle_soup, eighty_twenty

RISK = 400.0
START = 50_000.0


def ym(eday):
    return str((np.datetime64("1970-01-01") + np.timedelta64(int(eday), "D")).astype("datetime64[M]"))


def main():
    RAW = {m: load_fut(m) for m in ["es", "nq", "cl"]}
    recs = []; per = defaultdict(list)
    for m in ["es", "nq", "cl"]:
        df = resample(RAW[m], "5min")
        for x in nr7_orb(df, m, manage="partial")[0]:
            x["_mkt"] = m; x["_strat"] = "NR7"; recs.append(x); per["NR7"].append(x)
    dfn = resample(RAW["nq"], "5min")
    for fn, nm in [(vwap_fade, "VWAP2s"), (turtle_soup, "TurtleSoup"), (eighty_twenty, "80-20")]:
        for x in fn(dfn, "nq", manage="partial")[0]:
            x["_mkt"] = "nq"; x["_strat"] = nm; recs.append(x); per[nm].append(x)
    recs.sort(key=lambda r: (r["eday"], r["xday"]))

    n = len(recs)
    win = np.mean([r["pnl_R"] > 0 for r in recs]) * 100
    net = sum(r["pnl_R"] for r in recs) * RISK
    bal = START; peak = START; mdd = 0.0
    for r in recs:
        bal += r["pnl_R"] * RISK; peak = max(peak, bal); mdd = min(mdd, bal - peak)
    print("AGGRESSIVE NR7 (NR7 ES+NQ+CL + NQ mean-reversion) — 3y, $50,000, $400/trade\n")
    print(f"  total trades : {n}")
    print(f"  win rate     : {win:.1f}%")
    print(f"  net P&L      : ${net:+,.0f}")
    print(f"  final balance: ${START+net:,.0f}   ({net/START*100:+.0f}%)")
    print(f"  max drawdown : ${mdd:,.0f}")
    print("  by strategy  :")
    for s in ["NR7", "VWAP2s", "TurtleSoup", "80-20"]:
        sp = sum(x["pnl_R"] for x in per[s]) * RISK
        print(f"     {s:<11} {len(per[s]):>4}t  ${sp:+,.0f}   <- {'ROBUST' if s=='NR7' else 'overfit-suspect (single-market NQ)'}")

    mt = defaultdict(int); mp = defaultdict(float)
    for r in recs:
        k = ym(r["eday"]); mt[k] += 1; mp[k] += r["pnl_R"] * RISK
    print(f"\n{'month':<9}{'trades':>8}{'PnL($)':>11}{'balance($)':>13}")
    bal = START
    for k in sorted(mt):
        bal += mp[k]
        print(f"{k:<9}{mt[k]:>8}{mp[k]:>+11,.0f}{bal:>13,.0f}")
    print(f"\n  {len(mt)} months, avg {n/len(mt):.0f} trades/month (range {min(mt.values())}-{max(mt.values())})")
    print(f"  vs pure NR7's ~9/month — the NQ reversion is what makes it fast.")


if __name__ == "__main__":
    main()
