"""
apex_multi.py — how you ACTUALLY pass Apex in one month: a real ~85% edge run across several cheap
promo evals. One account passes ~85% of months; running K accounts started a few days apart turns
"at least one passes this month" into a near-certainty. This is the legit Apex 20-account play.
Computes P(>=1 of K passes within the month) from the real per-start outcomes on 3y of data.
"""
from __future__ import annotations
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nr7_paper_bot import build, simulate_eval

BIG, SMALL = 400, 150


def per_start(recs, horizon=30):
    days = sorted(set(r["eday"] for r in recs))
    out = []
    for s in days:
        outcome, log = simulate_eval(recs, s, BIG, SMALL, horizon=horizon)
        if log:
            out.append((s, outcome))
    return out


def campaign(per, K, stag):
    """K accounts started `stag` trading-days apart. Success = >=1 PASS. Also track all-blow-up."""
    pr = [(s, o) for (s, o) in per]
    n = len(pr); succ = 0; allblow = 0; tot = 0
    for i in range(n):
        idxs = [i + j * stag for j in range(K)]
        if idxs[-1] >= n:
            break
        outs = [pr[k][1] for k in idxs]
        tot += 1
        if any(o == "PASS" for o in outs):
            succ += 1
        if all(o == "BLOWUP" for o in outs):
            allblow += 1
    return succ / tot if tot else 0, allblow / tot if tot else 0


def main():
    print("Building aggressive portfolio (NR7 + NQ mean-reversion)...", flush=True)
    recs = build("aggressive")
    per = per_start(recs, horizon=30)
    n = len(per)
    p1 = sum(1 for _, o in per if o == "PASS") / n
    pb = sum(1 for _, o in per if o == "BLOWUP") / n
    ps = sum(1 for _, o in per if o == "TOO_SLOW") / n
    print(f"\nSINGLE account, 1-month EOD, sprint ${BIG}/${SMALL}:")
    print(f"  pass {p1*100:.0f}%   blow-up {pb*100:.0f}%   too-slow {ps*100:.0f}%   ({n} start days tested)")

    print(f"\nMULTI-ACCOUNT — start a fresh promo eval every 3 trading days, all within the month:")
    print(f"{'accounts':>9}{'P(>=1 passes)':>16}{'P(all blow up)':>16}{'promo cost ~$30 ea':>20}")
    for K in [1, 2, 3, 4, 5]:
        sp, ab = campaign(per, K, stag=3)
        print(f"{K:>9}{sp*100:>15.0f}%{ab*100:>15.1f}%{('$'+str(K*30)):>20}")

    print("\nSame, started every 5 trading days (more decorrelated):")
    print(f"{'accounts':>9}{'P(>=1 passes)':>16}{'P(all blow up)':>16}")
    for K in [1, 2, 3, 4, 5]:
        sp, ab = campaign(per, K, stag=5)
        print(f"{K:>9}{sp*100:>15.0f}%{ab*100:>15.1f}%")
    print("\nPASS = at least one account hit +$3,000 within its 30 days. This is the real")
    print("'guaranteed 1-month pass': an 85% edge + a few $30 evals, not a 100% strategy.")


if __name__ == "__main__":
    main()
