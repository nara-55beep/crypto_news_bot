"""
audit_feed_vs_history.py — THE gap the earlier audits missed.

The replay proved: live bot == backtest engine WHEN BOTH ARE FED THE BRIDGE DATA.
But the 10-year backtest was built on Dukascopy HISTORICAL .bi5 tick files, while the
live bot runs on JForex STREAMING ticks. If those two sources produce different 1m
bars, then the live bot is trading a different data series than the one that produced
the "+1302% / 12-day pass" numbers - even though the code is identical.

This downloads the historical .bi5 bars for the days the bot traded live and compares
them bar-by-bar with the bridge bars: prices (OHLC) and volume (VWAP is volume-weighted).
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PROJ)

from lucid_pass_paper import _duka_ticks, _duka_rows_to_1m, DUKA_INSTRUMENTS

DAYS = ["2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29"]
HOURS = range(13, 21)


def fetch_hist(inst, day):
    frames = []
    for h in HOURS:
        rows = _duka_ticks(inst, pd.Timestamp(day, tz="UTC").replace(hour=h))
        if rows:
            frames.append(_duka_rows_to_1m(rows))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def load_bridge(market):
    p = os.path.join(PROJ, "data", f"lucid_live_bridge_{market}_1m.csv")
    df = pd.read_csv(p)
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    return df


def main():
    for symbol, (market, inst) in DUKA_INSTRUMENTS.items():
        bridge_all = load_bridge(market)
        print(f"\n{'='*76}\n### {market.upper()}  ({inst})\n{'='*76}")
        print(f"{'day':<12}{'bars H/L':>12}{'common':>8}{'close diff':>22}{'volume ratio':>16}")
        for day in DAYS:
            hist = fetch_hist(inst, day)
            if hist.empty:
                print(f"{day:<12}{'no hist data':>12}")
                continue
            d0 = pd.Timestamp(day, tz="UTC")
            live = bridge_all[(bridge_all["dt_utc"] >= d0 + pd.Timedelta(hours=13)) &
                              (bridge_all["dt_utc"] < d0 + pd.Timedelta(hours=21))]
            m = hist.merge(live, on="dt_utc", suffixes=("_h", "_l"))
            if m.empty:
                print(f"{day:<12}{len(hist):>5}/{len(live):<6}{'0':>8}  (no overlapping bars)")
                continue
            cdiff = (m["close_h"] - m["close_l"]).abs()
            vol_ratio = (m["volume_l"].sum() / m["volume_h"].sum()) if m["volume_h"].sum() else float("nan")
            exact = int((cdiff < 1e-9).sum())
            print(f"{day:<12}{len(hist):>5}/{len(live):<6}{len(m):>8}"
                  f"   max {cdiff.max():>8.4f} mean {cdiff.mean():>7.4f}"
                  f"   live/hist {vol_ratio:>6.2f}x  (exact bars {exact}/{len(m)})")


if __name__ == "__main__":
    main()
