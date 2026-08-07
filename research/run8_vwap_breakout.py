"""
run8_vwap_breakout.py — faithful backtest of the requested "Momentum Breakout with VWAP
Confirmation" strategy on 5m BTC/ETH/SOL, exactly as specified, with realistic costs.

ENTRY LONG (all): close > prior-20-bar high (breakout) · EMA9>EMA21 · RSI14 in [50,70]
                  · close > daily-VWAP · volume >= 3x its 20-bar average.  SHORT = mirror.
EXIT: stop 2.5% · TP1 = +1.5R on 50% · runner trails the 21-EMA (exit on close back through it).
Enter on the NEXT bar's open (no look-ahead). Costs charged per executed leg.
Reports the spec's metrics: win rate, avg trade, profit factor, # trades, maxDD, trades/day.
"""
import os
import numpy as np, pandas as pd

STOP_PCT = 0.025
TP1_R = 1.5
COINS = ["BTC", "ETH", "SOL"]


def load(c): return pd.read_csv(f"cache/{c}_5m.csv", index_col=0, parse_dates=True)


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + up/dn.replace(0, np.nan))


def indicators(df):
    df = df.copy()
    tp = (df.high + df.low + df.close) / 3
    day = df.index.normalize()
    cum_pv = (tp * df.volume).groupby(day).cumsum()
    cum_v = df.volume.groupby(day).cumsum()
    df["vwap"] = cum_pv / cum_v
    df["ema9"] = ema(df.close, 9)
    df["ema21"] = ema(df.close, 21)
    df["rsi"] = rsi(df.close, 14)
    df["res"] = df.high.rolling(20).max().shift(1)     # prior 20-bar high (exclude current)
    df["sup"] = df.low.rolling(20).min().shift(1)
    df["volma"] = df.volume.rolling(20).mean().shift(1)
    return df.dropna()


def signals(df):
    spike = df.volume >= 3 * df.volma
    longs = ((df.close > df.res) & (df.ema9 > df.ema21) &
             (df.rsi.between(50, 70)) & (df.close > df.vwap) & spike)
    shorts = ((df.close < df.sup) & (df.ema9 < df.ema21) &
              (df.rsi.between(30, 50)) & (df.close < df.vwap) & spike)
    return longs, shorts


def backtest(df, cost_bps):
    longs, shorts = signals(df)
    o, h, l, c = df.open.values, df.high.values, df.low.values, df.close.values
    ema21 = df.ema21.values
    L, S = longs.values, shorts.values
    n = len(df); i = 0; trades = []
    cost = cost_bps / 1e4
    while i < n - 1:
        side = 1 if L[i] else (-1 if S[i] else 0)
        if side == 0:
            i += 1; continue
        entry = o[i+1]                                   # next-bar open, no look-ahead
        stop = entry * (1 - STOP_PCT) if side == 1 else entry * (1 + STOP_PCT)
        tp1 = entry * (1 + side * TP1_R * STOP_PCT)
        filled_tp1 = False; ret = 0.0; rem = 1.0; j = i + 1
        while j < n:
            hi, lo = h[j], l[j]
            # stop first (conservative)
            hit_stop = (lo <= stop) if side == 1 else (hi >= stop)
            if hit_stop:
                ret += rem * side * (stop - entry) / entry
                rem = 0.0; break
            if not filled_tp1:
                hit_tp1 = (hi >= tp1) if side == 1 else (lo <= tp1)
                if hit_tp1:
                    ret += 0.5 * side * (tp1 - entry) / entry
                    rem = 0.5; filled_tp1 = True
            # runner trails the 21-EMA: exit on close back through it
            crossed = (c[j] < ema21[j]) if side == 1 else (c[j] > ema21[j])
            if filled_tp1 and crossed:
                ret += rem * side * (c[j] - entry) / entry
                rem = 0.0; break
            if (not filled_tp1) and crossed:             # bailed before TP1 (trail = exit signal)
                ret += rem * side * (c[j] - entry) / entry
                rem = 0.0; break
            j += 1
        if rem > 0:                                      # ran out of data
            ret += rem * side * (c[min(j, n-1)] - entry) / entry
        legs = 1.0 + (0.5 if filled_tp1 else 0.0) + (0.5 if filled_tp1 else 1.0)  # entry + exits
        ret -= legs * cost
        trades.append({"ret": ret, "bars_held": j - (i+1), "ts": df.index[i]})
        i = j + 1                                        # one position at a time
    return pd.DataFrame(trades)


def stats(tr, days, label):
    if len(tr) == 0:
        print(f"  {label:<14} 0 trades"); return
    r = tr.ret.values
    wins = r[r > 0]; losses = r[r <= 0]
    eq = (1 + pd.Series(r)).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.inf
    print(f"  {label:<14} {len(tr):>4} tr  {len(tr)/days*1:>4.2f}/day  "
          f"win {len(wins)/len(r)*100:>4.1f}%  avg {r.mean()*100:>+5.2f}%  "
          f"PF {pf:>4.2f}  tot {(eq.iloc[-1]-1)*100:>+6.1f}%  maxDD {dd*100:>5.1f}%")


for cost in (0, 5, 7.5):
    print(f"\n===== cost {cost} bps/leg =====")
    allret = []
    for c in COINS:
        if not os.path.exists(f"cache/{c}_5m.csv"):
            print(f"  {c}: 5m not downloaded"); continue
        df = indicators(load(c))
        days = (df.index[-1] - df.index[0]).days
        tr = backtest(df, cost)
        stats(tr, days, c)
        allret.append(tr)
    if allret:
        comb = pd.concat(allret).sort_values("ts")
        d = (comb.ts.iloc[-1] - comb.ts.iloc[0]).days
        stats(comb, d, "COMBINED")
