"""
bt_nr7_btc.py — the NR7 Breakout Apex strategy ported to BTC, 3 years. Same logic as the ES/NQ/CL
version: NR7 day = narrowest High-Low of the last 7 days (UTC days for 24/7 BTC); next day break the
NR7 day's High (long) / Low (short), first hit = entry, stop = opposite end (=R); manage 1/2 off at
+1R -> stop to breakeven -> runner to +2R; flat at day close. Executed on 5m bars.
$100 start, all-in. Run at 1x (no leverage) and 20x. 2 bps/side cost.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import walk_trade_partial

START = 100.0
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def load_btc(tf="5min"):
    df = pd.read_csv(os.path.join(CACHE, "BTC_1m_max.csv"), index_col=0, parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.resample(tf, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def run(df, lev, target_r=2.0, cost_bps=2.0):
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    n = len(C)
    days_norm = df.index.tz_convert("UTC").normalize()
    udays, inv = np.unique(days_norm.values, return_inverse=True)
    nd = len(udays)
    day_hi = np.full(nd, -1e18); day_lo = np.full(nd, 1e18)
    day_first = np.full(nd, -1, np.int64); day_last = np.full(nd, -1, np.int64)
    for i in range(n):
        d = inv[i]
        if day_first[d] < 0: day_first[d] = i
        day_last[d] = i
        if H[i] > day_hi[d]: day_hi[d] = H[i]
        if L[i] < day_lo[d]: day_lo[d] = L[i]
    day_rng = day_hi - day_lo

    eq = START; peak = START; ec = [START]; trades = []; blew = None
    cost = lev * cost_bps / 1e4 * 2
    for d in range(7, nd):
        rp = day_rng[d - 1]
        if not all(rp < day_rng[d - 1 - k] for k in range(1, 7)):
            continue
        hi_lvl, lo_lvl = day_hi[d - 1], day_lo[d - 1]
        a, b = int(day_first[d]), int(day_last[d])
        fi = dirn = entry = stop = None
        for m in range(a, b + 1):
            if H[m] >= hi_lvl:
                fi, dirn, entry, stop = m, 1, hi_lvl, lo_lvl; break
            if L[m] <= lo_lvl:
                fi, dirn, entry, stop = m, -1, lo_lvl, hi_lvl; break
        if fi is None:
            continue
        R = abs(entry - stop); sf = R / entry
        if R <= 0:
            continue
        tgt = entry + dirn * target_r * R
        r = walk_trade_partial(H, L, C, fi, dirn, entry, stop, tgt, b + 1, p1=1.0, frac=0.5)
        if r is None:
            continue
        _, rsn, pnl_R, _, _ = r
        price_ret = pnl_R * sf                      # R-multiple -> actual % price move
        eq = max(0.0, eq * (1 + lev * price_ret) - eq * cost)
        peak = max(peak, eq); ec.append(eq)
        trades.append({"side": "long" if dirn > 0 else "short", "pnl_R": pnl_R,
                       "ret": price_ret, "reason": rsn, "eq": eq})
        if eq < 1.0 and blew is None:
            blew = udays[d]
    tr = pd.DataFrame(trades)
    e = np.array(ec); pk = np.maximum.accumulate(e); dd = ((e - pk) / pk).min() * 100 if len(e) > 1 else 0
    return dict(tr=tr, eq=eq, peak=peak, dd=dd, blew=blew)


def report(name, r):
    tr = r["tr"]
    if not len(tr):
        print(f"{name:<22} no trades"); return
    win = (tr["pnl_R"] > 0).mean() * 100
    bl = ""
    if r["blew"] is not None:
        bl = "  BLEW UP " + str(np.datetime_as_string(r["blew"], unit="D"))
    print(f"{name:<22} {len(tr):>4} trades  win {win:>4.1f}%  final ${r['eq']:>13,.2f}  "
          f"peak ${r['peak']:>12,.0f}  maxDD {r['dd']:>5.0f}%{bl}")
    print(f"{'':<22} avg {tr['ret'].mean()*100:+.3f}% price/trade · "
          f"avg {tr['pnl_R'].mean():+.2f}R · exits {dict(tr['reason'].value_counts())}")


def main():
    df = load_btc("5min")
    print(f"NR7 Breakout Apex — BTC 5m {df.index[0].date()}->{df.index[-1].date()} "
          f"({len(df):,} 5m bars), $100 start, all-in, 2bps/side\n")
    report("1x (no leverage)", run(df, 1.0))
    report("20x leverage", run(df, 20.0))


if __name__ == "__main__":
    main()
