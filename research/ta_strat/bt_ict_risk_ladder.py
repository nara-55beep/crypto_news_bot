"""
bt_ict_risk_ladder.py — adds the user's DE-RISKING stop/size ladder to the ICT SM strategy:
risk 2% of equity per trade; after each consecutive LOSS halve it (1% -> 0.5% -> 0.25% ...);
after a WIN reset to 2%. (Sizing the position so the structural ICT stop = that % of equity,
which needs some leverage on tight stops — capped at LEV_CAP.)

Separates STRATEGY (which trades) from SIZING (how much), so we compare on the SAME trades:
  * 1x no-leverage (the earlier baseline)
  * fixed 2% risk (no ladder)
  * 2% LADDER  (halve after loss, reset on win)  <- the requested scheme
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_ict_sm_tf import resample, pivots, find_fvg
from bt_ict_2022 import in_killzone_et, NY, MSS_WINDOW, ENTRY_EXPIRY, MAX_HOLD, LEFT, RIGHT, STOP_BUF_FRAC

START = 50_000.0
LEV_CAP = 20.0
BASE_RISK = 0.02
RISK_FLOOR = BASE_RISK / 16        # 0.125% — stop halving here ("and so on")


def detect_trades(df, target_r, killzone, bias_on):
    """Return the ordered trade outcomes {ret, stop_frac, reason} — NO sizing yet."""
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(C)
    if n < 50:
        return []
    et_hr = df.index.tz_convert(NY).hour.values if killzone else np.zeros(n, int)
    if bias_on:
        dc = df["close"].resample("1D").last().dropna()
        ema = dc.ewm(span=20, adjust=False).mean()
        bser = pd.Series(np.where(dc > ema, 1, -1), index=dc.index).shift(1)
        bmap = {d.date(): int(v) for d, v in bser.dropna().items()}
        bias = np.array([bmap.get(d, 0) for d in df.index.tz_convert(NY).date])
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
    trades = []; i = RIGHT + 1
    while i < n - 2:
        if killzone and not in_killzone_et(et_hr[i]):
            i += 1; continue
        lphj, lplj = last_ph[i], last_pl[i]
        setup = None
        al = (not bias_on) or bias[i] >= 0
        ash = (not bias_on) or bias[i] <= 0
        if al and lplj >= 0 and L[i] < L[lplj] and C[i] > L[lplj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lphj >= 0 and C[k] > H[lphj]:
                    f = find_fvg(H, L, i, k, "bull")
                    if f:
                        stop = L[i] * (1 - STOP_BUF_FRAC); R = f - stop
                        if R > 0: setup = ("bull", f, stop, f + target_r * R, k)
                    break
        if setup is None and ash and lphj >= 0 and H[i] > H[lphj] and C[i] < H[lphj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lplj >= 0 and C[k] < L[lplj]:
                    f = find_fvg(H, L, i, k, "bear")
                    if f:
                        stop = H[i] * (1 + STOP_BUF_FRAC); R = stop - f
                        if R > 0: setup = ("bear", f, stop, f - target_r * R, k)
                    break
        if setup:
            d, entry, stop, tgt, k = setup
            fi = None
            for ff in range(k + 1, min(n, k + 1 + ENTRY_EXPIRY)):
                if (d == "bull" and L[ff] <= stop) or (d == "bear" and H[ff] >= stop): break
                if (d == "bull" and L[ff] <= entry) or (d == "bear" and H[ff] >= entry): fi = ff; break
            if fi is not None:
                ex = rsn = None; end = min(n, fi + MAX_HOLD)
                for m in range(fi, end):
                    if d == "bull":
                        if L[m] <= stop: ex, rsn = stop, "stop"; break
                        if H[m] >= tgt: ex, rsn = tgt, "target"; break
                    else:
                        if H[m] >= stop: ex, rsn = stop, "stop"; break
                        if L[m] <= tgt: ex, rsn = tgt, "target"; break
                if ex is None:
                    m = end - 1; ex, rsn = C[m], "time"
                ret = (1 if d == "bull" else -1) * (ex / entry - 1.0)
                trades.append({"ret": ret, "stop_frac": abs(entry - stop) / entry, "reason": rsn})
                i = m + 1; continue
        i += 1
    return trades


def size_equity(trades, mode, cost_bps):
    """Walk trades, apply sizing -> equity curve, levs. mode: 'allin'|'fixed'|'ladder'."""
    eq = START; eqc = [START]; levs = []; consec = 0; cost = 2 * cost_bps / 1e4
    for t in trades:
        sf = max(t["stop_frac"], 1e-5)
        if mode == "allin":
            lev = 1.0
        elif mode == "fixed":
            lev = min(BASE_RISK / sf, LEV_CAP)
        else:  # ladder
            risk = max(BASE_RISK / (2 ** consec), RISK_FLOOR)
            lev = min(risk / sf, LEV_CAP)
        levs.append(lev)
        pf = max(lev * (t["ret"] - cost), -1.0)
        eq = max(eq * (1 + pf), 0.0)
        eqc.append(eq)
        if pf > 0: consec = 0
        else: consec += 1
        if eq <= 1.0: break
    eqc = np.array(eqc); peak = np.maximum.accumulate(eqc); dd = ((eqc - peak) / peak).min()
    return dict(final=eq, roi=eq / START - 1, dd=dd, avglev=np.mean(levs) if levs else 0)


def main():
    BTC = pd.read_csv(os.path.join(os.path.dirname(__file__), "cache", "BTC_1m_max.csv"), index_col=0, parse_dates=True)
    BTC = BTC[~BTC.index.duplicated(keep="last")].sort_index()
    if BTC.index.tz is None: BTC.index = BTC.index.tz_localize("UTC")
    def loadf(nm):
        df = pd.read_csv(os.path.join(os.path.dirname(__file__), "cache", f"{nm}_1m_3y.csv"))
        df["dt"] = pd.to_datetime(df["dt_utc"], utc=True)
        return df.set_index("dt")[["open", "high", "low", "close", "volume"]].sort_index()
    # best configs found: BTC 15m base, BTC 5m killzone, NQ 15m base, ES 15m base
    CFG = [("BTC 15m base", BTC, "15min", 2.0, False, False),
           ("BTC 5m killzone", BTC, "5min", 2.0, True, False),
           ("NQ1! 15m base", loadf("nq"), "15min", 2.0, False, False),
           ("ES1! 15m base", loadf("es"), "15min", 2.0, False, False),
           ("CL1! 15m base", loadf("cl"), "15min", 2.0, False, False)]
    print("Effect of the de-risking SIZE ladder (2% -> 1% -> 0.5% after losses, reset on win)")
    print("vs fixed-2% and 1x no-leverage. 3 years, $50k, net of ~2bps/side.\n")
    print(f"{'config':<18}{'trades':>7} | {'1x no-lev ROI%':>14} | {'fixed 2% ROI%':>14}{'DD%':>7}{'lev':>5}"
          f" | {'2% LADDER ROI%':>15}{'DD%':>7}{'lev':>5}")
    print("-" * 104)
    for name, df1, rule, tr, kz, bias in CFG:
        d = resample(df1, rule)
        trades = detect_trades(d, tr, kz, bias)
        a = size_equity(trades, "allin", 2.0)
        f = size_equity(trades, "fixed", 2.0)
        l = size_equity(trades, "ladder", 2.0)
        print(f"{name:<18}{len(trades):>7} | {a['roi']*100:>13.0f} | {f['roi']*100:>13.0f}{f['dd']*100:>7.0f}"
              f"{f['avglev']:>5.1f} | {l['roi']*100:>14.0f}{l['dd']*100:>7.0f}{l['avglev']:>5.1f}")
    print("-" * 104)
    print("avglev = average leverage the 2%-risk sizing needed (ICT stops are tight). LADDER lowers it after losses.")


if __name__ == "__main__":
    main()
