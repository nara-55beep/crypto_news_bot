"""
bt_nr7_monthly.py — NR7 Breakout Apex (ES+NQ+CL), 3 years, $50,000 start. Fixed $400 risk/trade
(0.8% of $50k, Apex-style, no compounding), partial-managed (1/2 at +1R, BE, runner 2R), flat by
close. Reports the 3-year result + a month-by-month table with the trade count per market.
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut
from bt_ict_sm_tf import resample
from apex_strats2 import nr7_orb

RISK = 400.0
START = 50_000.0


def ym(eday):
    return str((np.datetime64("1970-01-01") + np.timedelta64(int(eday), "D")).astype("datetime64[M]"))


def main():
    recs = []
    per_mkt = {}
    for m in ["es", "nq", "cl"]:
        df = resample(load_fut(m), "5min")
        r = nr7_orb(df, m, manage="partial")[0]
        for x in r:
            x["_mkt"] = m
        per_mkt[m] = r
        recs += r
    recs.sort(key=lambda r: (r["eday"], r["xday"]))

    # overall
    n = len(recs)
    win = np.mean([r["pnl_R"] > 0 for r in recs]) * 100
    net = sum(r["pnl_R"] for r in recs) * RISK
    bal = START; peak = START; mdd = 0.0
    for r in recs:
        bal += r["pnl_R"] * RISK; peak = max(peak, bal); mdd = min(mdd, bal - peak)
    print("NR7 Breakout Apex (ES+NQ+CL) — 3 years, $50,000 start, $400 risk/trade (fixed)\n")
    print(f"  total trades : {n}")
    print(f"  win rate     : {win:.1f}%")
    print(f"  net P&L      : ${net:+,.0f}")
    print(f"  final balance: ${START+net:,.0f}   ({net/START*100:+.1f}%)")
    print(f"  max drawdown : ${mdd:,.0f}")
    print("  per market   : " + "  ".join(
        f"{m.upper()} {len(per_mkt[m])}t ${sum(x['pnl_R'] for x in per_mkt[m])*RISK:+,.0f}" for m in ["es", "nq", "cl"]))

    # monthly
    mt = defaultdict(int); mp = defaultdict(float)
    mk = defaultdict(lambda: defaultdict(int))
    for r in recs:
        k = ym(r["eday"]); mt[k] += 1; mp[k] += r["pnl_R"] * RISK; mk[k][r["_mkt"]] += 1
    print(f"\n{'month':<9}{'trades':>7}{'ES':>4}{'NQ':>4}{'CL':>4}{'PnL($)':>11}{'balance($)':>13}")
    bal = START; tot = 0
    for k in sorted(mt):
        bal += mp[k]; tot += mt[k]
        print(f"{k:<9}{mt[k]:>7}{mk[k]['es']:>4}{mk[k]['nq']:>4}{mk[k]['cl']:>4}{mp[k]:>+11,.0f}{bal:>13,.0f}")
    months = len(mt)
    print(f"\n  {months} months, avg {n/months:.1f} trades/month "
          f"(range {min(mt.values())}-{max(mt.values())} per month)")


if __name__ == "__main__":
    main()
