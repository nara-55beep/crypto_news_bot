"""
bt_rsi2.py — backtest "RSI2 EMA50 Scalper (paper - ATR filter)" (rsi2_scalper_paper.py) on 3y BTC 15m.
Exact rules: $100, 10x all-in (notional=equity x10), 1.5bps/side. EMA50, RSI(2) Wilder, ATR(14) Wilder.
ENTRY only if ATR% in its trailing-30d [15th,85th] pctile band, AND (long: close>EMA50 & RSI2<8) /
(short: close<EMA50 & RSI2>92). EXIT: SL -1% / TP +1.5% / RSI(long>70,short<30) / TIME 72 bars.
-5% daily equity stop -> pause 12h. Reports 10x faithful + 1x + ATR-off for context.
"""
from __future__ import annotations
import os
import numpy as np, pandas as pd

START = 100.0
RSI_ENTRY = 8.0; RSI_EXIT_LONG = 70.0; RSI_EXIT_SHORT = 30.0
EMA_LEN = 50; STOP_PCT = 0.01; TAKE_PCT = 0.015
COST_BPS = 1.5; MAX_HOLD = 72; DAILY_STOP = 0.05; COOLDOWN_BARS = 48
ATR_LB = 96 * 30; ATR_MINP = 96 * 10
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def load_btc_15m():
    df = pd.read_csv(os.path.join(CACHE, "BTC_1m_max.csv"), index_col=0, parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.resample("15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def indicators(df):
    c = df["close"]
    ema = c.ewm(span=EMA_LEN, adjust=False).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)
    h, l = df["high"], df["low"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    atrp = atr / c
    q15 = atrp.rolling(ATR_LB, min_periods=ATR_MINP).quantile(0.15)
    q85 = atrp.rolling(ATR_LB, min_periods=ATR_MINP).quantile(0.85)
    return ema.values, rsi2.values, atrp.values, q15.values, q85.values


def simulate(df, ind, lev, atr_filter=True):
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    ema, rsi2, atrp, q15, q85 = ind
    days = df.index.tz_localize(None).normalize().values if df.index.tz is not None else df.index.normalize().values
    n = len(C); fee = lev * COST_BPS / 1e4 * 2          # round-turn fee as fraction of equity
    eq = START; peak = START; trades = []; pos = None
    cur_day = None; day_start = eq; paused_until = -1; blew = None
    for i in range(EMA_LEN + 5, n):
        if days[i] != cur_day:
            cur_day = days[i]; day_start = eq
        if pos is not None:
            E = pos["E"]; side = pos["side"]; hi, lo, cl = H[i], L[i], C[i]
            xpx = rsn = None
            if side == "long":
                if lo <= E * (1 - STOP_PCT): xpx, rsn = E * (1 - STOP_PCT), "SL"
                elif hi >= E * (1 + TAKE_PCT): xpx, rsn = E * (1 + TAKE_PCT), "TP"
                elif rsi2[i] > RSI_EXIT_LONG: xpx, rsn = cl, "RSI"
            else:
                if hi >= E * (1 + STOP_PCT): xpx, rsn = E * (1 + STOP_PCT), "SL"
                elif lo <= E * (1 - TAKE_PCT): xpx, rsn = E * (1 - TAKE_PCT), "TP"
                elif rsi2[i] < RSI_EXIT_SHORT: xpx, rsn = cl, "RSI"
            if rsn is None and (i - pos["ebar"]) >= MAX_HOLD: xpx, rsn = cl, "TIME"
            if rsn:
                ret = (xpx - E) / E if side == "long" else (E - xpx) / E
                eq = max(0.0, eq * (1 + lev * ret) - eq * fee)
                trades.append({"side": side, "ret": ret, "reason": rsn, "eq": eq})
                peak = max(peak, eq)
                if eq < 1.0 and blew is None: blew = df.index[i]
                pos = None
                if eq <= day_start * (1 - DAILY_STOP):           # daily stop -> pause 12h
                    paused_until = i + COOLDOWN_BARS
            continue
        if eq < 1.0 or i < paused_until:
            continue
        if atr_filter and not (q15[i] == q15[i] and q15[i] <= atrp[i] <= q85[i]):
            continue
        cl = C[i]
        if cl > ema[i] and rsi2[i] < RSI_ENTRY:
            pos = {"side": "long", "E": cl, "ebar": i}
        elif cl < ema[i] and rsi2[i] > 100 - RSI_ENTRY:
            pos = {"side": "short", "E": cl, "ebar": i}
    tr = pd.DataFrame(trades)
    dd = 0.0
    if len(tr):
        e = np.concatenate([[START], tr["eq"].values]); pk = np.maximum.accumulate(e)
        dd = ((e - pk) / pk).min() * 100
    return dict(tr=tr, eq=eq, peak=peak, dd=dd, blew=blew)


def report(name, r):
    tr = r["tr"]
    if not len(tr):
        print(f"{name:<28} no trades"); return
    win = (tr["ret"] > 0).mean() * 100
    print(f"{name:<28} {len(tr):>5} trades  win {win:>4.1f}%  final ${r['eq']:>11,.2f}  "
          f"peak ${r['peak']:>10,.0f}  maxDD {r['dd']:>5.0f}%  "
          f"{'BLEW '+str(r['blew'].date()) if r['blew'] is not None else ''}")
    rc = {k: int(v) for k, v in tr["reason"].value_counts().items()}
    print(f"{'':<28} exits {rc}  avg ret/trade {tr['ret'].mean()*100:+.4f}% price")


def main():
    df = load_btc_15m(); ind = indicators(df)
    print(f"RSI2 EMA50 Scalper (ATR filter) — BTC 15m {df.index[0].date()}->{df.index[-1].date()} "
          f"({len(df):,} 15m bars)\n")
    report("FAITHFUL: 10x, ATR filter", simulate(df, ind, 10.0, True))
    report("1x, ATR filter", simulate(df, ind, 1.0, True))
    report("10x, NO ATR filter", simulate(df, ind, 10.0, False))
    report("1x, NO ATR filter", simulate(df, ind, 1.0, False))


if __name__ == "__main__":
    main()
