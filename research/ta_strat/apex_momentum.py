"""
apex_momentum.py — the Zarattini & Aziz "Intraday Momentum" strategy (SSRN 4824172), the only
academically-replicated intraday futures edge (~Sharpe 1.3, ~40% win, convex payoff). Volatility
"noise band" around the session open that widens with sqrt(time); trade momentum THROUGH the band
(long above upper / short below lower), band is the trailing stop, flat by session close.
Tested on ES/NQ/CL through the same Apex k-fold + 1-month EOD pass, head-to-head vs NR7.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, trades_to_records, apex_eval
from apex_strats2 import sessions, daily_ohlc, nr7_orb
from apex_walkforward import FOLDS, in_fold, expR


def intraday_momentum(df, name, sig_lb=14, k=1.0, start_min=30, manage="none"):
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess)
    day_absret = np.abs(dc / o - 1.0)
    sigma = pd.Series(day_absret).rolling(sig_lb, min_periods=5).mean().shift(1).values
    trades = []
    for ki, (d_ord, a, b) in enumerate(sess):
        s = sigma[ki]
        if not (s > 0):
            continue
        op = O[a]; M = b - a + 1
        pos = 0; entry = e_stop = fi = None; madv = mfav = 0.0
        for m in range(a, b + 1):
            frac = (m - a + 1) / M
            band = op * s * np.sqrt(frac) * k
            upper = op + band; lower = op - band
            px = C[m]
            if pos == 0:
                if (m - a) >= start_min:
                    if px > upper:
                        pos = 1; entry = px; e_stop = lower; fi = m; madv = mfav = 0.0
                    elif px < lower:
                        pos = -1; entry = px; e_stop = upper; fi = m; madv = mfav = 0.0
            else:
                fav = pos * (H[m] - entry) if pos > 0 else pos * (L[m] - entry)
                fav = pos * ((H[m] if pos > 0 else L[m]) - entry)
                adv = pos * ((L[m] if pos > 0 else H[m]) - entry)
                mfav = max(mfav, fav); madv = min(madv, adv)
                exit_now = (pos == 1 and px < lower) or (pos == -1 and px > upper) or (m == b)
                if exit_now:
                    risk = abs(entry - e_stop)
                    if risk > 0:
                        ret = pos * (px - entry) / risk
                        tgt = entry + pos * 2 * risk
                        trades.append((fi, pos, entry, e_stop, tgt, m, "mom",
                                       ret, madv / risk, mfav / risk))
                    pos = 0
    return trades_to_records(trades, df, name)


def best_pass_1mo(recs, trail="eod", horizon=30):
    best = None
    for R in [200, 300, 400, 500, 600, 800]:
        m = apex_eval(recs, R, start_step=2, horizon_days=horizon, trail=trail)
        if not m:
            continue
        total = m["passes"] + m["fails"] + m["censored"]
        pr = m["passes"] / total if total else 0
        if best is None or pr > best[0]:
            best = (pr, R, m)
    return best


def main():
    print("ZARATTINI INTRADAY MOMENTUM vs NR7  —  ES/NQ/CL, 3y\n")
    print(f"{'block':<22}{'n':>5}{'Y1':>7}{'Y2':>7}{'Y3':>7}{'win%':>6}  signs")
    mom = {}; nr7 = {}
    for m in ["es", "nq", "cl"]:
        df = load_fut(m)
        rmom, _ = intraday_momentum(df, m, manage="partial")
        rnr7, _ = nr7_orb(df, m, manage="partial")
        mom[m] = rmom; nr7[m] = rnr7
        for tagn, r in [(f"momentum_{m}", rmom)]:
            evs = [expR(in_fold(r, lo, hi)) for (lo, hi) in FOLDS]
            signs = "".join("+" if e > 0.03 else ("." if e > -0.03 else "-") for e in evs)
            w = float(np.mean([x["pnl_R"] > 0 for x in r])) * 100 if r else 0
            print(f"{tagn:<22}{len(r):>5}{evs[0]:>+7.2f}{evs[1]:>+7.2f}{evs[2]:>+7.2f}{w:>6.0f}  {signs}")

    def merge(d):
        out = []
        for v in d.values():
            out.extend(v)
        out.sort(key=lambda r: (r["eday"], r["xday"]))
        return out
    pmom = merge(mom); pnr7 = merge(nr7); pall = sorted(pmom + pnr7, key=lambda r: (r["eday"], r["xday"]))
    print("\n1-MONTH EOD PASS (reach +$3k within 30d, no breach):")
    for nm, port in [("Momentum (ES+NQ+CL)", pmom), ("NR7 (ES+NQ+CL)", pnr7),
                     ("Momentum + NR7", pall)]:
        bp = best_pass_1mo(port)
        if bp:
            pr, R, mm = bp
            print(f"  {nm:<22} n={len(port):>4} expR={expR(port):+.2f}  1mo-pass {pr*100:.0f}% @ ${R} "
                  f"(blowups {mm['fails']}, medDays {mm['med_days']})")


if __name__ == "__main__":
    main()
