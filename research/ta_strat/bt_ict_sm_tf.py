"""
bt_ict_sm_tf.py — backtest the ICT SM Trades strategy (the SAME grab->MSS->FVG logic that the
/ict chart draws) on BTC over 3 years, across 1m / 3m / 5m / 15m. $50,000 start, NO leverage
(spot, full equity per trade at 1x), entry at the FVG, stop beyond the liquidity-grab candle,
target 2R (trend continuation). Reports per timeframe.
"""
from __future__ import annotations
import os, sys, time
import numpy as np, pandas as pd

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
START = 50_000.0
LEFT = RIGHT = 5
MSS_WINDOW = 20
ENTRY_EXPIRY = 16
MAX_HOLD = 240
TARGET_R = 2.0
FVG_MIN_FRAC = 0.00035
STOP_BUF_FRAC = 0.0002        # tiny buffer beyond the grab extreme


def load_1m():
    df = pd.read_csv(os.path.join(CACHE, "BTC_1m_max.csv"), index_col=0, parse_dates=True)
    return df[~df.index.duplicated(keep="last")].sort_index()


def resample(df, rule):
    if rule == "1min":
        return df
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def pivots(H, L, left, right):
    n = len(H); ph = np.zeros(n, bool); pl = np.zeros(n, bool)
    for i in range(left, n - right):
        if H[i] == H[i - left:i + right + 1].max() and H[i] > H[i - 1] and H[i] >= H[i + 1]:
            ph[i] = True
        if L[i] == L[i - left:i + right + 1].min() and L[i] < L[i - 1] and L[i] <= L[i + 1]:
            pl[i] = True
    return ph, pl


def find_fvg(H, L, i, k, direction):
    for a in range(i, k):
        if a + 1 >= len(H) or a - 1 < 0:
            continue
        if direction == "bull" and L[a + 1] - H[a - 1] >= FVG_MIN_FRAC * L[a + 1]:
            return (H[a - 1] + L[a + 1]) / 2.0
        if direction == "bear" and L[a - 1] - H[a + 1] >= FVG_MIN_FRAC * H[a + 1]:
            return (L[a - 1] + H[a + 1]) / 2.0
    return None


def run_tf(df, cost_bps):
    O, H, L, C = (df["open"].values, df["high"].values, df["low"].values, df["close"].values)
    n = len(C)
    if n < 50:
        return None
    ph, pl = pivots(H, L, LEFT, RIGHT)
    # last CONFIRMED pivot index as of each bar (confirmed when RIGHT bars have passed)
    last_ph = np.full(n, -1); last_pl = np.full(n, -1); lph = lpl = -1
    for i in range(n):
        j = i - RIGHT
        if j >= 0:
            if ph[j]: lph = j
            if pl[j]: lpl = j
        last_ph[i] = lph; last_pl[i] = lpl

    eq = START; trades = []; eqc = [START]
    i = RIGHT + 1
    while i < n - 2:
        lphj, lplj = last_ph[i], last_pl[i]
        setup = None
        # bullish: sweep last pivot low (wick+close back), then MSS up through last pivot high
        if lplj >= 0 and L[i] < L[lplj] and C[i] > L[lplj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lphj >= 0 and C[k] > H[lphj]:
                    fvg = find_fvg(H, L, i, k, "bull")
                    if fvg:
                        stop = L[i] * (1 - STOP_BUF_FRAC); R = fvg - stop
                        if R > 0:
                            setup = ("bull", fvg, stop, fvg + TARGET_R * R, k)
                    break
        if setup is None and lphj >= 0 and H[i] > H[lphj] and C[i] < H[lphj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lplj >= 0 and C[k] < L[lplj]:
                    fvg = find_fvg(H, L, i, k, "bear")
                    if fvg:
                        stop = H[i] * (1 + STOP_BUF_FRAC); R = stop - fvg
                        if R > 0:
                            setup = ("bear", fvg, stop, fvg - TARGET_R * R, k)
                    break
        if setup:
            d, entry, stop, tgt, k = setup
            # wait for limit fill at the FVG (invalidate if stop hit first)
            fi = None
            for f in range(k + 1, min(n, k + 1 + ENTRY_EXPIRY)):
                if (d == "bull" and L[f] <= stop) or (d == "bear" and H[f] >= stop):
                    break
                if (d == "bull" and L[f] <= entry) or (d == "bear" and H[f] >= entry):
                    fi = f; break
            if fi is not None:
                exit_px = reason = None
                end = min(n, fi + MAX_HOLD)
                for m in range(fi, end):
                    if d == "bull":
                        if L[m] <= stop: exit_px, reason = stop, "stop"; break
                        if H[m] >= tgt: exit_px, reason = tgt, "target"; break
                    else:
                        if H[m] >= stop: exit_px, reason = stop, "stop"; break
                        if L[m] <= tgt: exit_px, reason = tgt, "target"; break
                if exit_px is None:
                    m = end - 1; exit_px, reason = C[m], "time"
                ret = (1 if d == "bull" else -1) * (exit_px / entry - 1.0)
                pnl = eq * (ret - 2 * cost_bps / 1e4)        # NO leverage: full equity at 1x, round-trip cost
                eq += pnl
                trades.append({"dir": d, "ret": ret, "pnl": pnl, "reason": reason})
                eqc.append(eq)
                i = m + 1
                continue
        i += 1
    return trades, eq, np.array(eqc)


def metrics(trades, eqc, final):
    if not trades:
        return None
    pnl = np.array([t["pnl"] for t in trades])
    wins = (pnl > 0)
    peak = np.maximum.accumulate(eqc); dd = ((eqc - peak) / peak).min()
    pf = pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum()) if (pnl < 0).any() else float("inf")
    from collections import Counter
    return dict(n=len(trades), win=wins.mean(), roi=final / START - 1, final=final,
                profit=final - START, dd=dd, pf=pf,
                reasons=dict(Counter(t["reason"] for t in trades)))


def main():
    df1 = load_1m()
    print(f"[data] BTC 1m: {len(df1):,} bars  {df1.index[0].date()} -> {df1.index[-1].date()} "
          f"({(df1.index[-1]-df1.index[0]).days}d)\n", flush=True)
    TFS = [("1m", "1min"), ("3m", "3min"), ("5m", "5min"), ("15m", "15min")]
    print(f"ICT SM Trades on BTC — $50,000, NO leverage (spot 1x), 3 years, entry FVG / stop beyond grab / 2R target")
    print("=" * 104)
    print(f"{'TF':<5}{'trades':>8}{'win%':>7}{'  ROI% (0bps)':>14}{'  ROI% (2bps)':>14}"
          f"{'maxDD%':>9}{'PF':>7}{'final$ (2bps)':>15}")
    print("-" * 104)
    for label, rule in TFS:
        d = resample(df1, rule)
        t0 = time.time()
        tr0, eq0, eqc0 = run_tf(d, 0.0)
        trc, eqc_, eqcc = run_tf(d, 2.0)
        m0 = metrics(tr0, eqc0, eq0); mc = metrics(trc, eqcc, eqc_)
        if not mc:
            print(f"{label:<5} no trades"); continue
        print(f"{label:<5}{mc['n']:>8}{mc['win']*100:>6.0f}{m0['roi']*100:>13.0f}{mc['roi']*100:>14.0f}"
              f"{mc['dd']*100:>9.0f}{mc['pf']:>7.2f}{mc['final']:>15,.0f}   ({time.time()-t0:.0f}s)")
    print("-" * 104)
    print("ROI(0bps)=gross, ROI(2bps)=net of ~2bps/side round-trip cost. No leverage = full equity per trade at 1x.")


if __name__ == "__main__":
    main()
