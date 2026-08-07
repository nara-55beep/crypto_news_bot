"""
apex_grind.py — the only path left to PASS Apex by skill: a HIGH-WIN-RATE, smooth-equity
strategy engineered for the $2,500 trailing drawdown (not ICT — built for the rule).

Idea: trade WITH the intraday trend (price vs session VWAP + EMA9/EMA20), enter on shallow
pullbacks to VWAP/EMA9 with a rejection close, take a NEAR target for a high hit-rate, tight
stop below the pullback. Small fixed $ risk -> integer ES contracts. NY session, flat 15:55,
daily profit-lock + daily stop so green days can't give back into the trailing DD.

Honest test: rolling 1-month Apex eval (+$3000 before -$2500 trailing DD) over 3 years, per
year. A "pass" only counts if it's RELIABLE across all years — not a lucky regime.
"""
from __future__ import annotations
import os, sys
from collections import Counter
from datetime import time as dtime
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CACHE = os.path.join(ROOT, "research", "ta_strat", "cache")
NY = "America/New_York"
PT, TICK, SLIP, COMM = 50.0, 0.25, 1.0, 4.0
START, TGT, DD, EVAL = 50_000.0, 3_000.0, 2_500.0, 20
RISK, MAXC = 350.0, 5
RR = 1.3                    # near target for a high hit-rate
KZ = (dtime(9, 35), dtime(15, 0)); FLAT = dtime(15, 55)
DAY_LOCK, DAY_STOP, MAXTR = 600.0, -500.0, 4
PULLBACK_ATR = 0.25        # within 0.25*ATR of EMA9 = a pullback touch


def load_rth():
    df = pd.read_csv(os.path.join(CACHE, "apex3yr_es_24h.csv"))
    df["dt"] = pd.to_datetime(df["dt_utc"], utc=True).dt.tz_convert(NY)
    df = df.sort_values("dt").reset_index(drop=True)
    df["tm"] = df["dt"].dt.time; df["date"] = df["dt"].dt.date
    df = df[(df["tm"] >= dtime(9, 30)) & (df["tm"] <= dtime(16, 0))].reset_index(drop=True)
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    # session VWAP (reset each day at 9:30)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["pv"] = tp * df["volume"].fillna(0)
    df["cpv"] = df.groupby("date")["pv"].cumsum()
    df["cv"] = df.groupby("date")["volume"].transform(lambda x: x.fillna(0).cumsum()).replace(0, np.nan)
    df["vwap"] = df["cpv"] / df["cv"]
    return df


def backtest(df):
    trades = []
    for date, g in df.groupby("date", sort=True):
        g = g.reset_index(drop=True)
        o, h, l, c = g["open"].values, g["high"].values, g["low"].values, g["close"].values
        e9, e20, vw, at = g["ema9"].values, g["ema20"].values, g["vwap"].values, g["atr"].values
        tm = g["tm"].values; dts = g["dt"].values
        n = len(g); pos = None; ntr = 0; dp = 0.0; i = 0
        while i < n:
            if pos is not None:
                s = pos["s"]; hi, lo = h[i], l[i]
                uL = s * (lo - pos["e"]) * PT * pos["q"]; uH = s * (hi - pos["e"]) * PT * pos["q"]
                pos["mae"] = min(pos["mae"], min(uL, uH)); pos["mfe"] = max(pos["mfe"], max(uL, uH))
                ex = rs = None
                if s > 0:
                    if lo <= pos["stop"]: ex, rs = pos["stop"], "stop"
                    elif hi >= pos["tgt"]: ex, rs = pos["tgt"], "target"
                else:
                    if hi >= pos["stop"]: ex, rs = pos["stop"], "stop"
                    elif lo <= pos["tgt"]: ex, rs = pos["tgt"], "target"
                if rs is None and tm[i] >= FLAT: ex, rs = c[i], "eod"
                if rs:
                    fill = ex - np.sign(s) * TICK * SLIP
                    pnl = s * (fill - pos["e"]) * PT * pos["q"] - COMM * pos["q"]
                    dp += pnl
                    trades.append({"date": date, "entry_dt": pos["edt"], "pnl": pnl,
                                   "mae": pos["mae"], "mfe": pos["mfe"], "reason": rs})
                    pos = None
                    if dp <= DAY_STOP or ntr >= MAXTR: break
                i += 1; continue
            if (KZ[0] <= tm[i] <= KZ[1] and ntr < MAXTR and DAY_STOP < dp < DAY_LOCK
                    and i >= 3 and np.isfinite(vw[i]) and at[i] > 0):
                up = c[i] > vw[i] and e9[i] > e20[i]
                dn = c[i] < vw[i] and e9[i] < e20[i]
                tol = PULLBACK_ATR * at[i]
                if up and l[i] <= e9[i] + tol and c[i] > e9[i] and c[i] > o[i]:      # pullback + bullish reject
                    stop = min(l[i], l[i - 1]) - TICK; R = c[i] - stop
                    if R > 0:
                        ep = c[i] + TICK * SLIP; q = int(np.clip(np.floor(RISK / (R * PT)), 1, MAXC))
                        pos = {"s": 1, "e": ep, "q": q, "stop": stop, "tgt": ep + RR * R,
                               "edt": dts[i], "mae": 0.0, "mfe": 0.0}; ntr += 1
                elif dn and h[i] >= e9[i] - tol and c[i] < e9[i] and c[i] < o[i]:
                    stop = max(h[i], h[i - 1]) + TICK; R = stop - c[i]
                    if R > 0:
                        ep = c[i] - TICK * SLIP; q = int(np.clip(np.floor(RISK / (R * PT)), 1, MAXC))
                        pos = {"s": -1, "e": ep, "q": q, "stop": stop, "tgt": ep - RR * R,
                               "edt": dts[i], "mae": 0.0, "mfe": 0.0}; ntr += 1
            i += 1
    return pd.DataFrame(trades)


def apex(trades, dates):
    if trades.empty: return 0, 0
    td = sorted(dates); idx = {d: k for k, d in enumerate(td)}
    tr = trades.copy(); tr["di"] = tr["date"].map(idx); np_ = nf = 0
    for s in range(len(td)):
        w = tr[(tr["di"] >= s) & (tr["di"] < s + EVAL)]
        if w.empty: continue
        eq = pk = START; out = None
        for _, t in w.iterrows():
            pk = max(pk, eq + t["mfe"])
            if eq + t["mae"] <= pk - DD: out = "f"; break
            eq += t["pnl"]; pk = max(pk, eq)
            if eq >= START + TGT: out = "p"; break
            if eq <= pk - DD: out = "f"; break
        if out == "p": np_ += 1
        elif out == "f": nf += 1
    return np_, nf


def rep(tr, df, label):
    print(f"\n===== {label} =====")
    if tr.empty: print("NO TRADES"); return
    p = tr["pnl"].values; w = p[p > 0]; lo = p[p < 0]
    pf = w.sum() / abs(lo.sum()) if lo.sum() else 99
    np_, nf = apex(tr, df["date"].unique()); tot = np_ + nf
    print(f"trades {len(tr)} ({len(tr)/df['date'].nunique():.2f}/day) | WIN {(p>0).mean()*100:.0f}% | "
          f"net ${p.sum():+,.0f} | PF {pf:.2f} | {dict(Counter(tr['reason']))}")
    print(f"APEX pass: {np_/tot*100:.0f}% ({np_}/{tot})" if tot else "no windows")


def main():
    df = load_rth()
    print(f"[data] RTH ES {len(df):,} bars, {df['date'].nunique()} days {df['dt'].iloc[0].date()}->{df['dt'].iloc[-1].date()}", flush=True)
    tr = backtest(df)
    rep(tr, df, "FULL 3 YEARS")
    last1 = df[df["dt"] >= df["dt"].iloc[-1] - pd.Timedelta(days=365)]
    rep(tr[tr["date"].isin(set(last1["date"]))], last1, "LAST 1 YEAR")
    print("\nper-year:")
    tr["yr"] = pd.to_datetime(tr["entry_dt"]).dt.year
    for y, gt in tr.groupby("yr"):
        gd = df[pd.to_datetime(df["dt"]).dt.year == y]; np_, nf = apex(gt, gd["date"].unique()); tot = np_ + nf
        print(f"  {y}: {len(gt)} tr, net ${gt['pnl'].sum():+,.0f}, WIN {(gt['pnl']>0).mean()*100:.0f}%, "
              f"apex {np_/tot*100:.0f}% ({np_}/{tot})" if tot else f"  {y}: no windows")


if __name__ == "__main__":
    main()
