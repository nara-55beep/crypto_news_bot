"""
apex_portfolio.py — combine the genuinely +EV price-action streams into ONE Apex account and
simulate. Diversifying across markets / timeframes / setups gives more (uncorrelated) trades ->
the equity drifts up faster AND smoother -> it reaches +$3,000 before the -$2,500 trailing DD
far more often than any single stream. Sweeps per-trade $ risk and breakeven management.

Building blocks (only blocks shown +EV in the solo search):
  * ICT structural (sweep->MSS->FVG): NQ 15m base, NQ 5m KZ, NQ 3m KZ, CL 15m KZ, CL 5m KZ
  * CL ib_fade 60 (initial-balance extension fade — 68% win, 0 DD breaches solo)
"""
from __future__ import annotations
import os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, apex_eval, trades_to_records
from apex_ict import ict_records
from apex_search import ib_fade, sweep_fade

# ICT structural blocks: (market, tf_rule, killzone, bias)
ICT_BLOCKS = [
    ("nq", "15min", False, False),
    ("nq", "5min", True, False),
    ("nq", "3min", True, False),
    ("cl", "15min", True, False),
    ("cl", "5min", True, False),
    ("es", "15min", False, False),
]


def build_streams(be_at=None):
    streams = {}
    dfs = {m: load_fut(m) for m in ["es", "nq", "cl"]}
    for (m, tf, kz, bias) in ICT_BLOCKS:
        recs, _ = ict_records(dfs[m], m, tf, killzone=kz, bias_on=bias, be_at=be_at)
        if len(recs) >= 15:
            streams[f"ict_{m}_{tf}_{'kz' if kz else 'base'}"] = recs
    # price-action mean-reversion blocks
    recs, _ = ib_fade(dfs["cl"], "cl", ib_min=60, ext=0.33)
    streams["ibfade_cl_60"] = recs
    recs, _ = ib_fade(dfs["nq"], "nq", ib_min=60, ext=0.33)
    streams["ibfade_nq_60"] = recs
    return streams


def merge(streams, keys):
    out = []
    for k in keys:
        out.extend(streams.get(k, []))
    out.sort(key=lambda r: (r["eday"], r["xday"]))
    return out


def summarize(recs):
    if not recs:
        return (0, 0.0, 0.0)
    pnl = np.array([r["pnl_R"] for r in recs])
    win = (pnl > 0).mean()
    exp = pnl.mean()
    return (len(recs), win, exp)


def main():
    t0 = time.time()
    print("Building streams (this resamples + simulates each block on 1m)...", flush=True)
    streams = build_streams(be_at=None)
    streams_be = build_streams(be_at=1.0)   # breakeven-at-1R variant
    print(f"built in {time.time()-t0:.0f}s\n", flush=True)

    print("--- solo blocks (no management) ---")
    print(f"{'block':<24}{'n':>5}{'win%':>6}{'expR':>7}")
    for k, recs in streams.items():
        n, win, exp = summarize(recs)
        print(f"{k:<24}{n:>5}{win*100:>5.0f}{exp:>7.2f}")

    PORTS = {
        "ALL (ict+ibfade)": list(streams.keys()),
        "ICT only": [k for k in streams if k.startswith("ict")],
        "ICT (no ES)": [k for k in streams if k.startswith("ict") and "_es_" not in k],
        "NQ+CL ict + cl ibfade60": [k for k in streams if ("_nq_" in k or "_cl_" in k)],
    }
    for tag, mgmt in [("no mgmt", streams), ("BE@1R", streams_be)]:
        print(f"\n================ PORTFOLIOS — {tag} ================")
        print(f"{'portfolio':<26}{'risk':>6}{'trades':>7}{'win%':>6}{'pass%':>7}{'fails':>7}{'cens':>6}{'medDay':>7}{'bestDay%':>9}")
        for pname, keys in PORTS.items():
            recs = merge(mgmt, keys)
            n, win, exp = summarize(recs)
            for R in [150, 200, 250, 300, 400]:
                m = apex_eval(recs, R)
                if m is None:
                    continue
                print(f"{pname:<26}{('$'+str(R)):>6}{n:>7}{win*100:>5.0f}{m['pass_rate']*100:>7.0f}"
                      f"{m['fails']:>7}{m['censored']:>6}{(m['med_days'] or 0):>7.0f}{(m['med_bestday_share'] or 0)*100:>8.0f}%")
            print()


if __name__ == "__main__":
    main()
