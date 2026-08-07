"""
apex_monthly.py — month-by-month track record of the recommended portfolio, $50k start, FIXED
$400/trade risk (Apex sizing, no compounding), executed on 1m / 3m / 5m / 15m bars.
Portfolio = NR7 breakout (ES+NQ+CL) + NQ mean-reversion (vwap2sigma + turtle-soup + 80-20),
partial-managed (1/2 off at +1R, runner to 2R), flat by session close.
Reports per-market 3y totals + every month's trades / PnL($) / running balance, per timeframe.
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
START = 50000.0
TFS = [("1m", "1min"), ("3m", "3min"), ("5m", "5min"), ("15m", "15min")]
RAW = {m: load_fut(m) for m in ["es", "nq", "cl"]}


def ym_of(eday):
    d = (np.datetime64("1970-01-01") + np.timedelta64(int(eday), "D")).astype("datetime64[M]")
    return str(d)   # 'YYYY-MM'


def tag(recs, mkt, strat):
    for r in recs:
        r["_mkt"] = mkt; r["_strat"] = strat
    return recs


def build_portfolio(rule):
    recs = []
    for m in ["es", "nq", "cl"]:
        df = resample(RAW[m], rule)
        recs += tag(nr7_orb(df, m, manage="partial")[0], m, "nr7")
    dfn = resample(RAW["nq"], rule)
    recs += tag(vwap_fade(dfn, "nq", manage="partial")[0], "nq", "vwap2s")
    recs += tag(turtle_soup(dfn, "nq", manage="partial")[0], "nq", "turtle")
    recs += tag(eighty_twenty(dfn, "nq", manage="partial")[0], "nq", "80-20")
    recs.sort(key=lambda r: (r["eday"], r["xday"]))
    return recs


def main():
    summary = []
    for tname, rule in TFS:
        recs = build_portfolio(rule)
        # per-market totals
        mkt_pnl = defaultdict(float); mkt_n = defaultdict(int)
        mt = defaultdict(int); mp = defaultdict(float)
        for r in recs:
            usd = r["pnl_R"] * RISK
            mkt_pnl[r["_mkt"]] += usd; mkt_n[r["_mkt"]] += 1
            ym = ym_of(r["eday"]); mt[ym] += 1; mp[ym] += usd
        months = sorted(mt)
        print(f"\n{'='*60}\nTIMEFRAME {tname}  —  $50k start, ${int(RISK)}/trade fixed risk\n{'='*60}")
        print("per-market 3y:  " + "   ".join(
            f"{k.upper()} ${mkt_pnl[k]:+,.0f} ({mkt_n[k]}t)" for k in ["es", "nq", "cl"]))
        print(f"{'month':<9}{'trades':>7}{'PnL($)':>11}{'balance($)':>13}")
        bal = START; peak = START; maxdd = 0.0; winm = 0; tot_n = 0
        for ym in months:
            bal += mp[ym]; peak = max(peak, bal); maxdd = min(maxdd, bal - peak)
            winm += 1 if mp[ym] > 0 else 0; tot_n += mt[ym]
            print(f"{ym:<9}{mt[ym]:>7}{mp[ym]:>+11,.0f}{bal:>13,.0f}")
        net = bal - START
        print(f"  -> 3y: {tot_n} trades | net ${net:+,.0f} | final ${bal:,.0f} | "
              f"maxDD ${maxdd:,.0f} | up-months {winm}/{len(months)}")
        summary.append((tname, tot_n, net, bal, maxdd, winm, len(months)))

    print(f"\n{'='*60}\nCROSS-TIMEFRAME SUMMARY ($50k start, $400/trade)\n{'='*60}")
    print(f"{'TF':<5}{'trades':>8}{'net$':>12}{'final$':>13}{'maxDD$':>11}{'up-months':>11}")
    for (t, n, net, bal, dd, w, mm) in summary:
        print(f"{t:<5}{n:>8}{net:>+12,.0f}{bal:>13,.0f}{dd:>11,.0f}{(str(w)+'/'+str(mm)):>11}")


if __name__ == "__main__":
    main()
