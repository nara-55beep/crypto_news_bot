"""
run6_daytrade.py — does ANY genuine DAY-TRADING strategy survive on BTC/ETH at
Lighter's ZERO-fee reality? Tests intraday mean-reversion (the natural high-frequency
edge), not breakout (already shown to die fast). Holds minutes-to-hours, many trades.

Strategies (single-asset, long-only AND long/short variants):
  * Bollinger fade: buy when close < lower band, exit at the mean (mean-reversion).
  * RSI(2) reversion (Connors): buy oversold, exit when it snaps back.
  * Short momentum: buy on a fast breakout (sanity baseline).
Costs tested at 0bps (Lighter fee-free) and 2bps (slippage). Metrics on daily-resampled
equity; reports trades/day + OOS(>2024-01) Sharpe. We try to BREAK each one.
"""
import os
import numpy as np, pandas as pd
import engine as E
pd.set_option("display.width", 220)

SPLIT = pd.Timestamp("2024-01-01")     # OOS = the most recent ~1.5y (toughest, post-bull)


def load(coin, tf):
    return pd.read_csv(f"cache/{coin}_{tf}.csv", index_col=0, parse_dates=True)


def rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100/(1+rs)


def backtest_pos(df, pos, cost_bps):
    """pos: target position series in {-1,0,1}. Returns (daily_ret, trades/day)."""
    bar_ret = df["close"].pct_change()
    pe = pos.shift(1).fillna(0)
    turn = (pe - pe.shift(1)).abs()
    r = (pe * bar_ret - turn * cost_bps/1e4).fillna(0)
    daily = (1 + r).resample("1D").prod() - 1
    entries = ((pos != 0) & (pos.shift(1) == 0)).sum()
    days = (df.index[-1] - df.index[0]).days or 1
    return daily, entries / days


def report(tag, df, pos, costs=(0, 2)):
    for cost in costs:
        daily, tpd = backtest_pos(df, pos, cost)
        mf = E.equity_metrics(daily); mo = E.equity_metrics(daily[daily.index >= SPLIT])
        print(f"  {tag:<34} {cost}bps  {tpd:>4.1f} trades/day  "
              f"Sharpe full {mf.get('Sharpe',0):>5}  OOS {mo.get('Sharpe',0):>5}  "
              f"CAGR {mf.get('CAGR',0)*100:>5.0f}%  maxDD {mf.get('maxDD',0)*100:>5.0f}%")


# --- strategy signal builders (return pos series in {-1,0,1}) ---
def bollinger_fade(df, n=20, k=2.0, long_short=False, trend_ma=0):
    px = df["close"]; ma = px.rolling(n).mean(); sd = px.rolling(n).std()
    lower, upper = ma - k*sd, ma + k*sd
    pos = pd.Series(0.0, index=px.index); state = 0
    pv, lv, uv, mv = px.values, lower.values, upper.values, ma.values
    tf = (px > px.rolling(trend_ma).mean()).values if trend_ma else np.ones(len(px), bool)
    for i in range(len(px)):
        if np.isnan(lv[i]):
            pos.iloc[i] = 0; continue
        if state == 0:
            if pv[i] <= lv[i] and tf[i]:
                state = 1
            elif long_short and pv[i] >= uv[i] and not tf[i]:
                state = -1
        elif state == 1 and pv[i] >= mv[i]:
            state = 0
        elif state == -1 and pv[i] <= mv[i]:
            state = 0
        pos.iloc[i] = state
    return pos


def rsi2(df, buy=10, sell=60, trend_ma=200, long_short=False):
    px = df["close"]; r = rsi(px, 2); ma = px.rolling(trend_ma).mean()
    pos = pd.Series(0.0, index=px.index); state = 0
    rv, pv, mv = r.values, px.values, ma.values
    for i in range(len(px)):
        if np.isnan(mv[i]) or np.isnan(rv[i]):
            pos.iloc[i] = 0; continue
        if state == 0:
            if rv[i] < buy and pv[i] > mv[i]:
                state = 1
            elif long_short and rv[i] > (100-buy) and pv[i] < mv[i]:
                state = -1
        elif state == 1 and rv[i] > sell:
            state = 0
        elif state == -1 and rv[i] < (100-sell):
            state = 0
        pos.iloc[i] = state
    return pos


for tf in ("1h", "15m"):
    if not os.path.exists(f"cache/BTC_{tf}.csv"):
        print(f"[{tf}] not downloaded yet, skipping"); continue
    print(f"\n================ {tf} bars ================")
    for coin in ("BTC", "ETH"):
        df = load(coin, tf)
        print(f"-- {coin} {tf} ({df.index[0].date()}→{df.index[-1].date()}, {len(df)} bars) --")
        report(f"{coin} Bollinger fade 20/2 LO", df, bollinger_fade(df, 20, 2.0, False, 200))
        report(f"{coin} Bollinger fade 20/2.5 LS", df, bollinger_fade(df, 20, 2.5, True))
        report(f"{coin} RSI2 <10 LO (200MA)", df, rsi2(df, 10, 60, 200, False))
        report(f"{coin} RSI2 LS", df, rsi2(df, 10, 60, 200, True))
