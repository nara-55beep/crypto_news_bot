"""
apex_swing.py — DAILY mean-reversion strategies (the strongest documented edges: IBS, Double Seven,
3-Days-Down, sigma-spike fade, Turnaround Tuesday). They hold overnight (so they need a swing/
overnight-allowed account, NOT the standard flat-by-close Apex eval) — but they have the best shot
at a real, smooth, trailing-DD-surviving edge, so worth measuring. Built on synthetic RTH daily bars
from the 1-min stream; sized with R = 1x ATR(20) of the daily range; mae/mfe from held-day extremes.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, cal_ordinal, TICK
from apex_strats2 import sessions, daily_ohlc


def _daily(df):
    sess = sessions(df)
    o, h, l, c = daily_ohlc(df, sess)
    days = np.array([d for (d, a, b) in sess])
    last_bar = np.array([b for (d, a, b) in sess])
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr = pd.Series(tr).rolling(20, min_periods=5).mean().values
    dow = pd.to_datetime(df.index[last_bar].tz_convert("America/New_York").date).dayofweek
    return sess, o, h, l, c, days, last_bar, atr, np.array(dow)


def _records(df, name, trades, o, h, l, c, days, last_bar, atr):
    """trades: list of (i_entry_day, i_exit_day, d). Build R-records (close-to-close)."""
    cost_tick = TICK[name]
    recs = []
    for (ie, ix, d) in trades:
        R = atr[ie]
        if not (R > 0):
            continue
        entry = c[ie]; exitp = c[ix]
        pnl_R = d * (exitp - entry) / R
        seg_h = h[ie + 1:ix + 1].max() if ix > ie else h[ix]
        seg_l = l[ie + 1:ix + 1].min() if ix > ie else l[ix]
        if d > 0:
            mae_R = min(0.0, (seg_l - entry) / R); mfe_R = max(0.0, (seg_h - entry) / R)
        else:
            mae_R = min(0.0, (entry - seg_h) / R); mfe_R = max(0.0, (entry - seg_l) / R)
        cost_R = 2 * cost_tick / R + 0.05
        recs.append(dict(eday=int(days[ie]), xday=int(days[ix]), reason="swing", mkt=name,
                         pnl_R=pnl_R - cost_R, mae_R=mae_R, mfe_R=mfe_R))
    recs.sort(key=lambda r: (r["eday"], r["xday"]))
    return recs, len(np.unique(days))


def ibs_daily(df, name, lo=0.2, hi=0.8, sma_n=200, long_only_trend=True):
    sess, o, h, l, c, days, lb, atr, dow = _daily(df)
    ibs = np.where(h - l > 0, (c - l) / (h - l), 0.5)
    sma = pd.Series(c).rolling(sma_n, min_periods=20).mean().values
    trades = []; i = 1; n = len(c)
    while i < n:
        ok_trend = (not long_only_trend) or (not np.isnan(sma[i]) and c[i] > sma[i])
        if ibs[i] < lo and ok_trend:
            j = i + 1
            while j < n and not (ibs[j] > hi):
                j += 1
            if j >= n:
                break
            trades.append((i, j, +1)); i = j + 1
        else:
            i += 1
    return _records(df, name, trades, o, h, l, c, days, lb, atr)


def double_seven(df, name, sma_n=200):
    sess, o, h, l, c, days, lb, atr, dow = _daily(df)
    sma = pd.Series(c).rolling(sma_n, min_periods=20).mean().values
    low7 = pd.Series(c).rolling(7).min().values; high7 = pd.Series(c).rolling(7).max().values
    trades = []; i = 7; n = len(c)
    while i < n:
        if not np.isnan(sma[i]) and c[i] > sma[i] and c[i] <= low7[i] + 1e-9:
            j = i + 1
            while j < n and not (c[j] >= high7[j] - 1e-9):
                j += 1
            if j >= n:
                break
            trades.append((i, j, +1)); i = j + 1
        else:
            i += 1
    return _records(df, name, trades, o, h, l, c, days, lb, atr)


def three_down(df, name, k=3, hold=1, sma_n=0):
    sess, o, h, l, c, days, lb, atr, dow = _daily(df)
    sma = pd.Series(c).rolling(sma_n, min_periods=20).mean().values if sma_n else None
    trades = []; n = len(c)
    for i in range(k, n - hold):
        if all(c[i - j] < c[i - j - 1] for j in range(k)):
            if sma is not None and not (not np.isnan(sma[i]) and c[i] > sma[i]):
                continue
            trades.append((i, i + hold, +1))
    return _records(df, name, trades, o, h, l, c, days, lb, atr)


def sigma_daily(df, name, z=2.0, win=20, hold=1, sma_n=0):
    sess, o, h, l, c, days, lb, atr, dow = _daily(df)
    ret = np.concatenate([[0.0], np.diff(c) / c[:-1]])
    sig = pd.Series(ret).rolling(win, min_periods=10).std().shift(1).values
    zsp = ret / np.where(sig > 0, sig, np.nan)
    sma = pd.Series(c).rolling(sma_n, min_periods=20).mean().values if sma_n else None
    trades = []; n = len(c)
    for i in range(win, n - hold):
        if np.isnan(zsp[i]):
            continue
        if zsp[i] <= -z:
            if sma is not None and not (not np.isnan(sma[i]) and c[i] > sma[i]):
                continue
            trades.append((i, i + hold, +1))
    return _records(df, name, trades, o, h, l, c, days, lb, atr)


def turnaround_tuesday(df, name, thr=0.0, hold=1):
    sess, o, h, l, c, days, lb, atr, dow = _daily(df)
    trades = []; n = len(c)
    for i in range(1, n - hold):
        if dow[i] == 1 and c[i] < c[i - 1] * (1 - thr):   # Tuesday down vs Monday
            trades.append((i, i + hold, +1))
    return _records(df, name, trades, o, h, l, c, days, lb, atr)


SWING = {
    "ibs_daily": ibs_daily, "double_seven": double_seven, "three_down": three_down,
    "sigma_daily": sigma_daily, "turnaround_tue": turnaround_tuesday,
}


if __name__ == "__main__":
    from apex_lib import apex_eval
    from apex_walkforward import FOLDS, in_fold, out_fold, expR, best_pass
    print("=== DAILY swing MR strategies — per-block expR by fold (Y1/Y2/Y3) ===")
    print(f"{'block':<22}{'n':>5}{'Y1':>7}{'Y2':>7}{'Y3':>7}{'win%':>6}  signs")
    blocks = {}
    for m in ["es", "nq", "cl"]:
        df = load_fut(m)
        for nm, fn in SWING.items():
            recs, _ = fn(df, m)
            if len(recs) < 15:
                continue
            blocks[f"{nm}_{m}"] = recs
            evs = [expR(in_fold(recs, lo, hi)) for (lo, hi) in FOLDS]
            signs = "".join("+" if e > 0.03 else ("." if e > -0.03 else "-") for e in evs)
            w = float(np.mean([r["pnl_R"] > 0 for r in recs])) * 100
            print(f"{nm+'_'+m:<22}{len(recs):>5}{evs[0]:>+7.2f}{evs[1]:>+7.2f}{evs[2]:>+7.2f}{w:>6.0f}  {signs}")
    stable = [k for k, r in blocks.items() if all(expR(in_fold(r, lo, hi)) > 0.03 for (lo, hi) in FOLDS)]
    print(f"\n+EV in ALL 3 folds: {stable if stable else 'NONE'}")
    # portfolio of all index (es+nq) MR blocks, Apex eod + intraday
    port = []
    for k, r in blocks.items():
        port.extend(r)
    port.sort(key=lambda x: (x["eday"], x["xday"]))
    print(f"\nALL-swing portfolio n={len(port)} expR={expR(port):+.2f}")
    for trail in ["intraday", "eod"]:
        m = best_pass(port, trail)
        print(f"   {trail}: pass {m['pass_rate']*100:.0f}% fails={m['fails']} cens={m['censored']} R=${m['R']} medDays={m['med_days']}")
