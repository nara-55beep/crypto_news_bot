"""
run5_intraday.py — does the Donchian breakout edge survive at HIGHER FREQUENCY
(more trades)? Test 4h and 1h bars for BTC/ETH with the same long-only breakout +
200-period MA regime filter, scanning short bar-lookbacks (= more trades).

Metrics are computed on DAILY-resampled equity so Sharpe stays comparable to the
daily study. Costs in bps per unit turnover (Lighter perp ≈ low; we stress 5-20bps).
"""
import os, time
import data, engine as E
import numpy as np, pandas as pd
import ccxt
pd.set_option("display.width", 220)

CACHE = data.CACHE
SPLIT = pd.Timestamp("2022-06-01")


def load_tf(coin, tf, since_iso):
    path = os.path.join(CACHE, f"{coin}_{tf}.csv")
    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    ex = data._ex()
    print(f"  downloading {coin} {tf} ...")
    df = data.fetch_ohlcv(ex, f"{coin}/USDT", tf, since_iso)
    df.to_csv(path)
    return df


def donchian_intraday(df, entry, exit, ma=200, allow_short=False):
    px = df["close"]
    hi = px.rolling(entry).max(); lo = px.rolling(exit).min()
    maf = px.rolling(ma).mean()
    sig = np.zeros(len(px)); state = 0
    pv, hv, lv, mv = px.values, hi.values, lo.values, maf.values
    for i in range(len(px)):
        if np.isnan(hv[i]) or np.isnan(mv[i]):
            sig[i] = 0; continue
        if state <= 0 and pv[i] >= hv[i] and pv[i] > mv[i]:
            state = 1
        elif state >= 0 and pv[i] <= lv[i]:
            state = 0
        sig[i] = state
    return pd.Series(sig, index=px.index)


def evaluate(df, entry, exit, cost_bps=10.0):
    pos = donchian_intraday(df, entry, exit)
    bar_ret = df["close"].pct_change()
    pos_eff = pos.shift(1).fillna(0)
    turnover = (pos_eff - pos_eff.shift(1)).abs()
    r = (pos_eff * bar_ret - turnover * cost_bps / 1e4).fillna(0)
    # daily-resampled returns for comparable annualized metrics
    daily = (1 + r).resample("1D").prod() - 1
    entries = int(((pos == 1) & (pos.shift(1) != 1)).sum())
    yrs = len(df) / (len(df) / ((df.index[-1] - df.index[0]).days / 365.25))
    trades_yr = entries / max(yrs, 0.1)
    return daily, trades_yr


print("loading intraday data (cached after first run)...")
data4 = {c: load_tf(c, "4h", "2019-01-01T00:00:00Z") for c in ("BTC", "ETH")}
data1 = {c: load_tf(c, "1h", "2021-01-01T00:00:00Z") for c in ("BTC", "ETH")}
print(f"  4h BTC bars {len(data4['BTC'])}, 1h BTC bars {len(data1['BTC'])}\n")


def scan(label, dfs, grid, cost):
    print(f"=== {label} (cost {cost}bps) ===")
    print(f"  {'entry/exit':<12}{'trades/yr':>10}{'Sharpe full':>13}{'Sharpe OOS':>12}{'CAGR':>8}{'maxDD':>8}")
    for entry, exit in grid:
        # average BTC+ETH equally
        dailies = []
        tyr = []
        for c, df in dfs.items():
            d, ty = evaluate(df, entry, exit, cost)
            dailies.append(d); tyr.append(ty)
        idx = dailies[0].index.union(dailies[1].index)
        book = sum(d.reindex(idx).fillna(0) for d in dailies) / len(dailies)
        mf = E.equity_metrics(book)
        mo = E.equity_metrics(book[book.index >= SPLIT])
        print(f"  {str(entry)+'/'+str(exit):<12}{np.mean(tyr):>10.0f}"
              f"{mf.get('Sharpe',0):>13}{mo.get('Sharpe',0):>12}"
              f"{mf.get('CAGR',0)*100:>7.0f}%{mf.get('maxDD',0)*100:>7.0f}%")


# 4h: from long-horizon (few trades) to short-horizon (many trades)
scan("4h breakout BTC+ETH", data4,
     [(120, 40), (80, 30), (50, 20), (30, 12), (20, 8), (12, 6), (8, 4)], cost=10)
print()
scan("1h breakout BTC+ETH", data1,
     [(168, 48), (96, 24), (48, 12), (24, 8), (12, 6), (8, 4), (6, 3)], cost=10)
print()
scan("1h breakout BTC+ETH — LOW cost (Lighter)", data1,
     [(48, 12), (24, 8), (12, 6), (8, 4)], cost=3)
