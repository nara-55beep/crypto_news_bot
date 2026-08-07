"""
bt_nr7_lucid.py — can NR7 (ES+NQ+CL) pass Lucid Trading? Lucid uses END-OF-DAY trailing drawdown
(only updates at 4:45pm close, locks once peak hits start+DD), one-time fee, no monthly, flat-by-close
(which NR7 already is). Tests the real Lucid account specs with EOD trailing, sweeping per-trade risk.

Lucid specs (verified 2026):  size / profit target / max-loss(DD)
  25K  / $1,250 / $1,000     50K / $3,000 / $2,000
  100K / $6,000 / $3,000     150K / $9,000 / $4,500
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from bt_ict_sm_tf import resample
from apex_strats2 import nr7_orb

ACCOUNTS = {  # name: (start, target, dd)
    "25K":  (25_000.0, 1_250.0, 1_000.0),
    "50K":  (50_000.0, 3_000.0, 2_000.0),
    "100K": (100_000.0, 6_000.0, 3_000.0),
    "150K": (150_000.0, 9_000.0, 4_500.0),
}
RISKS = [150, 200, 250, 300, 400, 500, 600, 800]


def main():
    recs = []
    for m in ["es", "nq", "cl"]:
        recs += nr7_orb(resample(load_fut(m), "5min"), m, manage="partial")[0]
    recs.sort(key=lambda r: (r["eday"], r["xday"]))
    print(f"NR7 (ES+NQ+CL) vs Lucid Trading — EOD trailing drawdown, no time limit, {len(recs)} trades/3y\n")

    for acct, (start, target, dd) in ACCOUNTS.items():
        print(f"=== Lucid {acct}: target +${target:,.0f}, ${dd:,.0f} EOD trailing ===")
        print(f"  {'risk/trade':>10}{'pass%':>8}{'breaches':>10}{'med days':>10}")
        best = None
        for R in RISKS:
            m = apex_eval(recs, R, start_bal=start, target=target, dd=dd, buf=0.0,
                          start_step=3, horizon_days=250, trail="eod")
            if not m:
                continue
            decided = m["passes"] + m["fails"]
            pr = m["passes"] / decided if decided else 0
            print(f"  {('$'+str(R)):>10}{pr*100:>7.0f}%{m['fails']:>10}{(m['med_days'] or 0):>10.0f}")
            if best is None or pr > best[1]:
                best = (R, pr, m["fails"], m["med_days"])
        if best:
            print(f"  -> best: {pr if False else ''}${best[0]}/trade = {best[1]*100:.0f}% pass, "
                  f"{best[2]} breaches, ~{best[3]:.0f} days median\n")


if __name__ == "__main__":
    main()
