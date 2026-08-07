"""
audit_data_equivalence.py — the strategy code is identical (audit_live_vs_backtest.py
proved 43/43). So if live differs from backtest, it must be the DATA.

VWAP is cumulative from the FIRST bar of the session. If the live bridge is missing
early-session bars (JForex started late / feed dropped), the VWAP and its sigma bands
are computed on a DIFFERENT sample than the historical backtest -> different signals.

Compares, per market:
  - bars per session day: historical seed vs live bridge
  - session START coverage (does each live day begin at 13:00 UTC?)
  - volume magnitude (VWAP is volume-weighted)
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
CACHE = os.path.join(HERE, "cache")
SESSION_START, SESSION_END = 13 * 60, 21 * 60


def sess(df):
    d = df.copy()
    d["dt_utc"] = pd.to_datetime(d["dt_utc"], utc=True)
    m = d["dt_utc"].dt.hour * 60 + d["dt_utc"].dt.minute
    d = d[(m >= SESSION_START) & (m < SESSION_END)].copy()
    d["day"] = d["dt_utc"].dt.tz_convert("America/New_York").dt.date
    d["min"] = m
    return d


def main():
    print("=" * 78)
    print("DATA AUDIT — historical (backtest) vs live bridge (paper bot)")
    print("=" * 78)
    for mkt in ("es", "nq", "cl"):
        hist = sess(pd.read_csv(os.path.join(CACHE, f"{mkt}_1m_3y.csv"),
                                usecols=["dt_utc", "close", "volume"]))
        bpath = os.path.join(PROJ, "data", f"lucid_live_bridge_{mkt}_1m.csv")
        if not os.path.exists(bpath):
            print(f"\n{mkt.upper()}: no bridge file"); continue
        live = sess(pd.read_csv(bpath, usecols=["dt_utc", "close", "volume"]))

        hg = hist.groupby("day")
        lg = live.groupby("day")
        h_bars = hg.size()
        l_bars = lg.size()
        h_start = hg["min"].min()
        l_start = lg["min"].min()

        print(f"\n### {mkt.upper()} ###")
        print(f"  historical: {len(h_bars)} session days, median {h_bars.median():.0f} bars/day, "
              f"median session start {h_start.median():.0f} min UTC ({int(h_start.median())//60}:{int(h_start.median())%60:02d})")
        print(f"  live bridge: {len(l_bars)} session days, median {l_bars.median():.0f} bars/day, "
              f"median session start {l_start.median():.0f} min UTC ({int(l_start.median())//60}:{int(l_start.median())%60:02d})")
        print(f"  volume: historical median/bar {hist['volume'].median():.3f} | live median/bar {live['volume'].median():.3f}")

        # per-day live detail: did each day start at 13:00 and have full bars?
        print(f"  {'day':<12}{'bars':>6}{'start UTC':>11}{'LATE START?':>13}{'SHORT DAY?':>12}")
        full = int(h_bars.median())
        for day in sorted(l_bars.index):
            n = int(l_bars[day]); st = int(l_start[day])
            late = st > SESSION_START + 5
            short = n < full * 0.9
            print(f"  {str(day):<12}{n:>6}{f'{st//60}:{st%60:02d}':>11}"
                  f"{('YES +' + str(st - SESSION_START) + 'm') if late else 'no':>13}"
                  f"{('YES -' + str(full - n) + ' bars') if short else 'no':>12}")


if __name__ == "__main__":
    main()
