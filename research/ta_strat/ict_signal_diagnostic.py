"""
DIAGNOSTIC ONLY (no new strategy, no optimization): does the ICT sweep+MSS+FVG signal have raw
directional edge BEFORE stop/target rules?

For every valid signal (entry = 50% of the FVG, stop = sweep extreme, 1R = |entry-stop|), measure
over the rest of the session: MFE, MAE (ticks & R), and the first-touch race — did price reach
+0.5R/+1R/+1.5R/+2R BEFORE -1R (conservative: same-bar tie = stop wins). Break down by market, side,
time window, weekday, ATR regime, VWAP side, and which liquidity level was swept.

Signals come from the existing engine with filters OFF and a light displacement (0.5 body / 1.0 ATR)
so we diagnose the raw signal population, not a filtered subset. Regular-session data -> overnight
liquidity not available (noted).
"""
from __future__ import annotations
import os, sys
from dataclasses import asdict
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ict_sb_2022_backtester import load_csv, Cfg, backtest, _mins, CACHE

TARGETS = [0.5, 1.0, 1.5, 2.0]


def measure(after, entry, stop, direction, tick):
    Rp = abs(entry - stop)
    if Rp <= 0 or len(after) == 0:
        return None
    H = after["high"].values; L = after["low"].values
    lng = direction == "long"
    mfe = mae = 0.0
    for h, l in zip(H, L):
        fav = (h - entry) if lng else (entry - l)
        adv = (entry - l) if lng else (h - entry)
        if fav > mfe: mfe = fav
        if adv > mae: mae = adv
    hits = {x: None for x in TARGETS}
    for h, l in zip(H, L):
        fav = (h - entry) if lng else (entry - l)
        adv = (entry - l) if lng else (h - entry)
        if adv >= Rp:                                  # -1R hit first (stop)
            for x in hits:
                if hits[x] is None: hits[x] = False
            break
        for x in hits:
            if hits[x] is None and fav >= x * Rp: hits[x] = True
    for x in hits:
        if hits[x] is None: hits[x] = False
    return dict(mfe_t=mfe / tick, mae_t=mae / tick, mfe_R=mfe / Rp, mae_R=mae / Rp,
                h05=hits[0.5], h1=hits[1.0], h15=hits[1.5], h2=hits[2.0])


def diagnose(symbol, df, tick):
    cfg = Cfg(symbol=symbol, tick=tick, window="AM", disp_body=0.5, disp_atr=1.0,
              f_bias=False, f_vwap=False, f_minR=False, f_maxstop=False, f_news=False)
    trades, _ = backtest(df, cfg)
    if len(trades) == 0:
        print(f"{symbol}: no signals"); return None
    df = df.copy(); df["date"] = df.index.normalize()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap"] = tp.groupby(df["date"]).cumsum() / pd.Series(1.0, index=df.index).groupby(df["date"]).cumsum()
    drange = df.groupby("date").apply(lambda x: x["high"].max() - x["low"].min())
    atr_med = drange.median()
    rows = []
    for _, t in trades.iterrows():
        et = pd.Timestamp(t["entry_time"])
        day = et.normalize()
        dd = df[(df["date"] == day) & (df.index >= et)]
        m = measure(dd, t["entry"], t["stop"], t["direction"], tick)
        if m is None: continue
        vwap_at = float(df.loc[df.index <= et, "vwap"].iloc[-1]) if (df.index <= et).any() else np.nan
        m.update(symbol=symbol, direction=t["direction"], liquidity=t["liquidity"],
                 minute=_mins(pd.DatetimeIndex([et]))[0], dow=et.day_name(),
                 atr_hi=(drange.get(day, atr_med) >= atr_med),
                 vwap_side=("above" if t["entry"] >= vwap_at else "below"))
        rows.append(m)
    return pd.DataFrame(rows)


def block(name, d):
    if len(d) == 0:
        print(f"  {name:<22} n=0"); return
    print(f"  {name:<22} n={len(d):>4}  +0.5R {d.h05.mean()*100:>3.0f}%  +1R {d.h1.mean()*100:>3.0f}%  "
          f"+1.5R {d.h15.mean()*100:>3.0f}%  +2R {d.h2.mean()*100:>3.0f}%  | "
          f"MFE {d.mfe_R.mean():.2f}R MAE {d.mae_R.mean():.2f}R")


def report(symbol, D):
    print(f"\n{'='*78}\n{symbol}: {len(D)} signals — raw MFE/MAE & first-touch race (rest-of-session)\n{'='*78}")
    print(f"  OVERALL  MFE avg {D.mfe_R.mean():.2f}R (med {D.mfe_R.median():.2f}R, {D.mfe_t.mean():.0f}t) | "
          f"MAE avg {D.mae_R.mean():.2f}R (med {D.mae_R.median():.2f}R, {D.mae_t.mean():.0f}t)")
    print(f"  reached before -1R:  +0.5R {D.h05.mean()*100:.0f}%   +1R {D.h1.mean()*100:.0f}%   "
          f"+1.5R {D.h15.mean()*100:.0f}%   +2R {D.h2.mean()*100:.0f}%")
    print("  -- by side --")
    for s in ["long", "short"]:
        block(s, D[D.direction == s])
    print("  -- by time window --")
    block("09:30-10:00", D[D.minute < 600]); block("10:00-11:00", D[D.minute >= 600])
    print("  -- by weekday --")
    for w in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
        block(w, D[D.dow == w])
    print("  -- by ATR regime --")
    block("high-ATR day", D[D.atr_hi]); block("low-ATR day", D[~D.atr_hi])
    print("  -- by VWAP side --")
    block("entry above VWAP", D[D.vwap_side == "above"]); block("entry below VWAP", D[D.vwap_side == "below"])
    print("  -- by liquidity swept --")
    for lq in sorted(D.liquidity.unique()):
        block(lq, D[D.liquidity == lq])


def expectancy(D):
    p05, p1, p15, p2 = D.h05.mean(), D.h1.mean(), D.h15.mean(), D.h2.mean()
    e1 = 2 * p1 - 1                       # 1R target, 1R stop
    e15 = 2.5 * p15 - 1                   # 1.5R target
    e2 = 3 * p2 - 1                       # 2R target
    # BE after +0.5R (approx): reach 2R -> +2; reach 0.5R but not 2R -> ~0 (BE); never 0.5R -> -1
    ebe = p2 * 2 + (p05 - p2) * 0.0 - (1 - p05) * 1
    return dict(e_1R=round(e1, 3), e_1_5R=round(e15, 3), e_2R=round(e2, 3), e_BE_after_0_5R=round(ebe, 3))


def main():
    res = {}
    for symbol, fname, tick in [("MNQ", "nq_1m_3y.csv", 0.25), ("MES", "es_1m_3y.csv", 0.25)]:
        path = os.path.join(CACHE, fname)
        if not os.path.exists(path):
            print(f"{symbol} data missing"); continue
        D = diagnose(symbol, load_csv(path), tick)
        if D is None or len(D) == 0:
            continue
        report(symbol, D)
        res[symbol] = (D, expectancy(D))
    print(f"\n{'='*78}\nEXPECTANCY per trade (R), from the first-touch race (incl. the -1R stop):\n{'='*78}")
    print(f"  {'symbol':<7}{'1R tgt':>9}{'1.5R tgt':>10}{'2R tgt':>9}{'BE@0.5R':>10}")
    for sym, (D, e) in res.items():
        print(f"  {sym:<7}{e['e_1R']:>9}{e['e_1_5R']:>10}{e['e_2R']:>9}{e['e_BE_after_0_5R']:>10}")
    print("  (>0 = positive raw edge at that target.  1R needs P(+1R)>50%; 2R needs P(+2R)>33%.)")


if __name__ == "__main__":
    main()
