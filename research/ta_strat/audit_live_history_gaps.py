"""
audit_live_history_gaps.py — the live bot builds Turtle/NR7 levels from ITS OWN merged
frame (3y seed CSV + live bridge CSV). If that merged frame is missing session days, or
a day's high/low differs, the 10-session Turtle window and the NR7 comparison shift
-> different levels -> different trades, even with identical code.

Loads EXACTLY what the live bot loads (lucid_pass_paper._load_local_bridge_market)
and compares its recent daily bars against the Dukascopy historical record.
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
CACHE = os.path.join(HERE, "cache")
sys.path.insert(0, HERE); sys.path.insert(0, PROJ)

import lucid_pass_paper as LIVE
import bt_lucid_10y as BT

N_RECENT = 25


def live_daily(market: str, key: str) -> pd.DataFrame:
    """Exactly what the live bot sees for this component (same loader + prep)."""
    raw = LIVE._load_local_bridge_market(market)          # seed + bridge merge
    frames = LIVE._build_component_frames_from_raw({key: raw}, drop_incomplete=True)
    d = frames[key]
    if d.empty:
        return pd.DataFrame()
    return LIVE._daily(d)


def hist_daily(market: str, bar_min: int) -> pd.DataFrame:
    raw = BT.load_market(market, None, None)
    days = BT.make_days(raw, bar_min)
    return pd.DataFrame([{
        "day": x["day"], "high": float(x["hi"].max()), "low": float(x["lo"].min()),
        "close": float(x["cl"][-1]), "open": float(x["op"][0]),
    } for x in days])


def main():
    for key in ("NQ_TURTLE30", "CL_NR7_30", "ES_VWAP3"):
        c = BT.COMPONENTS[key]
        mkt, bar_min = c["m"], c["bar_min"]
        print(f"\n{'='*80}\n### {key}   (market {mkt}, {bar_min}m bars)\n{'='*80}")
        ld = live_daily(mkt, key)
        hd = hist_daily(mkt, bar_min)
        if ld.empty:
            print("  live frame EMPTY"); continue

        ld["day"] = pd.to_datetime(ld["day"]).dt.date
        hd["day"] = pd.to_datetime(hd["day"]).dt.date
        lset, hset = set(ld["day"]), set(hd["day"])

        # focus on the window that matters: the most recent sessions
        recent_hist = sorted(hset)[-N_RECENT:]
        missing = [d for d in recent_hist if d not in lset]
        extra = sorted([d for d in lset if d > min(recent_hist) and d not in hset])

        print(f"  live frame days: {len(lset)}   historical days: {len(hset)}")
        print(f"  MISSING from live (last {N_RECENT} sessions): {len(missing)}")
        if missing:
            print(f"     -> {[str(d) for d in missing]}")
        print(f"  present in live but NOT historical: {len(extra)}")
        if extra:
            print(f"     -> {[str(d) for d in extra][:8]}")

        # compare daily high/low on shared recent days (drives Turtle & NR7 levels)
        m = ld.merge(hd, on="day", suffixes=("_l", "_h"))
        m = m[m["day"].isin(recent_hist)]
        if m.empty:
            print("  no shared recent days to compare"); continue
        m["dhigh"] = (m["high_l"] - m["high_h"]).abs()
        m["dlow"] = (m["low_l"] - m["low_h"]).abs()
        bad = m[(m["dhigh"] > 1e-6) | (m["dlow"] > 1e-6)]
        print(f"  daily HIGH/LOW mismatches on shared days: {len(bad)}/{len(m)}")
        for _, r in bad.tail(8).iterrows():
            print(f"     {r['day']}  high live {r['high_l']:.2f} vs hist {r['high_h']:.2f} (d={r['dhigh']:.2f})"
                  f" | low live {r['low_l']:.2f} vs hist {r['low_h']:.2f} (d={r['dlow']:.2f})")


if __name__ == "__main__":
    main()
