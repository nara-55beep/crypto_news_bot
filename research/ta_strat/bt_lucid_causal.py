"""
bt_lucid_causal.py — remove the look-ahead from the VWAP components and re-measure.

THE BIAS (confirmed in bt_lucid_10y.sig_vwap and lucid_pass_paper._vwap_signal):
  for i in range(n):
      cum_v += v[i]; cum_pv += tp[i]*v[i]; ...      # bar i's OWN price+volume
      vwap  = cum_pv/cum_v ;  sig = sqrt(var)       # ...define the band
      if hi[i] >= vwap + k*sig:  -> ENTER           # ...then bar i is tested against it
  You cannot know that band until bar i has closed, so you could not have had an order
  resting at it during bar i.

CAUSAL MODEL (what is actually executable):
  band is computed from bars 0..i-1 only. After bar i-1 closes you place a resting limit
  order at that band. During bar i, if price trades through it you are filled, and the
  rest of bar i manages the position normally (the order was already working).

Also runs an ultra-conservative variant where management starts at bar i+1.

Turtle/NR7 levels already come from PRIOR sessions, so they are causal as-is; their
stop uses the running extreme, which is knowable at fill time.

Usage: bt_lucid_causal.py [--start YYYY-MM-DD] [--mode causal|delayed|original]
"""
from __future__ import annotations
import os, sys, math
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bt_lucid_10y as B
from bt_lucid_10y import (MARKETS, COMPONENTS, VWAP_K, VWAP_MIN_BARS, SESSION_END_MIN,
                          START_BALANCE, load_market, make_days, manage,
                          sig_turtle, sig_nr7)

NY = "America/New_York"


def sig_vwap_causal(day: dict):
    """Band from bars strictly BEFORE i; bar i is then tested against it."""
    hi, lo, cl, vol = day["hi"], day["lo"], day["cl"], day["vol"]
    n = len(hi)
    if n - 1 < VWAP_MIN_BARS:
        return None
    tp = (hi + lo + cl) / 3.0
    v = np.where(vol > 0, vol, 1.0)
    cum_v = cum_pv = cum_p2v = 0.0
    for i in range(n):
        # --- band from everything known BEFORE bar i ---
        if cum_v > 0 and i >= VWAP_MIN_BARS:
            vwap = cum_pv / cum_v
            sig = math.sqrt(max(cum_p2v / cum_v - vwap * vwap, 0.0))
            if sig > 0:
                up = vwap + VWAP_K * sig
                dn = vwap - VWAP_K * sig
                if hi[i] >= up:
                    return (i, -1, up, vwap + (VWAP_K + 1.0) * sig, vwap)
                if lo[i] <= dn:
                    return (i, 1, dn, vwap - (VWAP_K + 1.0) * sig, vwap)
        # --- only now fold bar i into the running stats ---
        cum_v += v[i]; cum_pv += tp[i] * v[i]; cum_p2v += tp[i] * tp[i] * v[i]
    return None


def sim_component(key, days, mode):
    c = COMPONENTS[key]
    pv, tick = MARKETS[c["m"]]
    flat_min = SESSION_END_MIN - c["bar_min"]
    kind = c["kind"]
    daily_hist = []
    events = []
    for day in days:
        if kind == "vwap":
            sig = B.sig_vwap(day) if mode == "original" else sig_vwap_causal(day)
        elif kind == "turtle":
            sig = sig_turtle(day, daily_hist, tick)
        else:
            sig = sig_nr7(day, daily_hist, tick)
        if sig is not None:
            e, side, entry, stop, target = sig
            start = e + 1 if mode == "delayed" else e
            if start < len(day["hi"]):
                events += manage(day, start, side, entry, stop, target, pv, tick,
                                 flat_min, key, day["day"])
        daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                           float(day["cl"][-1]), float(day["op"][0])))
    return events


def run(mode, start=None, end=None):
    frames = {m: load_market(m, start, end) for m in MARKETS}
    events = []
    for key, c in COMPONENTS.items():
        if frames[c["m"]].empty:
            continue
        events += sim_component(key, make_days(frames[c["m"]], c["bar_min"]), mode)
    events.sort(key=lambda x: pd.Timestamp(x["ts"]).value)
    return events


def stats(events, label):
    closes = [e for e in events if e["kind"] == "close"]
    if not closes:
        print(f"{label}: no trades"); return None
    tot = np.array([e["trade_total"] for e in closes])
    gp = tot[tot > 0].sum(); gn = -tot[tot <= 0].sum()
    bal = START_BALANCE; peak = bal; mdd = 0.0
    daily = defaultdict(float)
    for e in events:
        bal += e["cash"]; peak = max(peak, bal); mdd = max(mdd, peak - bal)
        daily[pd.Timestamp(e["ts"]).tz_convert(NY).date()] += e["cash"]
    # Lucid 50K eval, start every trading day
    days = sorted(daily); pnl = np.array([daily[d] for d in days])
    npass = nfail = 0; dd = []
    for i0 in range(len(pnl)):
        cum = 0.0; pk = 0.0
        for k in range(i0, min(i0 + 200, len(pnl))):
            cum += pnl[k]
            if cum >= 3000:
                npass += 1; dd.append(k - i0 + 1); break
            pk = max(pk, cum)
            if cum <= min(pk - 2000, 0.0):
                nfail += 1; break
    print(f"\n=== {label} ===")
    print(f"  trades {len(closes)}   win {100*(tot>0).mean():.1f}%   PF {gp/gn if gn>0 else 9.99:.2f}")
    print(f"  net ${bal-START_BALANCE:>+12,.0f}   maxDD -${mdd:>9,.0f}")
    print(f"  Lucid eval: {npass} pass / {nfail} fail  ({100*npass/(npass+nfail) if npass+nfail else 0:.0f}% pass)"
          f"   median {int(np.median(dd)) if dd else -1} days, mean {np.mean(dd) if dd else -1:.1f}")
    return bal - START_BALANCE


def main():
    a = sys.argv[1:]
    start = a[a.index("--start") + 1] if "--start" in a else None
    modes = [a[a.index("--mode") + 1]] if "--mode" in a else ["original", "causal", "delayed"]
    res = {}
    for mode in modes:
        lbl = {"original": "ORIGINAL (band uses the bar it tests = LOOK-AHEAD)",
               "causal": "CAUSAL (band from prior bars only; resting order fills bar i)",
               "delayed": "CAUSAL + management delayed to next bar (ultra-conservative)"}[mode]
        res[mode] = stats(run(mode, start), lbl)
    if "original" in res and "causal" in res and res["original"]:
        print(f"\n  >>> look-ahead accounted for {100*(1-res['causal']/res['original']):.1f}% of the original profit")


if __name__ == "__main__":
    main()
