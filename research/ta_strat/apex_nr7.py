"""
apex_nr7.py — characterize the NR7-breakout Apex portfolio (the survivor). Pure price-action,
intraday, flat by close, Apex-legal. Reports the speed/safety trade-off across per-trade risk and
daily-loss-limit, per-market solo, and raw (no management) vs partial-managed, with HONEST rates
(pass among decided AND pass counting censored windows as not-yet-passed).
"""
from __future__ import annotations
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from apex_strats2 import nr7_orb
from apex_walkforward import FOLDS, in_fold, expR

DFS = {m: load_fut(m) for m in ["es", "nq", "cl"]}


def merge(*ls):
    out = []
    for L in ls:
        out.extend(L)
    out.sort(key=lambda r: (r["eday"], r["xday"]))
    return out


def line(recs, R, lim, trail="intraday"):
    m = apex_eval(recs, R, day_loss_limit=lim, start_step=3, trail=trail)
    if not m:
        return None
    decided = m["passes"] + m["fails"]
    incl = m["passes"] + m["fails"] + m["censored"]
    return dict(R=R, lim=lim, passes=m["passes"], fails=m["fails"], cens=m["censored"],
                pr_decided=m["passes"] / decided if decided else 0,
                pr_incl=m["passes"] / incl if incl else 0,
                med=m["med_days"])


def main():
    nr7 = {m: nr7_orb(DFS[m], m, manage="partial")[0] for m in ["es", "nq", "cl"]}
    nr7_raw = {m: nr7_orb(DFS[m], m, manage="none")[0] for m in ["es", "nq", "cl"]}
    port = merge(*nr7.values())
    port_raw = merge(*nr7_raw.values())

    print("Per-market NR7 (partial-managed), full 3y:")
    for m in ["es", "nq", "cl"]:
        r = nr7[m]
        print(f"  {m}: n={len(r)} expR={expR(r):+.2f} win={np.mean([x['pnl_R']>0 for x in r])*100:.0f}%")
    print(f"  PORTFOLIO n={len(port)} expR={expR(port):+.2f} win={np.mean([x['pnl_R']>0 for x in port])*100:.0f}%")

    print("\nApex intraday-trailing pass vs risk/day-limit (partial-managed portfolio):")
    print(f"{'risk':>6}{'dayLim':>8}{'pass(dec)':>10}{'pass(incl)':>11}{'fails':>7}{'cens':>6}{'medDays':>8}")
    for R in [150, 200, 250, 300, 400, 500]:
        for k in [None, 2, 3]:
            lim = None if k is None else k * R
            x = line(port, R, lim)
            if x:
                print(f"{R:>6}{str(lim):>8}{x['pr_decided']*100:>9.0f}%{x['pr_incl']*100:>10.0f}%"
                      f"{x['fails']:>7}{x['cens']:>6}{(x['med'] or 0):>8.0f}")

    print("\nPer-year robustness (partial portfolio, $250/trade, daylim=$750):")
    for (lo, hi), yr in zip(FOLDS, ["Y1", "Y2", "Y3"]):
        x = line(in_fold(port, lo, hi), 250, 750)
        if x:
            print(f"  {yr}: pass(decided)={x['pr_decided']*100:.0f}% pass(incl cens)={x['pr_incl']*100:.0f}% "
                  f"fails={x['fails']} cens={x['cens']} medDays={x['med']}")

    print("\nRaw (no management, fixed 2R) portfolio for comparison:")
    print(f"{'risk':>6}{'dayLim':>8}{'pass(dec)':>10}{'fails':>7}{'cens':>6}{'medDays':>8}")
    for R in [150, 250, 350]:
        x = line(port_raw, R, None)
        if x:
            print(f"{R:>6}{'None':>8}{x['pr_decided']*100:>9.0f}%{x['fails']:>7}{x['cens']:>6}{(x['med'] or 0):>8.0f}")


if __name__ == "__main__":
    main()
