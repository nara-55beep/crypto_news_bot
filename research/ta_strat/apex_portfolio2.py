"""
apex_portfolio2.py — build a diversified, partial-managed price-action portfolio, keep only the
+EV blocks, and test the Apex $50k pass rate. Includes an honest WALK-FORWARD: blocks are selected
on the first 2 years and the pass rate is measured on the unseen final year, so the result is not
curve-fit to the whole sample.

Management on every block: bank 1/2 at +1R, runner stop -> breakeven, runner target 2R.
Account rule: optional daily loss limit (sit out the rest of the day after -k*R).
"""
from __future__ import annotations
import os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from apex_ict import ict_records
from apex_search import ib_fade, sweep_fade

CUTOFF = np.datetime64("2025-06-19")   # 2y in -> last year is OOS
CUT_ORD = CUTOFF.astype("datetime64[D]").astype(int)


def gen_blocks():
    dfs = {m: load_fut(m) for m in ["es", "nq", "cl"]}
    blocks = {}
    for m in ["es", "nq", "cl"]:
        for tf in ["3min", "5min", "15min"]:
            for kz in [True, False]:
                recs, _ = ict_records(dfs[m], m, tf, killzone=kz, bias_on=False, manage="partial")
                if len(recs) >= 25:
                    blocks[f"ict_{m}_{tf}_{'kz' if kz else 'base'}"] = recs
    for m in ["es", "nq", "cl"]:
        for ib in [30, 60]:
            recs, _ = ib_fade(dfs[m], m, ib_min=ib, ext=0.33, manage="partial")
            if len(recs) >= 25:
                blocks[f"ibfade_{m}_{ib}"] = recs
        recs, _ = sweep_fade(dfs[m], m, ref="pdhl", targ_R=1.5, manage="partial")
        if len(recs) >= 25:
            blocks[f"sweepfade_{m}_pdhl15"] = recs
    return blocks


def expR(recs):
    return float(np.mean([r["pnl_R"] for r in recs])) if recs else 0.0


def slice_recs(recs, lo=None, hi=None):
    return [r for r in recs if (lo is None or r["eday"] >= lo) and (hi is None or r["eday"] < hi)]


def merge(blocks, keys):
    out = []
    for k in keys:
        out.extend(blocks[k])
    out.sort(key=lambda r: (r["eday"], r["xday"]))
    return out


def report(recs, label, risks=(150, 200, 250, 300), dll=(None, 2, 3)):
    print(f"  {label}: n={len(recs)} expR={expR(recs):+.2f} win={np.mean([r['pnl_R']>0 for r in recs])*100:.0f}%")
    print(f"    {'risk':>6}{'dayLim':>8}{'pass%':>7}{'fails':>7}{'cens':>6}{'medDay':>7}")
    best = None
    for R in risks:
        for k in dll:
            lim = None if k is None else k * R
            m = apex_eval(recs, R, day_loss_limit=lim, start_step=3)
            if m is None:
                continue
            tag = "none" if k is None else f"${lim}"
            print(f"    {('$'+str(R)):>6}{tag:>8}{m['pass_rate']*100:>7.0f}{m['fails']:>7}{m['censored']:>6}{(m['med_days'] or 0):>7.0f}")
            if best is None or m["pass_rate"] > best[0]:
                best = (m["pass_rate"], R, lim, m)
    return best


def main():
    t0 = time.time()
    print("Generating candidate blocks (partial-managed)...", flush=True)
    blocks = gen_blocks()
    print(f"built {len(blocks)} blocks in {time.time()-t0:.0f}s\n", flush=True)

    print("=== block expectancy (full / in-sample 2y / out-of-sample 1y) ===")
    print(f"{'block':<24}{'n':>5}{'full':>7}{'IS':>7}{'OOS':>7}")
    is_ev = {}
    for k, recs in sorted(blocks.items()):
        is_r = slice_recs(recs, hi=CUT_ORD); oos_r = slice_recs(recs, lo=CUT_ORD)
        is_ev[k] = (expR(is_r), len(is_r))
        print(f"{k:<24}{len(recs):>5}{expR(recs):>+7.2f}{expR(is_r):>+7.2f}{expR(oos_r):>+7.2f}")

    # ---- selection on IN-SAMPLE only, then test OOS ----
    sel = [k for k, (e, n) in is_ev.items() if e >= 0.05 and n >= 20]
    print(f"\nblocks selected on IS (expR>=0.05, n>=20): {len(sel)}")
    for k in sel:
        print(f"   {k}  (IS expR {is_ev[k][0]:+.2f})")

    full_sel = merge(blocks, sel)
    is_port = slice_recs(full_sel, hi=CUT_ORD)
    oos_port = slice_recs(full_sel, lo=CUT_ORD)

    print("\n================ SELECTED PORTFOLIO — IN-SAMPLE (2023-06..2025-06) ================")
    report(is_port, "IS portfolio")
    print("\n================ SELECTED PORTFOLIO — OUT-OF-SAMPLE (2025-06..2026-06) ================")
    boos = report(oos_port, "OOS portfolio")
    print("\n================ SELECTED PORTFOLIO — FULL 3y ================")
    bfull = report(full_sel, "FULL portfolio")

    print(f"\n[done {time.time()-t0:.0f}s]")
    if bfull:
        pr, R, lim, m = bfull
        print(f"BEST FULL: pass {pr*100:.0f}% @ ${R}/trade, dayLimit {lim}, fails={m['fails']}, "
              f"medDays={m['med_days']}, bestDayShare={ (m['med_bestday_share'] or 0)*100:.0f}%")


if __name__ == "__main__":
    main()
