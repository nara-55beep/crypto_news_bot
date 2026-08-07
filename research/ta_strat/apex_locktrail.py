"""
apex_locktrail.py — the "best-odds plan" simulated over a 1-MONTH window on an EOD-trailing $50k
Apex account, WITH the lock-the-trail sprint sizing: risk BIG until the trailing floor locks
(peak >= ~$52,600), then drop to small and coast to +$3,000. Reports 1-month pass rate, blow-ups,
and how fast it passes (within 1/2/3/4 weeks). NR7 (robust) vs NR7+NQ-MR (faster, overfit-suspect).
"""
from __future__ import annotations
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from apex_strats2 import nr7_orb, vwap_fade, turtle_soup, eighty_twenty
from apex_walkforward import FOLDS, in_fold, expR

RAW = {m: load_fut(m) for m in ["es", "nq", "cl"]}


def merge(*ls):
    out = []
    for L in ls:
        out.extend(L)
    out.sort(key=lambda r: (r["eday"], r["xday"]))
    return out


def nr7_port():
    return merge(*[nr7_orb(RAW[m], m, manage="partial")[0] for m in ["es", "nq", "cl"]])


def nqmr_port():
    return merge(vwap_fade(RAW["nq"], "nq", manage="partial")[0],
                 turtle_soup(RAW["nq"], "nq", manage="partial")[0],
                 eighty_twenty(RAW["nq"], "nq", manage="partial")[0])


def run(recs, label, big, small, switch_off=2600, horizon=30, trail="eod"):
    m = apex_eval(recs, R=small, start_step=2, horizon_days=horizon, trail=trail,
                  big=big, small=small, switch_off=switch_off)
    total = m["passes"] + m["fails"] + m["censored"]
    pr = m["passes"] / total if total else 0
    dl = m["days_list"]
    def within(d):
        return (sum(1 for x in dl if x <= d) / total * 100) if total else 0
    print(f"  {label:<26} big=${big} small=${small} | PASS {pr*100:>3.0f}%  blowups {m['fails']:>3}  "
          f"tooSlow {m['censored']:>3} | by wk1 {within(7):>3.0f}% wk2 {within(14):>3.0f}% "
          f"wk3 {within(21):>3.0f}% wk4 {within(30):>3.0f}%")
    return pr, m


def main():
    nr7 = nr7_port(); both = merge(nr7, nqmr_port())
    print("1-MONTH RESULTS on EOD $50k account, lock-the-trail sprint sizing")
    print("(reach +$3,000 within 30 trading days AND never breach the $2,500 EOD trail)\n")

    print("PLAN A — NR7 only (ES+NQ+CL), the robust core:")
    for big, small in [(400, 150), (600, 150), (800, 200), (1000, 200)]:
        run(nr7, "NR7 sprint", big, small)
    print("\n  flat-size baselines (no sprint), NR7 only:")
    for R in [300, 500, 800]:
        run(nr7, f"NR7 flat ${R}", R, R)

    print("\nPLAN B — NR7 + NQ mean-reversion (faster, but NQ-MR is in-sample/overfit-suspect):")
    for big, small in [(400, 150), (600, 150), (800, 200)]:
        run(both, "NR7+NQ-MR sprint", big, small)

    print("\nPER-YEAR robustness of PLAN A (NR7 sprint $600/$150), 1-month EOD:")
    for (lo, hi), y in zip(FOLDS, ["Y1", "Y2", "Y3"]):
        run(in_fold(nr7, lo, hi), f"NR7 {y}", 600, 150)


if __name__ == "__main__":
    main()
