"""
verify_tick_fix.py — controlled before/after proof that the O(1) receiver fix works.

The fix was deployed mid-session on 2026-07-30 at ~16:42 UTC. So on that ONE day:
   13:00-16:40 UTC  -> OLD receiver (per-tick CSV rewrite, ~23 ticks/s ceiling)
   16:45-21:00 UTC  -> NEW receiver (O(1), ~19k ticks/s)
Same day, same market conditions, same JForex producer. Comparing each window against
the Dukascopy historical tick files isolates the receiver's effect.
"""
from __future__ import annotations
import os, sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PROJ)
from lucid_pass_paper import _duka_ticks, _duka_rows_to_1m, DUKA_INSTRUMENTS

DAY = "2026-07-30"
CUTOVER_H = 17          # fix deployed ~16:42 UTC; 17:00+ is cleanly "after"
BEFORE = range(13, 16)  # 13,14,15  (old code)
AFTER = range(17, 21)   # 17..20    (new code)


def hist_window(inst, hours):
    fr = []
    for h in hours:
        rows = _duka_ticks(inst, pd.Timestamp(DAY, tz="UTC").replace(hour=h))
        if rows:
            fr.append(_duka_rows_to_1m(rows))
    return pd.concat(fr, ignore_index=True) if fr else pd.DataFrame()


def live_window(market, hours):
    p = os.path.join(PROJ, "data", f"lucid_live_bridge_{market}_1m.csv")
    df = pd.read_csv(p)
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    d0 = pd.Timestamp(DAY, tz="UTC")
    lo, hi = d0 + pd.Timedelta(hours=min(hours)), d0 + pd.Timedelta(hours=max(hours) + 1)
    return df[(df["dt_utc"] >= lo) & (df["dt_utc"] < hi)]


def compare(inst, market, hours, label):
    h = hist_window(inst, hours)
    l = live_window(market, hours)
    if h.empty or l.empty:
        print(f"    {label:<28} (no data)"); return
    m = h.merge(l, on="dt_utc", suffixes=("_h", "_l"))
    if m.empty:
        print(f"    {label:<28} (no overlap)"); return
    volr = m["volume_l"].sum() / m["volume_h"].sum() if m["volume_h"].sum() else float("nan")
    cd = (m["close_h"] - m["close_l"]).abs()
    exact = int((cd < 1e-9).sum())
    print(f"    {label:<28} volume captured {volr*100:>5.1f}%   exact bars {exact:>3}/{len(m):<3} "
          f"({100*exact/len(m):>5.1f}%)   mean close diff {cd.mean():.4f}")


def main():
    print(f"CONTROLLED TEST on {DAY} — same day, fix deployed mid-session ~16:42 UTC\n")
    for _sym, (market, inst) in DUKA_INSTRUMENTS.items():
        print(f"  {market.upper()}:")
        compare(inst, market, BEFORE, "BEFORE fix (13-16 UTC)")
        compare(inst, market, AFTER, "AFTER fix  (17-21 UTC)")
        print()


if __name__ == "__main__":
    main()
