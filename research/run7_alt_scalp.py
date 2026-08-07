"""
run7_alt_scalp.py — confirm the 'cost cliff' for ALTS/MEMES on fast (5m) bars, the
exact regime the day-trading brief targets. Same intraday mean-reversion, but alts
have WIDER spreads, so we stress 0 / 5 / 10 bps. If even the 0-bps winners die at a
realistic alt spread, taker scalping these is not viable — the point of the test.
"""
import os
import numpy as np, pandas as pd
import engine as E
pd.set_option("display.width", 220)
SPLIT = pd.Timestamp("2025-06-01")     # OOS = last ~1y


def load(coin, tf): return pd.read_csv(f"cache/{coin}_{tf}.csv", index_col=0, parse_dates=True)


def rsi(close, n):
    d = close.diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1+up/dn.replace(0, np.nan))


def rsi2(df, buy=10, sell=60, trend_ma=200, long_short=False):
    px = df["close"]; r = rsi(px, 2); ma = px.rolling(trend_ma).mean()
    pos = np.zeros(len(px)); state = 0; rv, pv, mv = r.values, px.values, ma.values
    for i in range(len(px)):
        if np.isnan(mv[i]) or np.isnan(rv[i]): pos[i] = 0; continue
        if state == 0:
            if rv[i] < buy and pv[i] > mv[i]: state = 1
            elif long_short and rv[i] > 100-buy and pv[i] < mv[i]: state = -1
        elif state == 1 and rv[i] > sell: state = 0
        elif state == -1 and rv[i] < 100-sell: state = 0
        pos[i] = state
    return pd.Series(pos, index=px.index)


def bollinger_fade(df, n=20, k=2.0):
    px = df["close"]; ma = px.rolling(n).mean(); sd = px.rolling(n).std()
    lo = ma - k*sd; pos = np.zeros(len(px)); state = 0
    pv, lv, mv = px.values, lo.values, ma.values
    for i in range(len(px)):
        if np.isnan(lv[i]): pos[i] = 0; continue
        if state == 0 and pv[i] <= lv[i]: state = 1
        elif state == 1 and pv[i] >= mv[i]: state = 0
        pos[i] = state
    return pd.Series(pos, index=px.index)


def evalp(df, pos, cost):
    bar = df["close"].pct_change(); pe = pos.shift(1).fillna(0)
    turn = (pe - pe.shift(1)).abs()
    r = (pe*bar - turn*cost/1e4).fillna(0)
    daily = (1+r).resample("1D").prod()-1
    tpd = ((pos != 0) & (pos.shift(1) == 0)).sum() / ((df.index[-1]-df.index[0]).days or 1)
    return daily, tpd


def report(tag, df, pos):
    for cost in (0, 5, 10):
        daily, tpd = evalp(df, pos, cost)
        mo = E.equity_metrics(daily[daily.index >= SPLIT])
        mf = E.equity_metrics(daily)
        print(f"  {tag:<26} {cost:>2}bps  {tpd:>4.1f} tr/day  full {mf.get('Sharpe',0):>5}  "
              f"OOS {mo.get('Sharpe',0):>5}  CAGR {mf.get('CAGR',0)*100:>5.0f}%")


for coin in ("SOL", "DOGE"):
    if not os.path.exists(f"cache/{coin}_5m.csv"):
        print(f"{coin} 5m not ready"); continue
    df = load(coin, "5m")
    print(f"\n-- {coin} 5m ({df.index[0].date()}→{df.index[-1].date()}, {len(df)} bars) --")
    report(f"{coin} RSI2 <10 LO", df, rsi2(df, 10, 60, 200, False))
    report(f"{coin} RSI2 LS", df, rsi2(df, 10, 60, 200, True))
    report(f"{coin} Bollinger fade 20/2", df, bollinger_fade(df, 20, 2.0))
