"""
apex_final.py — focused Apex validation of the strategies that survived the k-fold (positive in ALL
3 one-year folds). These are PRE-REGISTERED public strategies (Crabel NR7, Connors Double Seven /
3-Days-Down, VWAP-2sigma, Turtle Soup) — not curve-fit to this data — so cross-market/cross-year
positivity is real evidence. Reports the Apex $50k pass rate FULL-3y and PER-YEAR (robustness).
"""
from __future__ import annotations
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from apex_strats2 import STRATS
from apex_swing import SWING
from apex_walkforward import FOLDS, in_fold, expR

DFS = {m: load_fut(m) for m in ["es", "nq", "cl"]}


def block(stratmap, nm, mkt, **kw):
    recs, _ = stratmap[nm](DFS[mkt], mkt, **kw)
    return recs


def merge(*lists):
    out = []
    for L in lists:
        out.extend(L)
    out.sort(key=lambda r: (r["eday"], r["xday"]))
    return out


def apex_report(name, recs, trails=("intraday",), risks=(150, 200, 250, 300)):
    print(f"\n### {name}: n={len(recs)} expR={expR(recs):+.2f} "
          f"win={np.mean([r['pnl_R']>0 for r in recs])*100:.0f}%")
    for trail in trails:
        best = None
        for R in risks:
            for k in [None, 3]:
                lim = None if k is None else k * R
                m = apex_eval(recs, R, day_loss_limit=lim, start_step=3, trail=trail)
                if m and (best is None or m["pass_rate"] > best["pass_rate"]):
                    best = m; best["lim"] = lim
        if best:
            print(f"   [{trail}] best pass {best['pass_rate']*100:.0f}% @ ${best['R']}/trade "
                  f"daylim={best['lim']} | fails={best['fails']} cens={best['censored']} "
                  f"medDays={best['med_days']} bestDay={int((best['med_bestday_share'] or 0)*100)}%")
            # per-year robustness at that fixed config
            yr = []
            for (lo, hi) in FOLDS:
                mm = apex_eval(in_fold(recs, lo, hi), best["R"], day_loss_limit=best["lim"],
                               start_step=3, trail=trail)
                yr.append(f"{mm['pass_rate']*100:.0f}%" if mm else "—")
            print(f"   [{trail}] per-year pass @ that config: Y1 {yr[0]}  Y2 {yr[1]}  Y3 {yr[2]}")


def main():
    t0 = time.time()
    print("INTRADAY (Apex-compatible, flat by close) — partial-managed")

    nr7 = merge(block(STRATS, "nr7_orb", "es"), block(STRATS, "nr7_orb", "nq"),
                block(STRATS, "nr7_orb", "cl"))
    apex_report("NR7 breakout (ES+NQ+CL)", nr7, trails=("intraday",))

    nqmr = merge(block(STRATS, "vwap_fade", "nq"), block(STRATS, "turtle_soup", "nq"),
                 block(STRATS, "eighty_twenty", "nq"))
    apex_report("NQ mean-reversion (vwap2s + turtlesoup + 80-20)", nqmr, trails=("intraday",))

    allintra = merge(nr7, nqmr)
    apex_report("ALL intraday survivors", allintra, trails=("intraday",))

    print("\n" + "=" * 70)
    print("SWING (daily MR, holds OVERNIGHT -> needs swing/EOD-trailing account, NOT std Apex eval)")
    ds = merge(block(SWING, "double_seven", "es"), block(SWING, "double_seven", "nq"))
    td = merge(block(SWING, "three_down", "es"), block(SWING, "three_down", "nq"))
    ibs = merge(block(SWING, "ibs_daily", "es"), block(SWING, "ibs_daily", "nq"))
    swing = merge(ds, td, ibs)
    apex_report("Double Seven (ES+NQ)", ds, trails=("eod", "intraday"))
    apex_report("3-Days-Down (ES+NQ)", td, trails=("eod", "intraday"))
    apex_report("Swing MR combined (DoubleSeven+3Down+IBS, ES+NQ)", swing, trails=("eod", "intraday"))

    print(f"\n[done {time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
