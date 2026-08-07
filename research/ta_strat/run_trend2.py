"""
run_trend2.py — robustness sweep of the CANONICAL, hard-to-overfit trend signals on
daily BTC: time-series momentum (sign of past-N-day return) and MA-position. These are
1-2 parameter signals, the gold standard for "is there a real trend edge".

Continuous-position sim (always long or short or flat), leverage applied to daily
returns, cost on turnover, liquidation if a daily move exceeds the margin. We look for
signals positive over the FULL 3.5y AND the last 1y AND most individual years — that's
robustness, not curve-fitting.
"""
from __future__ import annotations
import sys, os, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind

DAILY = "../../crypto-trading-bot/data_cache/binanceusdm_BTCUSDT_1d_1300d.csv"


def load_daily():
    df = pd.read_csv(DAILY)
    df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    return df[~df.index.duplicated(keep="last")].sort_index()


def sim(close, pos, lev=2.0, slip_bps=3.0, start=100.0):
    """Continuous position in {-1,0,1}. pos decided on close, earns next day's return."""
    ret = close.pct_change().fillna(0.0)
    pe = pos.shift(1).fillna(0.0)
    turn = (pe - pe.shift(1)).abs().fillna(0.0)
    # per-day levered return with cost; floor daily at -1 (liquidation/wipeout)
    daily = lev * (pe * ret) - turn * lev * slip_bps / 1e4
    daily = daily.clip(lower=-1.0)
    eq = start * (1 + daily).cumprod()
    return eq, daily, pe


def metrics(close, pos, eq, daily, pe):
    peak = eq.cummax(); dd = (eq / peak - 1)
    maxdd = dd.min()
    roi = eq.iloc[-1] / eq.iloc[0] - 1
    dr = daily[daily != 0]
    sharpe = (daily.mean() / daily.std() * np.sqrt(365)) if daily.std() > 0 else 0
    # trades = sign changes in held position
    flips = (pe != pe.shift(1)) & (pe != 0)
    ntr = int(flips.sum())
    # win rate per contiguous holding run
    runs = []
    cur = 0.0; held = 0
    sign = pe.values; dvals = daily.values
    prev = 0.0
    for i in range(len(pe)):
        s = sign[i]
        if s != prev and prev != 0:
            runs.append(cur); cur = 0.0
        if s != 0:
            cur += dvals[i]
        prev = s
    if prev != 0:
        runs.append(cur)
    runs = np.array(runs)
    wr = (runs > 0).mean() if len(runs) else 0
    return dict(roi=roi, maxdd=maxdd, sharpe=sharpe, n=ntr, win=wr, final=eq.iloc[-1])


def yearly(close, pos, lev, slip_bps=3.0):
    out = {}
    for y in sorted(set(close.index.year)):
        m = close.index.year == y
        if m.sum() < 30:
            continue
        c = close[m]
        p = pos[m]
        eq, daily, pe = sim(c, p, lev, slip_bps)
        out[y] = eq.iloc[-1] / eq.iloc[0] - 1
    return out


def main():
    df = load_daily()
    c = df["close"]
    last_yr = df.index[-1] - pd.Timedelta(days=365)

    signals = {}
    # time-series momentum: long if up over N days, short if down
    for N in (30, 60, 90, 120):
        s = np.sign(c - c.shift(N)).fillna(0)
        signals[f"TSMOM {N}d L/S"] = s
    # MA position
    for n in (50, 100, 150, 200):
        ma = c.rolling(n).mean()
        s = pd.Series(np.where(c > ma, 1.0, np.where(c < ma, -1.0, 0.0)), index=c.index)
        s[ma.isna()] = 0
        signals[f"MA{n} pos L/S"] = s
    # dual MA (fast vs slow) — classic trend
    for f_, s_ in [(20, 100), (50, 200)]:
        mf = c.rolling(f_).mean(); ms = c.rolling(s_).mean()
        s = pd.Series(np.where(mf > ms, 1.0, -1.0), index=c.index)
        s[ms.isna()] = 0
        signals[f"MA{f_}/{s_} cross L/S"] = s
    # long-only variants of the best shapes (no shorting)
    for n in (100, 200):
        ma = c.rolling(n).mean()
        s = pd.Series(np.where(c > ma, 1.0, 0.0), index=c.index)
        s[ma.isna()] = 0
        signals[f"MA{n} LONG-only"] = s

    LEV = 3.0
    print(f"Daily BTC {df.index[0].date()} -> {df.index[-1].date()}  |  leverage {LEV}x, "
          f"3bps slip, continuous position\n")
    print(f"{'signal':<22}| {'FULL 3.5y':>26} || {'LAST 1y':>22} || per-year ROI%")
    print(f"{'':<22}| {'ROI%':>7}{'maxDD%':>8}{'Shrp':>6}{'n':>5} || {'ROI%':>7}{'win%':>6}{'DD%':>7} ||")
    print("-" * 130)
    results = []
    for name, sig in signals.items():
        eq, daily, pe = sim(c, sig, LEV)
        m = metrics(c, sig, eq, daily, pe)
        # last year
        mly = c.index >= last_yr
        eqy, daiy, pey = sim(c[mly], sig[mly], LEV)
        my = metrics(c[mly], sig[mly], eqy, daiy, pey)
        yr = yearly(c, sig, LEV)
        yrstr = " ".join(f"{y}:{v*100:>4.0f}" for y, v in yr.items())
        results.append((name, m, my, yr))
        print(f"{name:<22}| {m['roi']*100:>7.0f}{m['maxdd']*100:>8.0f}{m['sharpe']:>6.2f}{m['n']:>5} "
              f"|| {my['roi']*100:>7.0f}{my['win']*100:>6.0f}{my['maxdd']*100:>7.0f} || {yrstr}")

    # robust = positive full AND positive last-year AND positive in >=60% of years
    print(f"\n{'='*70}\nROBUST (full>0 AND last-1y>0 AND >=60% of years positive):")
    any_robust = False
    for name, m, my, yr in results:
        pos_years = sum(1 for v in yr.values() if v > 0)
        frac = pos_years / len(yr)
        if m['roi'] > 0 and my['roi'] > 0 and frac >= 0.6:
            any_robust = True
            print(f"  {name:<22} FULL {m['roi']*100:>5.0f}%  LAST1y {my['roi']*100:>5.0f}%  "
                  f"years+ {pos_years}/{len(yr)}  Sharpe {m['sharpe']:.2f}")
    if not any_robust:
        print("  NONE fully robust. Best last-1y performers:")
        for name, m, my, yr in sorted(results, key=lambda x: -x[2]['roi'])[:4]:
            print(f"  {name:<22} LAST1y {my['roi']*100:>5.0f}%  FULL {m['roi']*100:>5.0f}%  "
                  f"win {my['win']*100:.0f}%  DD {my['maxdd']*100:.0f}%")


if __name__ == "__main__":
    main()
