"""
bt_lucid_nolimit.py — the Lucid basket WITHOUT the once-per-day-per-strategy cap.

Baseline (bt_lucid_10y): each of the 5 engines trades at most once per NY day.
This version lets each engine RE-ENTER: after a trade closes, it keeps scanning the
same day and takes the next signal, repeatedly, until the session ends.

Everything else identical (same signals, same $200-risk mgmt, same costs). Reports the
3-year window and compares to the once-per-day baseline so you can see if lifting the
cap helps or hurts.

Usage: bt_lucid_nolimit.py [--start 2023-06-19] [--end ...]
"""
from __future__ import annotations
import os, sys, math
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bt_lucid_10y as B
from bt_lucid_10y import (MARKETS, COMPONENTS, VWAP_K, VWAP_MIN_BARS, TURTLE_LOOKBACK,
                          TURTLE_RECENCY, TURTLE_BUF_TICKS, SESSION_END_MIN, START_BALANCE,
                          load_market, make_days, manage)


# --- signal scanners with a START index (find first signal at/after `start`) ---
def sig_vwap_from(day, start):
    hi, lo, cl, vol = day["hi"], day["lo"], day["cl"], day["vol"]
    n = len(hi)
    if n - 1 < VWAP_MIN_BARS:
        return None
    tp = (hi + lo + cl) / 3.0
    v = np.where(vol > 0, vol, 1.0)
    cum_v = cum_pv = cum_p2v = 0.0
    for i in range(n):
        cum_v += v[i]; cum_pv += tp[i] * v[i]; cum_p2v += tp[i] * tp[i] * v[i]
        if cum_v <= 0 or i < VWAP_MIN_BARS:
            continue
        vwap = cum_pv / cum_v
        sig = math.sqrt(max(cum_p2v / cum_v - vwap * vwap, 0.0))
        if sig <= 0:
            continue
        up = vwap + VWAP_K * sig; dn = vwap - VWAP_K * sig
        if i < start:
            continue
        if hi[i] >= up:
            return (i, -1, up, vwap + (VWAP_K + 1.0) * sig, vwap)
        if lo[i] <= dn:
            return (i, 1, dn, vwap - (VWAP_K + 1.0) * sig, vwap)
    return None


def sig_turtle_from(day, daily_hist, tick, start):
    if len(daily_hist) < TURTLE_LOOKBACK + TURTLE_RECENCY:
        return None
    look = daily_hist[-TURTLE_LOOKBACK:]
    lows = np.array([x[2] for x in look]); highs = np.array([x[1] for x in look])
    pl = float(lows.min()); ph = float(highs.max())
    nh = len(daily_hist)
    lo_age = nh - 1 - int(np.argmin(lows)); hi_age = nh - 1 - int(np.argmax(highs))
    hi, lo = day["hi"], day["lo"]
    for i in range(start, len(hi)):
        if lo[i] < pl and lo_age >= TURTLE_RECENCY:
            d = 1; entry = pl + TURTLE_BUF_TICKS * tick; stop = float(lo[start:i + 1].min()) - tick
        elif hi[i] > ph and hi_age >= TURTLE_RECENCY:
            d = -1; entry = ph - TURTLE_BUF_TICKS * tick; stop = float(hi[start:i + 1].max()) + tick
        else:
            continue
        r = abs(entry - stop)
        if r <= 0:
            continue
        target = entry + d * 2.0 * r
        for j in range(i, len(hi)):
            if (d > 0 and hi[j] >= entry) or (d < 0 and lo[j] <= entry):
                return (j, d, entry, stop, target)
        return None
    return None


def sig_nr7_from(day, daily_hist, tick, start):
    if len(daily_hist) < 7:
        return None
    last = daily_hist[-1]; prior6 = daily_hist[-7:-1]
    if last[1] - last[2] >= min(x[1] - x[2] for x in prior6):
        return None
    hi_lvl, lo_lvl = last[1], last[2]
    hi, lo = day["hi"], day["lo"]
    for i in range(start, len(hi)):
        if hi[i] >= hi_lvl + tick:
            entry = hi_lvl + tick; stop = lo_lvl - tick; r = abs(entry - stop)
            return (i, 1, entry, stop, entry + 2.0 * r) if r > 0 else None
        if lo[i] <= lo_lvl - tick:
            entry = lo_lvl - tick; stop = hi_lvl + tick; r = abs(entry - stop)
            return (i, -1, entry, stop, entry - 2.0 * r) if r > 0 else None
    return None


def _exit_index(day, events):
    ce = [e for e in events if e["kind"] == "close"]
    if not ce:
        return len(day["hi"]) - 1
    idx = np.where(day["ts"] == ce[-1]["ts"])[0]
    return int(idx[0]) if len(idx) else len(day["hi"]) - 1


def sim_engine_nolimit(key, days):
    c = COMPONENTS[key]; pv, tick = MARKETS[c["m"]]; kind = c["kind"]
    flat_min = SESSION_END_MIN - c["bar_min"]
    daily_hist = []
    trades = []
    for day in days:
        scan = 0
        n = len(day["hi"])
        while scan < n:
            if kind == "vwap":
                sig = sig_vwap_from(day, scan)
            elif kind == "turtle":
                sig = sig_turtle_from(day, daily_hist, tick, scan)
            else:
                sig = sig_nr7_from(day, daily_hist, tick, scan)
            if sig is None:
                break
            e, side, entry, stop, target = sig
            evs = manage(day, e, side, entry, stop, target, pv, tick, flat_min, key, day["day"])
            trades += [x for x in evs if x["kind"] == "close"]
            scan = max(e + 1, _exit_index(day, evs) + 1)
        daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                           float(day["cl"][-1]), float(day["op"][0])))
    return trades


def run(start, end, only=None):
    keys = [only] if only else list(COMPONENTS)
    frames = {c["m"]: load_market(c["m"], start, end) for k, c in COMPONENTS.items() if k in keys}
    events = []
    for key in keys:
        c = COMPONENTS[key]
        if frames[c["m"]].empty:
            continue
        days = make_days(frames[c["m"]], c["bar_min"])
        ev = sim_engine_nolimit(key, days)
        events += ev
        print(f"  {key}: {len(ev)} trades (no daily cap)")
    return events


def report(events, label):
    tot = np.array([e["trade_total"] for e in events])
    n = len(tot); wins = int((tot > 0).sum())
    gp = tot[tot > 0].sum(); gn = -tot[tot <= 0].sum()
    bal = START_BALANCE; peak = bal; mdd = 0.0
    yr = defaultdict(float); monthly = defaultdict(float)
    for e in events:
        bal += e["cash"]; peak = max(peak, bal); mdd = max(mdd, peak - bal)
        d = pd.Timestamp(e["ts"]).tz_convert(B.NY)
        yr[d.year] += e["cash"]; monthly[(d.year, d.month)] += e["cash"]
    negm = sum(1 for v in monthly.values() if v < 0)
    print(f"\n================ {label} ================")
    print(f"trades {n}  win {100*wins/n:.1f}%  PF {gp/gn if gn>0 else 9.99:.2f}")
    print(f"${START_BALANCE:,.0f} -> ${bal:,.2f}  ({(bal/START_BALANCE-1)*100:+.1f}%)  maxDD -${mdd:,.2f}  losing months {negm}/{len(monthly)}")
    for y in sorted(yr):
        print(f"    {y}: ${yr[y]:>+11,.0f}")


def main():
    a = sys.argv[1:]
    start = a[a.index("--start") + 1] if "--start" in a else None
    end = a[a.index("--end") + 1] if "--end" in a else None
    only = a[a.index("--only") + 1] if "--only" in a else None
    ev = run(start, end, only)
    report(ev, f"{only or 'basket'} NO DAILY CAP  {start or 'data-start'} -> {end or 'end'}")


if __name__ == "__main__":
    main()
