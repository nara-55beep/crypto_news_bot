"""
bt_silver_bullet.py - ICT New York AM Silver Bullet, mechanical backtest (pure pandas).

Sequence per day (NY time, DST-aware):
  1. 09:30-09:59  -> BSL = highest high, SSL = lowest low  (morning liquidity)
  2. 10:00-10:59  -> the Silver Bullet hour. First liquidity sweep starts a setup:
       BULL: a candle Low strictly < SSL -> wait for MSS (close ABOVE the highest 3-candle
             swing-high formed before the sweep) -> the breaking candle (or the next) must
             leave a bullish FVG (Low[c3] > High[c1]); limit-buy at High[c1] (gap's lower edge).
       BEAR: mirror (sweep > BSL, close below lowest swing-low, bearish FVG High[c3] < Low[c1],
             limit-sell at Low[c1]).
  3. Entry fills when a later candle (<= 10:59) trades through the limit; else cancelled at 11:00.
  4. SL = sweep extreme (low for longs / high for shorts);  TP = fixed R:R (default 1:2).
     Once filled, the trade is managed to SL/TP using the FULL day's bars (may run past 11:00).

No swallowed errors (full traceback). Verbose per-day logger. Outputs trades + metrics.
"""
from __future__ import annotations

import os
import sys
import traceback
from collections import Counter

import numpy as np
import pandas as pd

NY = "America/New_York"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.columns = [str(c).lower() for c in d.columns]
    if not isinstance(d.index, pd.DatetimeIndex):
        tcol = next((c for c in d.columns if c in ("timestamp", "datetime", "dt_utc", "date", "time")), None)
        if tcol is None:
            raise ValueError(f"No timestamp column found in {list(d.columns)}")
        d[tcol] = pd.to_datetime(d[tcol], utc=True)
        d = d.set_index(tcol)
    if d.index.tz is None:
        d.index = d.index.tz_localize("UTC")
    d.index = d.index.tz_convert(NY)            # DST handled automatically by the zone
    keep = ["open", "high", "low", "close"] + (["volume"] if "volume" in d.columns else [])
    return d[keep].sort_index()


def _hhmm(ts) -> str:
    return pd.Timestamp(ts).strftime("%H:%M")


def _run_day(day, g: pd.DataFrame, rr: float, log) -> dict:
    minute = g.index.hour * 60 + g.index.minute
    liq = g[(minute >= 570) & (minute <= 599)]               # 09:30-09:59
    if len(liq) < 5:
        log(f"Day {day}: insufficient 09:30-09:59 data. Skipping day."); return {"outcome": "no_liq_data", "trade": None}
    BSL = float(liq["high"].max()); SSL = float(liq["low"].min())
    log(f"Day {day}: BSL/SSL calculated successfully. BSL={BSL:.2f} SSL={SSL:.2f}")

    sb = g[(minute >= 600) & (minute <= 659)]                # 10:00-10:59 (cancel at 11:00)
    if len(sb) < 5:
        log(f"Day {day}: insufficient 10:00-11:00 data. Skipping day."); return {"outcome": "no_sb_data", "trade": None}
    H = sb["high"].values; L = sb["low"].values; C = sb["close"].values
    T = sb.index; n = len(sb)

    sh = np.zeros(n, bool); sl = np.zeros(n, bool)           # 3-candle swing fractals
    for j in range(1, n - 1):
        if H[j] > H[j - 1] and H[j] > H[j + 1]: sh[j] = True
        if L[j] < L[j - 1] and L[j] < L[j + 1]: sl[j] = True

    sweep_dir = sweep_i = None                               # first sweep, either side
    for i in range(n):
        if L[i] < SSL: sweep_dir, sweep_i = "bull", i; break
        if H[i] > BSL: sweep_dir, sweep_i = "bear", i; break
    if sweep_dir is None:
        log(f"Day {day}: no liquidity sweep inside 10:00-11:00. Skipping day."); return {"outcome": "no_sweep", "trade": None}
    swept_px = L[sweep_i] if sweep_dir == "bull" else H[sweep_i]
    log(f"Day {day} {_hhmm(T[sweep_i])}: {'SSL' if sweep_dir=='bull' else 'BSL'} swept at {swept_px:.2f}. Waiting for MSS...")

    if sweep_dir == "bull":
        prior = [j for j in range(sweep_i) if sh[j]]
        if not prior:
            log(f"Day {day}: sweep but no swing-high formed before it. Skipping day."); return {"outcome": "no_mss_ref", "trade": None}
        ref = max(H[j] for j in prior)
        mss = next((k for k in range(sweep_i + 1, n) if C[k] > ref), None)
        if mss is None:
            log(f"Day {day}: SSL swept but no MSS (no close above {ref:.2f}). Skipping day."); return {"outcome": "no_mss", "trade": None}
        log(f"Day {day} {_hhmm(T[mss])}: MSS - closed above swing-high {ref:.2f}.")
        fvg = None
        if mss + 1 < n and L[mss + 1] > H[mss - 1]:
            fvg = (H[mss - 1], L[mss + 1], mss + 1)
        elif mss + 2 < n and L[mss + 2] > H[mss]:
            fvg = (H[mss], L[mss + 2], mss + 2)
        if fvg is None:
            log(f"Day {day} {_hhmm(T[mss])}: MSS occurred, but no valid FVG was created. Skipping day."); return {"outcome": "mss_no_fvg", "trade": None}
        entry, gap_top, c3 = fvg
        stop = float(L[sweep_i:mss + 1].min())
        if not (stop < entry):
            log(f"Day {day}: invalid levels (stop {stop:.2f} !< entry {entry:.2f}). Skipping day."); return {"outcome": "invalid", "trade": None}
        direction = "long"
    else:
        prior = [j for j in range(sweep_i) if sl[j]]
        if not prior:
            log(f"Day {day}: sweep but no swing-low formed before it. Skipping day."); return {"outcome": "no_mss_ref", "trade": None}
        ref = min(L[j] for j in prior)
        mss = next((k for k in range(sweep_i + 1, n) if C[k] < ref), None)
        if mss is None:
            log(f"Day {day}: BSL swept but no MSS (no close below {ref:.2f}). Skipping day."); return {"outcome": "no_mss", "trade": None}
        log(f"Day {day} {_hhmm(T[mss])}: MSS - closed below swing-low {ref:.2f}.")
        fvg = None
        if mss + 1 < n and H[mss + 1] < L[mss - 1]:
            fvg = (L[mss - 1], H[mss + 1], mss + 1)
        elif mss + 2 < n and H[mss + 2] < L[mss]:
            fvg = (L[mss], H[mss + 2], mss + 2)
        if fvg is None:
            log(f"Day {day} {_hhmm(T[mss])}: MSS occurred, but no valid FVG was created. Skipping day."); return {"outcome": "mss_no_fvg", "trade": None}
        entry, gap_bot, c3 = fvg
        stop = float(H[sweep_i:mss + 1].max())
        if not (stop > entry):
            log(f"Day {day}: invalid levels (stop {stop:.2f} !> entry {entry:.2f}). Skipping day."); return {"outcome": "invalid", "trade": None}
        direction = "short"

    risk = abs(entry - stop)
    tp = entry + rr * risk if direction == "long" else entry - rr * risk
    log(f"Day {day} {_hhmm(T[c3])}: FVG created, Limit Order placed at {entry:.2f} (SL {stop:.2f}, TP {tp:.2f}).")

    fill_i = None
    for f in range(c3 + 1, n):
        if (direction == "long" and L[f] <= entry) or (direction == "short" and H[f] >= entry):
            fill_i = f; break
    if fill_i is None:
        log(f"Day {day} 11:00: Limit Order expired unfilled. Closing setup."); return {"outcome": "unfilled", "trade": None}
    fill_t = T[fill_i]
    log(f"Day {day} {_hhmm(fill_t)}: Limit Order FILLED at {entry:.2f}.")

    mgmt = g[g.index >= fill_t]                               # manage on the full day (may run past 11:00)
    exit_t = exit_px = R = reason = None
    for t, row in mgmt.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if direction == "long":
            if lo <= stop: exit_px, R, reason = stop, -1.0, "SL"; exit_t = t; break
            if hi >= tp: exit_px, R, reason = tp, rr, "TP"; exit_t = t; break
        else:
            if hi >= stop: exit_px, R, reason = stop, -1.0, "SL"; exit_t = t; break
            if lo <= tp: exit_px, R, reason = tp, rr, "TP"; exit_t = t; break
    if R is None:                                            # neither hit by EOD -> mark out at last close
        last_t = mgmt.index[-1]; exit_px = float(mgmt.iloc[-1]["close"]); exit_t = last_t; reason = "EOD"
        R = (exit_px - entry) / risk if direction == "long" else (entry - exit_px) / risk
    outcome = "win" if R > 0 else "loss"
    log(f"Day {day} {_hhmm(exit_t)}: EXIT {reason} @ {exit_px:.2f} -> {R:+.2f}R ({outcome.upper()})")
    return {"outcome": outcome, "trade": {
        "day": str(day), "direction": direction, "entry_time": fill_t, "exit_time": exit_t,
        "entry": round(entry, 2), "stop": round(stop, 2), "tp": round(tp, 2),
        "exit": round(exit_px, 2), "reason": reason, "R": round(R, 3), "outcome": outcome}}


def backtest_silver_bullet(df: pd.DataFrame, rr: float = 2.0, verbose: bool = False):
    d = _prep(df)
    days = d.index.normalize()
    trades = []; outcomes = Counter()
    log = (lambda m: print(m)) if verbose else (lambda m: None)
    for day, g in d.groupby(days):
        try:
            res = _run_day(day.date(), g, rr, log)
            outcomes[res["outcome"]] += 1
            if res["trade"]:
                trades.append(res["trade"])
        except Exception:
            traceback.print_exc()                            # never swallow
    return pd.DataFrame(trades), outcomes


def summarize(name: str, trades: pd.DataFrame, outcomes: Counter):
    print(f"\n===== {name} =====")
    skips = {k: v for k, v in outcomes.items() if k not in ("win", "loss")}
    print(f"  trading days processed : {sum(outcomes.values())}")
    print(f"  day outcomes           : {dict(outcomes)}")
    if len(trades) == 0:
        print("  no trades."); return None
    n = len(trades); wins = int((trades["R"] > 0).sum())
    total_R = trades["R"].sum()
    eq = trades["R"].cumsum().values
    mdd = float((eq - np.maximum.accumulate(eq)).min())
    print(f"  TOTAL TRADES           : {n}")
    print(f"  WIN RATE %             : {wins / n * 100:.1f}%")
    print(f"  TOTAL P/L (R-multiples): {total_R:+.1f}R   (avg {total_R / n:+.3f}R/trade)")
    print(f"  MAX DRAWDOWN (R)       : {mdd:.1f}R")
    print(f"  longs/shorts           : {(trades['direction']=='long').sum()}/{(trades['direction']=='short').sum()}")
    return {"name": name, "trades": n, "win%": round(wins / n * 100, 1),
            "total_R": round(total_R, 1), "max_dd_R": round(mdd, 1)}


def main():
    rr = 2.0
    summ = []
    for mkt in ["es", "nq", "cl"]:
        path = os.path.join(CACHE, f"{mkt}_1m_3y.csv")
        if not os.path.exists(path):
            print(f"{mkt}: data missing"); continue
        df = pd.read_csv(path)
        trades, outcomes = backtest_silver_bullet(df, rr=rr, verbose=False)
        s = summarize(f"{mkt.upper()} — Silver Bullet 1:{int(rr)}", trades, outcomes)
        if s:
            summ.append(s)
            print("  sample trades:")
            for _, t in trades.head(6).iterrows():
                print(f"    {t['day']} {t['direction']:<5} in {_hhmm(t['entry_time'])} out {_hhmm(t['exit_time'])} "
                      f"{t['reason']:<3} {t['R']:+.1f}R ({t['outcome']})")

    print("\n===== PERFORMANCE SUMMARY (R:R = 1:2) =====")
    print(pd.DataFrame(summ).to_string(index=False) if summ else "no results")

    # demonstrate the verbose per-day logger on one recent month (NQ)
    print("\n===== VERBOSE LOGGER DEMO — NQ, last ~20 trading days =====")
    df = pd.read_csv(os.path.join(CACHE, "nq_1m_3y.csv"))
    d = _prep(df); cutoff = d.index.normalize().unique()[-20]
    backtest_silver_bullet(df[pd.to_datetime(df["dt_utc"], utc=True).dt.tz_convert(NY) >= cutoff], rr=rr, verbose=True)


if __name__ == "__main__":
    main()
