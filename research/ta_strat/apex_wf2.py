"""
apex_wf2.py — k-fold walk-forward Apex test on the broad NON-ICT web strategy batch.
Per block (strategy x market): expR by 3 one-year folds + sign pattern, then portfolio of the
blocks that are +EV in ALL 3 folds (the only ones with a chance of a real edge), Apex-tested
under intraday (hard) and EOD (lenient) trailing.
"""
from __future__ import annotations
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from apex_strats2 import STRATS
from apex_walkforward import FOLDS, in_fold, out_fold, expR, best_pass

MARKETS = ["es", "nq", "cl"]


def gen_blocks(manage="none"):
    blocks = {}
    dfs = {m: load_fut(m) for m in MARKETS}
    for m in MARKETS:
        for nm, fn in STRATS.items():
            try:
                recs, _ = fn(dfs[m], m, manage=manage)
            except TypeError:
                recs, _ = fn(dfs[m], m)
            if len(recs) >= 25:
                blocks[f"{nm}_{m}"] = recs
    return blocks


def main():
    t0 = time.time()
    print("Generating blocks for all web strategies x ES/NQ/CL...", flush=True)
    blocks = gen_blocks(manage="partial")
    print(f"built {len(blocks)} blocks in {time.time()-t0:.0f}s\n", flush=True)

    print("=== per-block expR by fold (Y1/Y2/Y3) — '+' = +EV that year ===")
    print(f"{'block':<22}{'n':>5}{'Y1':>7}{'Y2':>7}{'Y3':>7}  signs")
    stable = []
    rows = []
    for k, recs in sorted(blocks.items()):
        evs = [expR(in_fold(recs, lo, hi)) for (lo, hi) in FOLDS]
        signs = "".join("+" if e > 0.03 else ("." if e > -0.03 else "-") for e in evs)
        rows.append((k, len(recs), evs, signs))
        if all(e > 0.03 for e in evs):
            stable.append(k)
    for k, n, evs, signs in rows:
        print(f"{k:<22}{n:>5}{evs[0]:>+7.2f}{evs[1]:>+7.2f}{evs[2]:>+7.2f}  {signs}")
    print(f"\nblocks +EV in ALL 3 folds: {stable if stable else 'NONE'}")

    # walk-forward: select on 2 folds by expR, test held-out fold (portfolio of selected)
    print("\n=== WALK-FORWARD (select +EV on 2 yrs, test held-out yr) ===")
    for ti, (lo, hi) in enumerate(FOLDS, 1):
        sel = [k for k, recs in blocks.items()
               if expR(out_fold(recs, lo, hi)) >= 0.06 and len(out_fold(recs, lo, hi)) >= 30]
        test = []
        for k in sel:
            test.extend(in_fold(blocks[k], lo, hi))
        test.sort(key=lambda r: (r["eday"], r["xday"]))
        if not test:
            print(f"  TEST YEAR {ti}: selected {len(sel)} blocks, no test trades"); continue
        e = expR(test); mi = best_pass(test, "intraday"); me = best_pass(test, "eod")
        print(f"  TEST YEAR {ti}: {len(sel)} blocks | n={len(test)} expR={e:+.2f} | "
              f"intraday pass {mi['pass_rate']*100:.0f}% (fails {mi['fails']}) | "
              f"eod pass {me['pass_rate']*100:.0f}% (fails {me['fails']})")

    if stable:
        full = []
        for k in stable:
            full.extend(blocks[k])
        full.sort(key=lambda r: (r["eday"], r["xday"]))
        print(f"\n=== stable-blocks portfolio (full 3y, n={len(full)}, expR={expR(full):+.2f}) ===")
        for trail in ["intraday", "eod"]:
            m = best_pass(full, trail)
            print(f"   {trail}: pass {m['pass_rate']*100:.0f}% fails={m['fails']} cens={m['censored']} "
                  f"R=${m['R']} medDays={m['med_days']}")
    print(f"\n[done {time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
