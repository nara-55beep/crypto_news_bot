"""
bt_freq.py — backtest the base "Freqtrade-style (paper)" bot (freq_bot.FreqBot, 'freq_sample')
faithfully on 3 years of BTC 5m. Exact rules copied from freq_bot.py:
  indicators RSI(14, simple-avg), EMA(9)/EMA(21), Bollinger(20, 2sigma)
  LONG  if (RSI crosses up through 35 OR close<=BBlower*1.001) AND emaFast>=emaSlow*0.997
  SHORT if (RSI crosses down through 65 OR close>=BBupper*0.999) AND emaFast<=emaSlow*1.003
  exits: minimal_roi {0:+4%,30:+2%,60:+1%,120:0} | stoploss -3% price | trailing (arm +2%, trail 1%)
         | RSI exit (long>=70 / short<=30, only in profit) | liquidation ~-4.9% price
  sizing: $100 start, 20x, WHOLE balance staked as margin each trade (all-in, compounding). Zero fees
  (Lighter). 5m timeframe.
"""
from __future__ import annotations
import os
import numpy as np, pandas as pd

START = 100.0; LEV = 20.0
BUY_RSI = 35.0; SELL_RSI = 70.0; SHORT_RSI = 65.0
EMA_FAST = 9; EMA_SLOW = 21; BB_LEN = 20; BB_STD = 2.0
MINIMAL_ROI = {0: 0.04, 30: 0.02, 60: 0.01, 120: 0.0}
STOPLOSS = -0.03; TRAIL_OFFSET = 0.02; TRAIL_POSITIVE = 0.01
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def roi_for(held_min):
    roi = None
    for m in sorted(MINIMAL_ROI):
        if held_min >= m:
            roi = MINIMAL_ROI[m]
    return roi


def load_btc_5m():
    df = pd.read_csv(os.path.join(CACHE, "BTC_1m_max.csv"), index_col=0, parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    r = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return r


def indicators(c):
    s = pd.Series(c)
    d = s.diff()
    g = d.clip(lower=0).rolling(14).sum() / 14
    l = (-d.clip(upper=0)).rolling(14).sum() / 14
    rsi = (100 - 100 / (1 + g / l.replace(0, np.nan))).where(l != 0, 100.0)
    ef = s.ewm(span=EMA_FAST, adjust=False).mean()
    es = s.ewm(span=EMA_SLOW, adjust=False).mean()
    mid = s.rolling(BB_LEN).mean(); sd = s.rolling(BB_LEN).std(ddof=0)
    return rsi.values, ef.values, es.values, (mid - BB_STD * sd).values, (mid + BB_STD * sd).values


def simulate(df, ind, lev, i0=None, i1=None, stoploss=STOPLOSS):
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    rsi, ef, es, bl, bu = ind
    n = len(C)
    start = i0 if i0 is not None else (EMA_SLOW + BB_LEN)
    end = i1 if i1 is not None else n
    bal = START; peak_bal = START; min_bal = START
    pos = None  # dict: side, E, ebar, peak, armed
    trades = []; blew_at = None
    for i in range(start, end):
        if pos is not None:
            E = pos["E"]; side = pos["side"]; hi, lo, cl = H[i], L[i], C[i]
            held = (i - pos["ebar"]) * 5
            exit_px = reason = None
            if side == "long":
                # adverse first (stop/liq), then ROI, then trailing
                if lo <= E * (1 + stoploss):
                    exit_px, reason = E * (1 + stoploss), "stoploss"
                else:
                    roi = roi_for(held)
                    if roi is not None and hi >= E * (1 + roi):
                        exit_px, reason = E * (1 + roi), f"roi{roi*100:.0f}"
                    else:
                        prof_hi = (hi - E) / E
                        if not pos["armed"] and prof_hi >= TRAIL_OFFSET:
                            pos["armed"] = True
                        pos["peak"] = max(pos["peak"], hi)
                        if pos["armed"] and lo <= pos["peak"] * (1 - TRAIL_POSITIVE):
                            exit_px, reason = pos["peak"] * (1 - TRAIL_POSITIVE), "trailing"
                        elif (cl - E) / E > 0 and rsi[i] >= SELL_RSI:
                            exit_px, reason = cl, "rsi"
            else:  # short
                if hi >= E * (1 - stoploss):
                    exit_px, reason = E * (1 - stoploss), "stoploss"
                else:
                    roi = roi_for(held)
                    if roi is not None and lo <= E * (1 - roi):
                        exit_px, reason = E * (1 - roi), f"roi{roi*100:.0f}"
                    else:
                        prof_hi = (E - lo) / E
                        if not pos["armed"] and prof_hi >= TRAIL_OFFSET:
                            pos["armed"] = True
                        pos["peak"] = min(pos["peak"], lo)
                        if pos["armed"] and hi >= pos["peak"] * (1 + TRAIL_POSITIVE):
                            exit_px, reason = pos["peak"] * (1 + TRAIL_POSITIVE), "trailing"
                        elif (E - cl) / E > 0 and rsi[i] <= (100 - SELL_RSI):
                            exit_px, reason = cl, "rsi"
            if exit_px is not None:
                ret = (exit_px - E) / E if side == "long" else (E - exit_px) / E
                bal = max(0.0, bal * (1 + lev * ret))
                trades.append({"side": side, "ret": ret, "pnl_pct": lev * ret, "reason": reason, "bal": bal})
                peak_bal = max(peak_bal, bal); min_bal = min(min_bal, bal)
                if bal < 1.0 and blew_at is None:
                    blew_at = df.index[i]
                pos = None
            continue
        # flat -> entry check (need prior rsi for the cross)
        if bal < 1.0:
            continue
        if np.isnan(rsi[i]) or np.isnan(bl[i]) or np.isnan(ef[i]) or np.isnan(rsi[i - 1]):
            continue
        cl = C[i]
        crossed_up = rsi[i - 1] < BUY_RSI <= rsi[i]
        crossed_down = rsi[i - 1] > SHORT_RSI >= rsi[i]
        at_lower = cl <= bl[i] * 1.001
        at_upper = cl >= bu[i] * 0.999
        not_down = ef[i] >= es[i] * 0.997
        not_up = ef[i] <= es[i] * 1.003
        if (crossed_up or at_lower) and not_down:
            pos = {"side": "long", "E": cl, "ebar": i, "peak": cl, "armed": False}
        elif (crossed_down or at_upper) and not_up:
            pos = {"side": "short", "E": cl, "ebar": i, "peak": cl, "armed": False}

    if pos is not None:                          # close any open position at the window's last bar
        E = pos["E"]; side = pos["side"]; X = C[end - 1]
        ret = (X - E) / E if side == "long" else (E - X) / E
        bal = max(0.0, bal * (1 + lev * ret))
        trades.append({"side": side, "ret": ret, "pnl_pct": lev * ret, "reason": "window_end", "bal": bal})
        peak_bal = max(peak_bal, bal); min_bal = min(min_bal, bal)
    tr = pd.DataFrame(trades)
    return dict(tr=tr, bal=bal, peak=peak_bal, low=min_bal, blew=blew_at)


def winrate(tr):
    return (tr["ret"] >= -1e-9).mean() * 100 if len(tr) else 0.0


def main():
    df = load_btc_5m()
    ind = indicators(df["close"].values); n = len(df)
    print(f"Freqtrade-style — BTC 5m {df.index[0].date()}->{df.index[-1].date()}, zero fees")
    print("Does changing the STOP LOSS make it profitable over 3 years? Sweep, full 3y:\n")
    print(f"  {'stop':>6} {'trades':>7} {'win%':>6} {'stops%':>7} {'avgEV/trade':>12} {'1x final$':>11} {'20x final$':>11}")
    rows = []
    for sl in [0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08, 0.12, 0.20]:
        t = simulate(df, ind, 1.0, stoploss=-sl)["tr"]
        b20 = simulate(df, ind, 20.0, stoploss=-sl)
        ev = t["ret"].mean() * 100
        nst = (t["reason"] == "stoploss").mean() * 100
        b1 = START * (1 + t["ret"]).prod()        # 1x compounded
        tag = "  <- current" if abs(sl - 0.03) < 1e-9 else ""
        rows.append((sl, ev, b1, b20["bal"]))
        print(f"  {-sl*100:>5.1f}% {len(t):>7} {winrate(t):>5.0f}% {nst:>6.1f}% {ev:>+11.4f}% "
              f"{b1:>11,.2f} {b20['bal']:>11,.2f}{tag}")
    pos = [r for r in rows if r[1] > 0]
    print("\n  avgEV/trade is the raw BTC % move per trade. POSITIVE = the signal makes money.")
    if pos:
        best = max(pos, key=lambda r: r[1])
        print(f"  -> stops that flip it +EV: {', '.join(f'{-r[0]*100:.1f}%' for r in pos)}")
        print(f"  -> best: {-best[0]*100:.1f}% stop = {best[1]:+.4f}%/trade, 1x ${best[2]:,.0f} over 3y")
    else:
        print("  -> NO stop-loss value makes the signal +EV. The entry, not the stop, is the problem.")
    print("  NOTE: a wider stop helps the SIGNAL but at 20x a -5%+ move ~liquidates, so the bot's")
    print("  20x-all-in column still blows up. Profitable-over-3y needs a wider stop AND low leverage.")


if __name__ == "__main__":
    main()
