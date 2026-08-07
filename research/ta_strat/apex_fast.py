"""
apex_fast.py — backtest the HIGH-FREQUENCY "pass fast" archetypes the web surfaced:
  * tight_scalp  — small target / big stop momentum (the "passed Apex in a day" archetype)
  * consec_rev   — fade after N consecutive 1-min closes (high-frequency mean reversion)
  * vwap_fade    — at k=1.0/1.5 (more frequent than 2sigma)
Tested for 3-fold robustness (+EV all years?) and 1-month EOD pass speed, on ES/NQ/CL.
"""
from __future__ import annotations
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, walk_trade, walk_trade_partial, trades_to_records, TICK, apex_eval
from apex_strats2 import sessions, vwap_fade
from apex_walkforward import FOLDS, in_fold, expR

RAW = {m: load_fut(m) for m in ["es", "nq", "cl"]}
MH = 480


def consec_rev(df, name, n=3, targ_R=1.0, buf=1, manage="partial"):
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); tick = TICK[name]; trades = []
    for (_, a, b) in sess:
        run = 0; last = 0; m = a + 1
        while m <= b:
            d = 1 if C[m] > C[m - 1] else (-1 if C[m] < C[m - 1] else 0)
            if d != 0 and d == last:
                run += 1
            elif d != 0:
                run = 1; last = d
            if run >= n:
                fi = m + 1
                if fi > b:
                    break
                dirn = -last   # fade: after down-run go long
                if dirn > 0:
                    entry = O[fi]; stop = L[m - n + 1:m + 1].min() - buf * tick
                else:
                    entry = O[fi]; stop = H[m - n + 1:m + 1].max() + buf * tick
                risk = abs(entry - stop)
                if risk > 0:
                    target = entry + dirn * targ_R * risk
                    end = min(b + 1, fi + MH)
                    r = walk_trade_partial(H, L, C, fi, dirn, entry, stop, target, end, 1.0, 0.5) \
                        if manage == "partial" else walk_trade(H, L, C, fi, dirn, entry, stop, target, end)
                    if r:
                        xb, rsn, p, ma, mf = r
                        trades.append((fi, dirn, entry, stop, target, xb, rsn, p, ma, mf))
                        m = xb + 1; run = 0; last = 0; continue
                run = 0; last = 0
            m += 1
    return trades_to_records(trades, df, name)


def tight_scalp(df, name, tgt_t=10, stop_t=30, lb=5, manage="none"):
    """Break of last `lb` bars' extreme -> tiny target, big stop. The viral 'pass-in-a-day' archetype."""
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); tick = TICK[name]; trades = []
    for (_, a, b) in sess:
        m = a + lb
        while m <= b:
            hh = H[m - lb:m].max(); ll = L[m - lb:m].min(); dirn = 0
            if H[m] >= hh:
                dirn = 1; entry = hh
            elif L[m] <= ll:
                dirn = -1; entry = ll
            if dirn != 0:
                stop = entry - dirn * stop_t * tick; target = entry + dirn * tgt_t * tick
                end = min(b + 1, m + MH)
                r = walk_trade(H, L, C, m, dirn, entry, stop, target, end)
                if r:
                    xb, rsn, p, ma, mf = r
                    trades.append((m, dirn, entry, stop, target, xb, rsn, p, ma, mf))
                    m = xb + 1; continue
            m += 1
    return trades_to_records(trades, df, name)


def pass1mo(recs):
    best = None
    for R in [200, 300, 400, 600, 800]:
        m = apex_eval(recs, R, start_step=2, horizon_days=30, trail="eod")
        if not m:
            continue
        tot = m["passes"] + m["fails"] + m["censored"]
        pr = m["passes"] / tot if tot else 0
        if best is None or pr > best[0]:
            best = (pr, R, m["fails"], m["med_days"])
    return best


def row(name, recs):
    if len(recs) < 25:
        print(f"  {name:<26} (only {len(recs)} trades)"); return
    evs = [expR(in_fold(recs, lo, hi)) for (lo, hi) in FOLDS]
    signs = "".join("+" if e > 0.03 else ("." if e > -0.03 else "-") for e in evs)
    w = float(np.mean([r["pnl_R"] > 0 for r in recs])) * 100
    e = expR(recs)
    bp = pass1mo(recs)
    p1 = f"{bp[0]*100:.0f}%@${bp[1]} (blow {bp[2]}, med {bp[3]})" if bp else "-"
    print(f"  {name:<26} n={len(recs):>5} win={w:>3.0f}% expR={e:+.2f} folds[{signs}]  1mo:{p1}")


def main():
    print("HIGH-FREQUENCY 'fast pass' archetypes — robustness + 1-month EOD pass\n")
    for m in ["es", "nq", "cl"]:
        print(f"[{m.upper()}]")
        row(f"tight_scalp 10t/30t", tight_scalp(RAW[m], m, 10, 30)[0])
        row(f"tight_scalp 8t/24t", tight_scalp(RAW[m], m, 8, 24)[0])
        row(f"consec_rev n=2 1R", consec_rev(RAW[m], m, 2, 1.0)[0])
        row(f"consec_rev n=3 1R", consec_rev(RAW[m], m, 3, 1.0)[0])
        row(f"consec_rev n=3 1.5R", consec_rev(RAW[m], m, 3, 1.5)[0])
        row(f"vwap_fade k=1.0", vwap_fade(RAW[m], m, k_band=1.0, manage="partial")[0])
        row(f"vwap_fade k=1.5", vwap_fade(RAW[m], m, k_band=1.5, manage="partial")[0])


if __name__ == "__main__":
    main()
