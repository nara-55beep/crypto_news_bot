"""
bt_ict_2022.py — the FULL ICT 2022 model on BTC, with the rules a serious ICT 2022 trader uses
that the naive test lacked:
  * KILLZONE-ONLY entries  (London 02:00-05:00 ET, NY 07:00-11:00 ET) — time is the filter
  * DAILY HTF BIAS         (only longs when daily trend up / shorts when down)
  * 1:3 R:R target         (vs the 2R used before)
Same grab -> MSS -> FVG entry, stop beyond the grab. $50,000, no leverage (1x notional).
Tested 1m/3m/5m/15m over 3 years, gross (0bps) and net (~2bps/side). Also prints the BASE
(24/7, 2R, no bias) for comparison so the effect of the ICT-2022 rules is visible.
"""
from __future__ import annotations
import os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_ict_sm_tf import resample, pivots, find_fvg, START

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
NY = "America/New_York"
MSS_WINDOW, ENTRY_EXPIRY, MAX_HOLD = 20, 16, 240
LEFT = RIGHT = 5
STOP_BUF_FRAC = 0.0002


def in_killzone_et(hr):
    return (2 <= hr < 5) or (7 <= hr < 11)        # London open + NY open (incl silver bullet)


def run(df, cost_bps, target_r, killzone, bias_on):
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(C)
    if n < 50:
        return None
    et_hr = df.index.tz_convert(NY).hour.values if killzone else np.zeros(n, int)
    # daily bias from prior-day close vs daily EMA20 (no look-ahead)
    if bias_on:
        dc = df["close"].resample("1D").last().dropna()
        ema = dc.ewm(span=20, adjust=False).mean()
        bser = pd.Series(np.where(dc > ema, 1, -1), index=dc.index).shift(1)
        bmap = {d.date(): int(v) for d, v in bser.dropna().items()}
        day_arr = df.index.tz_convert(NY).date
        bias = np.array([bmap.get(d, 0) for d in day_arr])
    else:
        bias = np.zeros(n, int)
    ph, pl = pivots(H, L, LEFT, RIGHT)
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
        if killzone and not in_killzone_et(et_hr[i]):
            i += 1; continue
        lphj, lplj = last_ph[i], last_pl[i]
        setup = None
        allow_long = (not bias_on) or bias[i] >= 0
        allow_short = (not bias_on) or bias[i] <= 0
        if allow_long and lplj >= 0 and L[i] < L[lplj] and C[i] > L[lplj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lphj >= 0 and C[k] > H[lphj]:
                    fvg = find_fvg(H, L, i, k, "bull")
                    if fvg:
                        stop = L[i] * (1 - STOP_BUF_FRAC); R = fvg - stop
                        if R > 0:
                            setup = ("bull", fvg, stop, fvg + target_r * R, k)
                    break
        if setup is None and allow_short and lphj >= 0 and H[i] > H[lphj] and C[i] < H[lphj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lplj >= 0 and C[k] < L[lplj]:
                    fvg = find_fvg(H, L, i, k, "bear")
                    if fvg:
                        stop = H[i] * (1 + STOP_BUF_FRAC); R = stop - fvg
                        if R > 0:
                            setup = ("bear", fvg, stop, fvg - target_r * R, k)
                    break
        if setup:
            d, entry, stop, tgt, k = setup
            fi = None
            for f in range(k + 1, min(n, k + 1 + ENTRY_EXPIRY)):
                if (d == "bull" and L[f] <= stop) or (d == "bear" and H[f] >= stop):
                    break
                if (d == "bull" and L[f] <= entry) or (d == "bear" and H[f] >= entry):
                    fi = f; break
            if fi is not None:
                exit_px = reason = None; end = min(n, fi + MAX_HOLD)
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
                pnl = eq * (ret - 2 * cost_bps / 1e4)
                eq += pnl; trades.append({"ret": ret, "pnl": pnl}); eqc.append(eq)
                i = m + 1; continue
        i += 1
    pnl = np.array([t["pnl"] for t in trades]) if trades else np.array([])
    if len(pnl) == 0:
        return None
    eqc = np.array(eqc); peak = np.maximum.accumulate(eqc)
    dd = ((eqc - peak) / peak).min()
    pf = pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum()) if (pnl < 0).any() else float("inf")
    return dict(n=len(pnl), win=(pnl > 0).mean(), roi=eq / START - 1, final=eq, dd=dd, pf=pf)


def main():
    df1 = pd.read_csv(os.path.join(CACHE, "BTC_1m_max.csv"), index_col=0, parse_dates=True)
    df1 = df1[~df1.index.duplicated(keep="last")].sort_index()
    if df1.index.tz is None:
        df1.index = df1.index.tz_localize("UTC")
    print(f"[data] BTC {df1.index[0].date()} -> {df1.index[-1].date()} ({len(df1):,} 1m bars)\n", flush=True)
    TFS = [("1m", "1min"), ("3m", "3min"), ("5m", "5min"), ("15m", "15min")]
    for tag, kz, bias, tr in [("ICT-2022 killzone+bias+2R", True, True, 2.0),
                              ("ICT-2022 killzone+bias+3R", True, True, 3.0),
                              ("ICT-2022 killzone, NO bias, 2R", True, False, 2.0),
                              ("BASE 24/7, no bias, 2R", False, False, 2.0)]:
        print(f"=== {tag} ===  BTC, $50k, no leverage")
        print(f"{'TF':<5}{'trades':>8}{'win%':>7}{'ROI%(0bps)':>12}{'ROI%(2bps)':>12}{'maxDD%':>9}{'PF':>7}{'final$':>12}")
        for label, rule in TFS:
            d = resample(df1, rule)
            m0 = run(d, 0.0, tr, kz, bias); mc = run(d, 2.0, tr, kz, bias)
            if not mc:
                print(f"{label:<5} no trades"); continue
            print(f"{label:<5}{mc['n']:>8}{mc['win']*100:>6.0f}{m0['roi']*100:>11.0f}{mc['roi']*100:>12.0f}"
                  f"{mc['dd']*100:>9.0f}{mc['pf']:>7.2f}{mc['final']:>12,.0f}")
        print()


if __name__ == "__main__":
    main()
