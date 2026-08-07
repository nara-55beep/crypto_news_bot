"""
apex_walkforward.py — the honest test. 3 one-year folds. For each held-out test year, SELECT the
+EV blocks using ONLY the other two years, then measure the Apex pass rate on the unseen year.
A real edge passes in every fold; an overfit one passes only the years it was tuned on.
Reports both Apex-style intraday trailing (hard) and EOD trailing (lenient).
"""
from __future__ import annotations
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import apex_eval
from apex_portfolio2 import gen_blocks

O = lambda s: np.datetime64(s).astype("datetime64[D]").astype(int)
FOLDS = [(O("2023-06-19"), O("2024-06-19")),
         (O("2024-06-19"), O("2025-06-19")),
         (O("2025-06-19"), O("2026-06-23"))]


def in_fold(recs, lo, hi):
    return [r for r in recs if lo <= r["eday"] < hi]


def out_fold(recs, lo, hi):
    return [r for r in recs if r["eday"] < lo or r["eday"] >= hi]


def expR(recs):
    return float(np.mean([r["pnl_R"] for r in recs])) if recs else 0.0


def best_pass(recs, trail):
    best = (-1, None)
    for R in [150, 200, 250]:
        for k in [None, 3]:
            lim = None if k is None else k * R
            m = apex_eval(recs, R, day_loss_limit=lim, start_step=3, trail=trail)
            if m and m["pass_rate"] > best[0]:
                best = (m["pass_rate"], m)
    return best[1]


def main():
    t0 = time.time()
    print("Generating candidate blocks...", flush=True)
    blocks = gen_blocks()
    print(f"built {len(blocks)} blocks in {time.time()-t0:.0f}s\n", flush=True)

    # per-block sign consistency across the 3 folds
    print("=== per-block expR by fold (Y1 / Y2 / Y3), '+'=positive ===")
    print(f"{'block':<24}{'Y1':>7}{'Y2':>7}{'Y3':>7}  signs")
    stable = []
    for k, recs in sorted(blocks.items()):
        evs = [expR(in_fold(recs, lo, hi)) for (lo, hi) in FOLDS]
        signs = "".join("+" if e > 0.03 else ("." if e > -0.03 else "-") for e in evs)
        if all(e > 0.03 for e in evs):
            stable.append(k)
        print(f"{k:<24}{evs[0]:>+7.2f}{evs[1]:>+7.2f}{evs[2]:>+7.2f}  {signs}")
    print(f"\nblocks +EV in ALL 3 folds: {stable if stable else 'NONE'}")

    # walk-forward: select on the other 2 folds, test on the held-out fold
    print("\n================ WALK-FORWARD (select on 2 yrs, test on held-out yr) ================")
    for ti, (lo, hi) in enumerate(FOLDS, 1):
        sel = [k for k, recs in blocks.items()
               if expR(out_fold(recs, lo, hi)) >= 0.05 and len(out_fold(recs, lo, hi)) >= 30]
        test = []
        for k in sel:
            test.extend(in_fold(blocks[k], lo, hi))
        test.sort(key=lambda r: (r["eday"], r["xday"]))
        e = expR(test)
        mi = best_pass(test, "intraday"); me = best_pass(test, "eod")
        print(f"\n  TEST YEAR {ti}: selected {len(sel)} blocks  | test n={len(test)} expR={e:+.2f}")
        if mi:
            print(f"     intraday-trail (Apex): best pass {mi['pass_rate']*100:.0f}%  "
                  f"(fails {mi['fails']}, cens {mi['censored']}, R=${mi['R']})")
        if me:
            print(f"     eod-trail (lenient):   best pass {me['pass_rate']*100:.0f}%  "
                  f"(fails {me['fails']}, cens {me['censored']}, R=${me['R']})")

    # reference: the (hindsight) stable portfolio over the full 3y
    if stable:
        full = []
        for k in stable:
            full.extend(blocks[k])
        full.sort(key=lambda r: (r["eday"], r["xday"]))
        print(f"\n================ HINDSIGHT stable-blocks portfolio (full 3y, n={len(full)}, expR={expR(full):+.2f}) ===")
        mi = best_pass(full, "intraday"); me = best_pass(full, "eod")
        if mi: print(f"     intraday-trail: best pass {mi['pass_rate']*100:.0f}% (fails {mi['fails']}, R=${mi['R']}, medDays {mi['med_days']})")
        if me: print(f"     eod-trail:      best pass {me['pass_rate']*100:.0f}% (fails {me['fails']}, R=${me['R']}, medDays {me['med_days']})")
    print(f"\n[done {time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
