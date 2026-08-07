"""
verify_turtle_nr7_causality.py — test the claim that Turtle+NR7 are NOT causally clean.

TURTLE: the 30m code sees a sweep (low < prior_low) on bar i, then searches for a
recovery fill (high >= entry) starting at bar i ITSELF. Within one 30m candle you cannot
know the low happened before the recovery high. Drill into 1-MINUTE bars and resolve the
true ordering; split P&L into "same-minute (unresolvable)" vs "recovery in a later minute".

NR7: entry is the prior-session extreme +/- 1 tick, but the code never checks whether the
new session OPENED beyond that level (overnight gap) - in which case that price was never
available and you would have filled at the open instead.
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bt_lucid_10y import (MARKETS, COMPONENTS, TURTLE_LOOKBACK, TURTLE_RECENCY,
                          TURTLE_BUF_TICKS, SESSION_END_MIN, load_market, make_days, manage)


def one_min_days(market, start=None):
    raw = load_market(market, start, None)
    return {d["day"]: d for d in make_days(raw, 1)}


def analyse_turtle(start=None):
    key = "NQ_TURTLE30"; c = COMPONENTS[key]
    pv, tick = MARKETS[c["m"]]
    raw = load_market(c["m"], start, None)
    days30 = make_days(raw, 30)
    m1 = one_min_days(c["m"], start)
    flat_min = SESSION_END_MIN - 30

    daily_hist = []
    same_minute, later_minute, no_fill = [], [], []
    for day in days30:
        if len(daily_hist) >= TURTLE_LOOKBACK + TURTLE_RECENCY:
            look = daily_hist[-TURTLE_LOOKBACK:]
            lows = np.array([x[2] for x in look]); highs = np.array([x[1] for x in look])
            pl, ph = float(lows.min()), float(highs.max())
            nh = len(daily_hist)
            lo_age = nh - 1 - int(np.argmin(lows)); hi_age = nh - 1 - int(np.argmax(highs))
            hi, lo = day["hi"], day["lo"]
            sig = None
            for i in range(len(hi)):
                if lo[i] < pl and lo_age >= TURTLE_RECENCY:
                    d = 1; entry = pl + TURTLE_BUF_TICKS * tick; stop = float(lo[:i + 1].min()) - tick
                elif hi[i] > ph and hi_age >= TURTLE_RECENCY:
                    d = -1; entry = ph - TURTLE_BUF_TICKS * tick; stop = float(hi[:i + 1].max()) + tick
                else:
                    continue
                r = abs(entry - stop)
                if r <= 0:
                    continue
                target = entry + d * 2.0 * r
                for j in range(i, len(hi)):
                    if (d > 0 and hi[j] >= entry) or (d < 0 and lo[j] <= entry):
                        sig = (i, j, d, entry, stop, target); break
                break
            if sig:
                i, j, d, entry, stop, target = sig
                ev = manage(day, j, d, entry, stop, target, pv, tick, flat_min, key, day["day"])
                pnl = sum(x["trade_total"] for x in ev if x["kind"] == "close")
                # --- resolve ordering with 1-minute bars inside the SIGNAL 30m bar ---
                md = m1.get(day["day"])
                bucket = later_minute
                if md is not None and j == i:
                    t0 = pd.Timestamp(day["ts"][i]); t1 = t0 + pd.Timedelta(minutes=30)
                    ts = pd.to_datetime(md["ts"])
                    sel = (ts >= t0) & (ts < t1)
                    mh, ml = md["hi"][sel], md["lo"][sel]
                    sweep_k = rec_k = None
                    for k in range(len(mh)):
                        if sweep_k is None:
                            if (d > 0 and ml[k] < pl) or (d < 0 and mh[k] > ph):
                                sweep_k = k
                                # recovery in the SAME minute?
                                if (d > 0 and mh[k] >= entry) or (d < 0 and ml[k] <= entry):
                                    rec_k = k
                                    break
                        else:
                            if (d > 0 and mh[k] >= entry) or (d < 0 and ml[k] <= entry):
                                rec_k = k; break
                    if sweep_k is None or rec_k is None:
                        bucket = no_fill
                    elif rec_k == sweep_k:
                        bucket = same_minute
                bucket.append(pnl)
        daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                           float(day["cl"][-1]), float(day["op"][0])))

    print("=== TURTLE SOUP: intrabar ordering resolved at 1-minute resolution ===")
    tot = len(same_minute) + len(later_minute) + len(no_fill)
    print(f"  total trades                                : {tot}")
    print(f"  sweep & recovery in the SAME MINUTE (unresolvable): {len(same_minute):>4}  P&L ${sum(same_minute):>+11,.0f}")
    print(f"  recovery in a LATER minute (causally OK)          : {len(later_minute):>4}  P&L ${sum(later_minute):>+11,.0f}")
    print(f"  never actually fillable at 1m                     : {len(no_fill):>4}  P&L ${sum(no_fill):>+11,.0f}")


def analyse_nr7(start=None):
    key = "CL_NR7_30"; c = COMPONENTS[key]
    pv, tick = MARKETS[c["m"]]
    raw = load_market(c["m"], start, None)
    days = make_days(raw, 30)
    flat_min = SESSION_END_MIN - 30
    daily_hist = []
    gapped = clean = 0
    pnl_thresh = pnl_gap = 0.0
    for day in days:
        if len(daily_hist) >= 7:
            last = daily_hist[-1]; prior6 = daily_hist[-7:-1]
            if last[1] - last[2] < min(x[1] - x[2] for x in prior6):
                hi_lvl, lo_lvl = last[1], last[2]
                hi, lo, op = day["hi"], day["lo"], day["op"]
                for i in range(len(hi)):
                    if hi[i] >= hi_lvl + tick:
                        d, entry = 1, hi_lvl + tick
                    elif lo[i] <= lo_lvl - tick:
                        d, entry = -1, lo_lvl - tick
                    else:
                        continue
                    stop = (lo_lvl - tick) if d > 0 else (hi_lvl + tick)
                    r = abs(entry - stop)
                    if r <= 0:
                        break
                    target = entry + d * 2.0 * r
                    ev = manage(day, i, d, entry, stop, target, pv, tick, flat_min, key, day["day"])
                    p = sum(x["trade_total"] for x in ev if x["kind"] == "close")
                    pnl_thresh += p
                    # did the session OPEN beyond the level? then that price never existed
                    o0 = float(op[0])
                    if (d > 0 and o0 > entry) or (d < 0 and o0 < entry):
                        gapped += 1
                        ev2 = manage(day, 0, d, o0, stop, target, pv, tick, flat_min, key, day["day"])
                        pnl_gap += sum(x["trade_total"] for x in ev2 if x["kind"] == "close")
                    else:
                        clean += 1
                        pnl_gap += p
                    break
        daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                           float(day["cl"][-1]), float(day["op"][0])))
    print("\n=== NR7: was the modelled entry price actually available? ===")
    print(f"  entries where the session OPENED BEYOND the level (gap): {gapped} of {gapped+clean}")
    print(f"  P&L filling at the threshold (current model): ${pnl_thresh:>+11,.0f}")
    print(f"  P&L filling at the available open (gap-aware): ${pnl_gap:>+11,.0f}")


if __name__ == "__main__":
    s = sys.argv[sys.argv.index("--start") + 1] if "--start" in sys.argv else None
    analyse_turtle(s)
    analyse_nr7(s)
