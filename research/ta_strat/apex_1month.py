"""
apex_1month.py — the REAL constraint: pass Apex within ~1 month or pay again. So "didn't reach
+$3,000 within the horizon" now counts as a FAIL (you re-pay), same as a DD breach. Measures the
honest 1-month pass rate = P(hit +$3k within HORIZON trading-days AND never breach the $2,500 trail)
for each candidate portfolio across escalating per-trade risk. Speed needs size or frequency -> this
exposes the speed-vs-blowup trade-off directly.
"""
from __future__ import annotations
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval
from apex_strats2 import STRATS
from apex_swing import SWING

DFS = {m: load_fut(m) for m in ["es", "nq", "cl"]}
HORIZONS = {"~1mo (30d)": 30, "~6wk (45d)": 45}


def blk(stratmap, nm, mkt, **kw):
    return stratmap[nm](DFS[mkt], mkt, **kw)[0]


def merge(*ls):
    out = []
    for L in ls:
        out.extend(L)
    out.sort(key=lambda r: (r["eday"], r["xday"]))
    return out


def onemonth(recs, horizon, trail="intraday"):
    """Best 1-month pass rate across risk/day-limit. pass = reach +3k within horizon, no breach."""
    best = None
    for R in [200, 300, 400, 500, 600, 800, 1000]:
        for k in [None, 2, 3]:
            lim = None if k is None else k * R
            m = apex_eval(recs, R, day_loss_limit=lim, start_step=2, horizon_days=horizon, trail=trail)
            if not m:
                continue
            total = m["passes"] + m["fails"] + m["censored"]
            pr = m["passes"] / total if total else 0
            cand = dict(R=R, lim=lim, pr=pr, passes=m["passes"], fails=m["fails"],
                        cens=m["censored"], total=total, med=m["med_days"])
            if best is None or cand["pr"] > best["pr"]:
                best = cand
    return best


def main():
    # build candidate portfolios
    nr7 = merge(*[blk(STRATS, "nr7_orb", m, manage="partial") for m in ["es", "nq", "cl"]])
    nqmr = merge(blk(STRATS, "vwap_fade", "nq", manage="partial"),
                 blk(STRATS, "turtle_soup", "nq", manage="partial"),
                 blk(STRATS, "eighty_twenty", "nq", manage="partial"))
    allintra = merge(nr7, nqmr)
    # kitchen sink: every +EV-ish intraday block (max frequency for a 1-month sprint)
    ks = []
    for m in ["es", "nq", "cl"]:
        for nm in ["nr7_orb", "vwap_fade", "turtle_soup", "eighty_twenty", "lw_breakout", "stretch_orb"]:
            r = blk(STRATS, nm, m, manage="partial")
            if len(r) >= 25 and np.mean([x["pnl_R"] for x in r]) > 0:
                ks.extend(r)
    ks.sort(key=lambda r: (r["eday"], r["xday"]))
    swing = merge(blk(SWING, "double_seven", "es"), blk(SWING, "double_seven", "nq"),
                  blk(SWING, "three_down", "es"), blk(SWING, "three_down", "nq"),
                  blk(SWING, "ibs_daily", "es"), blk(SWING, "ibs_daily", "nq"))

    ports = {"NR7 (ES+NQ+CL)": nr7, "NR7 + NQ-MR": allintra,
             "Kitchen-sink intraday +EV": ks, "Swing MR (overnight acct)": swing}

    for trail in ["intraday", "eod"]:
        print(f"\n############### TRAILING MODEL: {trail.upper()} ###############")
        for hname, hz in HORIZONS.items():
            print(f"\n======== PASS WITHIN {hname} ({trail}) ========")
            print(f"{'portfolio':<28}{'n':>5}{'bestRisk':>9}{'dayLim':>8}{'PASS%':>7}{'blowups':>8}{'tooSlow':>8}{'medDays':>8}")
            for pname, recs in ports.items():
                b = onemonth(recs, hz, trail=trail)
                if not b:
                    print(f"{pname:<28} (none)"); continue
                print(f"{pname:<28}{len(recs):>5}{('$'+str(b['R'])):>9}{str(b['lim']):>8}{b['pr']*100:>6.0f}%"
                      f"{b['fails']:>8}{b['cens']:>8}{(b['med'] or 0):>8.0f}")
    print("\nPASS% = reached +$3,000 within the window AND never breached the $2,500 trail.")
    print("blowups = DD breaches; tooSlow = didn't reach target in time. INTRADAY = hard (unrealized")
    print("spikes ratchet the floor); EOD = floor only updates on end-of-day balance (far easier).")


if __name__ == "__main__":
    main()
