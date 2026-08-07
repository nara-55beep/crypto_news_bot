"""
exit_comparison.py — isolate the EXIT system.

Method (so the comparison is clean):
  * Generate V2 ENTRIES once per symbol = signal ONSETS (bar where the V2 long/short
    condition first turns on). This entry set is exit-independent, so it is IDENTICAL
    across every exit variant.
  * Take each onset as an INDEPENDENT trade and run it through each of 9 exit variants
    with the SAME stop (1R = atr_stop_mult*ATR), fees and slippage.
  * Report per-trade stats in R (account-independent) + a non-compounded $ curve
    (fixed 0.5% risk per trade) for return/drawdown.

This isolates exits: same entries, same stop distance, only the exit rule changes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.config import Settings
from backend.data.fetcher import fetch_history_df
from backend.exchange import make_exchange
from backend.strategy.signal import Strategy

S = Settings()
DAYS = 180
RISK_FRAC = 0.005
START = 10_000.0
FEE = S.fee_rate
SLIP = S.slippage
STOP_MULT = S.atr_stop_mult       # 1R = this * ATR (same as V2)
TRAIL = S.trail_atr_mult          # 2.0
XBARS = S.time_stop_bars          # 16
PARTIAL = S.partial_frac          # 0.5
TF_HOURS = 0.25                    # 15m

VARIANTS = [
    "1_fix_1R", "2_fix_1.5R", "3_fix_2R",
    "4_partial_BE_trail", "5_trail_ATR_full", "6_trail_EMA20_full",
    "7_timestop_if_negative", "8_timestop_if_not_0.5R", "9_V2_original",
]


def sim(O, H, L, C, A, E20, i, side, variant):
    """Run one trade from bar i with the chosen exit. Returns a dict of results in R."""
    n = len(C)
    long = side == "long"
    entry = C[i] * (1 + SLIP) if long else C[i] * (1 - SLIP)
    R = A[i] * STOP_MULT
    if R <= 0 or np.isnan(R):
        return None
    qty = 1.0 / R                                   # risk_amount = 1 -> pnl in R
    fee_u = lambda px, q: px * q * FEE
    fees = fee_u(entry, qty)                         # entry fee (R)
    realized = -fees
    stop = entry - R if long else entry + R
    best = entry
    mae = 0.0                                        # max adverse excursion (R, <=0)
    mfe = 0.0                                        # max favorable excursion (R, >=0)
    remaining = qty
    partial_done = False

    def fav(px):                                     # favorable price move in R
        return (px - entry) / R if long else (entry - px) / R

    def close(px, q):
        nonlocal fees
        f = fee_u(px, q); fees += f
        return q * ((px - entry) if long else (entry - px)) - f

    tp = None
    if variant == "1_fix_1R":   tp = entry + R if long else entry - R
    if variant == "2_fix_1.5R": tp = entry + 1.5 * R if long else entry - 1.5 * R
    if variant == "3_fix_2R":   tp = entry + 2 * R if long else entry - 2 * R

    for j in range(i + 1, n):
        hi, lo, cl, atrj, e20 = H[j], L[j], C[j], A[j], E20[j]
        best = max(best, hi) if long else min(best, lo)
        mfe = max(mfe, fav(best))
        mae = min(mae, fav(lo if long else hi))
        bars = j - i

        # --- stop-first (conservative) on the CURRENT stop ---
        if (long and lo <= stop) or (not long and hi >= stop):
            fill = stop * (1 - SLIP) if long else stop * (1 + SLIP)
            realized += close(fill, remaining)
            return _res(realized, side, bars, mae, mfe, fees)

        # --- fixed TP variants ---
        if tp is not None and ((long and hi >= tp) or (not long and lo <= tp)):
            fill = tp * (1 - SLIP) if long else tp * (1 + SLIP)
            realized += close(fill, remaining)
            return _res(realized, side, bars, mae, mfe, fees)

        # --- partial + breakeven + ATR trail (variants 4 and 9) ---
        if variant in ("4_partial_BE_trail", "9_V2_original"):
            if not partial_done and ((long and hi >= entry + R) or (not long and lo <= entry - R)):
                t1 = entry + R if long else entry - R
                half = remaining * PARTIAL
                fill = t1 * (1 - SLIP) if long else t1 * (1 + SLIP)
                realized += close(fill, half)
                remaining -= half
                stop = entry                                       # breakeven
                partial_done = True
            elif partial_done:
                tr = (best - TRAIL * atrj) if long else (best + TRAIL * atrj)
                stop = max(stop, tr) if long else min(stop, tr)

        # --- full ATR trail (variants 5, 7, 8) ---
        if variant in ("5_trail_ATR_full", "7_timestop_if_negative", "8_timestop_if_not_0.5R"):
            tr = (best - TRAIL * atrj) if long else (best + TRAIL * atrj)
            stop = max(stop, tr) if long else min(stop, tr)

        # --- full EMA20 trail (variant 6) ---
        if variant == "6_trail_EMA20_full":
            if (long and cl < e20) or (not long and cl > e20):
                fill = cl * (1 - SLIP) if long else cl * (1 + SLIP)
                realized += close(fill, remaining)
                return _res(realized, side, bars, mae, mfe, fees)

        # --- time stops ---
        if variant in ("7_timestop_if_negative",) and bars >= XBARS:
            if fav(cl) < 0:
                fill = cl * (1 - SLIP) if long else cl * (1 + SLIP)
                realized += close(fill, remaining)
                return _res(realized, side, bars, mae, mfe, fees)
        if variant in ("8_timestop_if_not_0.5R", "9_V2_original") and bars >= XBARS:
            if mfe < 0.5:
                fill = cl * (1 - SLIP) if long else cl * (1 + SLIP)
                realized += close(fill, remaining)
                return _res(realized, side, bars, mae, mfe, fees)

    fill = C[-1] * (1 - SLIP) if long else C[-1] * (1 + SLIP)
    realized += close(fill, remaining)
    return _res(realized, side, n - 1 - i, mae, mfe, fees)


def _res(r, side, bars, mae, mfe, fees):
    return {"r": r, "side": side, "bars": bars, "mae": mae, "mfe": mfe, "fees_r": fees,
            "gross_r": r + fees, "breakeven": abs(r) < 0.1}


def metrics(trades):
    if not trades:
        return {"trades": 0}
    r = np.array([t["r"] for t in trades])
    wins, losses = r[r > 0], r[r < 0]
    # non-compounded $ curve ordered by trade sequence
    pnl = r * (RISK_FRAC * START)
    eq = START + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min()
    return {
        "trades": len(trades),
        "ret%": round(float(pnl.sum()) / START * 100, 2),
        "PF": round(float(wins.sum() / -losses.sum()), 3) if losses.sum() < 0 else None,
        "win%": round(len(wins) / len(r) * 100, 1),
        "maxDD%": round(float(dd) * 100, 2),
        "avgWin_R": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avgLoss_R": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "exp_R": round(float(r.mean()), 4),                 # expectancy per trade (net)
        "grossExp_R": round(float(np.mean([t["gross_r"] for t in trades])), 4),
        "feeDrag_R": round(float(np.mean([t["fees_r"] for t in trades])), 4),
        "avgHold_h": round(float(np.mean([t["bars"] for t in trades])) * TF_HOURS, 1),
        "breakeven": int(sum(t["breakeven"] for t in trades)),
        "fees$": round(float(sum(t["fees_r"] for t in trades)) * (RISK_FRAC * START), 1),
        "avgWinMFE_R": round(float(np.mean([t["mfe"] for t in trades if t["r"] > 0])), 2) if len(wins) else 0,
        "avgWinMAE_R": round(float(np.mean([t["mae"] for t in trades if t["r"] > 0])), 2) if len(wins) else 0,
    }


def side_split(trades, side):
    sub = [t for t in trades if t["side"] == side]
    if not sub:
        return {"trades": 0}
    r = np.array([t["r"] for t in sub])
    w = r[r > 0]; lo = r[r < 0]
    return {"trades": len(sub), "exp_R": round(float(r.mean()), 4),
            "PF": round(float(w.sum() / -lo.sum()), 3) if lo.sum() < 0 else None,
            "win%": round(len(w) / len(r) * 100, 1)}


def run():
    ex = make_exchange(S)
    strat = Strategy(S)
    all_trades = {v: [] for v in VARIANTS}
    per_symbol = {}

    for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        df15 = fetch_history_df(ex, sym, "15m", DAYS)
        df4h = fetch_history_df(ex, sym, "4h", DAYS)
        df = strat.prepare(df4h, df15).dropna(subset=["ema50", "atr", "atr_floor"])
        O, H, L, C, A, E20 = (df["open"].values, df["high"].values, df["low"].values,
                              df["close"].values, df["atr"].values, df["ema20"].values)
        # side per bar, then ONSETS (exit-independent entries, identical for all variants)
        sides = []
        for _, row in df.iterrows():
            sg = strat.evaluate_row(row, symbol=sym)
            sides.append(sg.side if sg else None)
        onsets = [(k, sides[k]) for k in range(1, len(sides))
                  if sides[k] in ("long", "short") and sides[k] != sides[k - 1]]

        per_symbol[sym] = {}
        for v in VARIANTS:
            tr = [r for (k, sd) in onsets if (r := sim(O, H, L, C, A, E20, k, sd, v)) is not None]
            all_trades[v].extend(tr)
            per_symbol[sym][v] = metrics(tr)
        print(f"{sym}: {len(onsets)} entries (identical across all 9 exit variants)")

    # ---------------- report ----------------
    print("\n================= EXIT COMPARISON (all symbols combined) =================")
    cols = ["trades", "ret%", "PF", "win%", "maxDD%", "avgWin_R", "avgLoss_R", "exp_R",
            "grossExp_R", "feeDrag_R", "avgHold_h", "breakeven", "fees$"]
    print(f"{'variant':22} " + " ".join(f"{c:>10}" for c in cols))
    for v in VARIANTS:
        m = metrics(all_trades[v])
        print(f"{v:22} " + " ".join(f"{str(m.get(c)):>10}" for c in cols))

    print("\n----- long vs short (combined) -----")
    for v in VARIANTS:
        print(f"{v:22} long {side_split(all_trades[v], 'long')}  | short {side_split(all_trades[v], 'short')}")

    print("\n----- per-symbol expectancy (R/trade) & PF, by variant -----")
    for v in VARIANTS:
        line = f"{v:22} "
        for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
            m = per_symbol[sym][v]
            line += f"{sym.split('/')[0]}: exp {m.get('exp_R')} PF {m.get('PF')}   "
        print(line)

    print("\n----- winner MFE/MAE (variant 9, all): how far winners run / dip -----")
    m9 = metrics(all_trades["9_V2_original"])
    print(f"avg winner reached MFE {m9['avgWinMFE_R']}R, dipped to MAE {m9['avgWinMAE_R']}R before working")


if __name__ == "__main__":
    run()
