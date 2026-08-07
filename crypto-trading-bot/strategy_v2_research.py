"""
strategy_v2_research.py — V1 vs V2 backtest comparison (BTC/ETH/SOL).

Runs BOTH strategies through ONE simulator (identical data, fees, slippage, sizing,
and intrabar "stop-first" rule) so the comparison is apples-to-apples and honest.

V1 = the original system (1H EMA50/200 trend, pullback/breakout, fixed 2R, 1.5*ATR stop).
V2 = trend-following with a regime gate + better trade management:
       4H trend (EMA50/200) + ADX>thresh + not overextended from EMA50
       + structure-break / pullback-continuation trigger,
       regime filters (low ADX, low ATR percentile, post-giant-candle),
       management: take half at 1R, stop to breakeven, ATR-trail the rest, time stop.

Outputs per symbol: V1 vs V2 metrics, monthly returns, long-only/short-only split,
and which V2 filter removed the most BAD (losing) trades. Plus a small realistic
param grid (ADX 20/25 x ATR 1.5/2.0/2.5) to check robustness — NOT a search for the
"best" numbers.

Run:  python strategy_v2_research.py
"""

from __future__ import annotations

import json
import numpy as np
import pandas as pd

from backend.config import Settings
from backend.data.fetcher import fetch_history_df
from backend.exchange import make_exchange
from backend.strategy import indicators as ind
from backend.strategy.signal import Strategy

# --------------------------------------------------------------------------- #
#  config (simple, realistic values — deliberately NOT optimized)
# --------------------------------------------------------------------------- #
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
DAYS = 180
RISK = 0.005
FEE = 0.0004
SLIP = 0.0005
MAX_LEV = 3.0
START = 10_000.0

V2 = dict(
    adx_thresh=25, stop_mult=2.0,            # primary headline values
    overext_atr=1.5,                         # skip if > 1.5 ATR from EMA50 (don't chase)
    atr_floor_q=0.25, atr_floor_win=200,     # skip quietest 25% of ATR (regime)
    giant_mult=3.0,                          # skip the bar after a >3*ATR candle
    bo_lookback=20, pullback_atr=0.75,       # structure break / pullback proximity
    partial_at=1.0, partial_frac=0.5,        # take half at 1R, move stop to breakeven
    trail_atr=2.0,                           # ATR-trail the remainder
    time_stop_bars=16, time_stop_min_r=0.5,  # cut if < 0.5R after 16 bars (4h on 15m)
)


# --------------------------------------------------------------------------- #
#  extra indicators
# --------------------------------------------------------------------------- #
def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().fillna(0.0)


# --------------------------------------------------------------------------- #
#  V2 prepared frame (15m entry + merged 4H trend/ADX + triggers + filters)
# --------------------------------------------------------------------------- #
def prepare_v2(df15: pd.DataFrame, df4h: pd.DataFrame, p: dict) -> pd.DataFrame:
    e = df15.copy()
    e["ema50"] = ind.ema(e["close"], 50)
    e["ema20"] = ind.ema(e["close"], 20)
    e["atr"] = ind.atr(e, 14)
    e["dist_ema"] = (e["close"] - e["ema50"]).abs()
    e["range"] = e["high"] - e["low"]
    # structure break (prior N-bar high/low) and pullback proximity
    e["hh"] = e["high"].rolling(p["bo_lookback"]).max().shift(1)
    e["ll"] = e["low"].rolling(p["bo_lookback"]).min().shift(1)
    # regime: ATR floor = skip when ATR below its trailing q-quantile
    e["atr_floor"] = e["atr"].shift(1).rolling(p["atr_floor_win"]).quantile(p["atr_floor_q"])
    # 4H trend (+1/-1/0) and 4H ADX, merged onto 15m (most recent CLOSED 4H bar)
    es = Settings(); es.ema_fast = 50; es.ema_slow = 200
    trend4 = ind.htf_trend(df4h, es).rename("trend4")
    adx4 = adx(df4h, 14).rename("adx4")
    h4 = pd.concat([trend4, adx4], axis=1).reset_index()
    h4.columns = ["dt", "trend4", "adx4"]
    left = e.reset_index(); left.columns = ["dt"] + list(left.columns[1:])
    m = pd.merge_asof(left.sort_values("dt"), h4.sort_values("dt"), on="dt", direction="backward")
    m = m.set_index("dt")
    m["trend4"] = m["trend4"].fillna(0).astype(int)
    m["adx4"] = m["adx4"].fillna(0.0)
    return m


def v2_filters(row, side: str, p: dict) -> dict:
    """Return pass/fail of each V2 filter for a trend-aligned bar (used for entries
    AND for the 'which filter removed bad trades' attribution)."""
    atr = row["atr"]
    if side == "long":
        trig = (row["close"] > row["hh"]) or (
            row["dist_ema"] <= p["pullback_atr"] * atr and row["close"] > row["open"]
        )
    else:
        trig = (row["close"] < row["ll"]) or (
            row["dist_ema"] <= p["pullback_atr"] * atr and row["close"] < row["open"]
        )
    return {
        "trigger": bool(trig),
        "adx": row["adx4"] >= p["adx_thresh"],
        "not_overextended": row["dist_ema"] <= p["overext_atr"] * atr,
        "atr_regime": (row["atr"] == row["atr"]) and (row["atr"] >= row["atr_floor"]),
        "not_giant": row["prev_range"] <= p["giant_mult"] * atr,
    }


def v2_signal_row(row, p: dict):
    if np.isnan(row.get("atr", np.nan)) or np.isnan(row.get("atr_floor", np.nan)):
        return 0
    for side, val in (("long", 1), ("short", -1)):
        if row["trend4"] != val:
            continue
        f = v2_filters(row, side, p)
        if all(f.values()):
            return val
    return 0


# --------------------------------------------------------------------------- #
#  one-trade simulator (returns realized R, exit index) — shared by V1 & V2
# --------------------------------------------------------------------------- #
def manage_trade(bars, i, side, mode, p):
    """bars: DataFrame with open/high/low/close/atr. risk_amount normalized to 1,
    so the returned pnl IS in R units (net of fees+slippage). Intrabar = stop-first."""
    o = bars["open"].values; h = bars["high"].values; l = bars["low"].values
    c = bars["close"].values; a = bars["atr"].values
    n = len(c)
    entry = c[i] * (1 + SLIP) if side == "long" else c[i] * (1 - SLIP)
    sd = a[i] * (p["stop_mult"] if mode == "v2" else 1.5)
    if sd <= 0 or np.isnan(sd):
        return 0.0, i + 1
    qty = 1.0 / sd                         # risk_amount = 1  -> pnl in R
    fee_u = lambda px, q: px * q * FEE
    realized = -fee_u(entry, qty)          # entry fee
    long = side == "long"
    stop = entry - sd if long else entry + sd
    tp2 = entry + 2 * sd if long else entry - sd * 2
    target1 = entry + sd if long else entry - sd
    best = entry
    remaining = qty
    partial_done = False

    for j in range(i + 1, n):
        hi, lo, cl, atrj = h[j], l[j], c[j], a[j]
        best = max(best, hi) if long else min(best, lo)
        mfe_r = (best - entry) / sd if long else (entry - best) / sd

        if mode == "v1":
            if (long and lo <= stop) or (not long and hi >= stop):
                fill = stop * (1 - SLIP) if long else stop * (1 + SLIP)
                realized += remaining * ((fill - entry) if long else (entry - fill)) - fee_u(fill, remaining)
                return realized, j
            if (long and hi >= tp2) or (not long and lo <= tp2):
                fill = tp2 * (1 - SLIP) if long else tp2 * (1 + SLIP)
                realized += remaining * ((fill - entry) if long else (entry - fill)) - fee_u(fill, remaining)
                return realized, j
            continue

        # ---- V2 management ----
        if not partial_done:
            if (long and lo <= stop) or (not long and hi >= stop):     # full stop (stop-first)
                fill = stop * (1 - SLIP) if long else stop * (1 + SLIP)
                realized += remaining * ((fill - entry) if long else (entry - fill)) - fee_u(fill, remaining)
                return realized, j
            if (long and hi >= target1) or (not long and lo <= target1):  # take half at 1R
                half = remaining * p["partial_frac"]
                fill = target1 * (1 - SLIP) if long else target1 * (1 + SLIP)
                realized += half * ((fill - entry) if long else (entry - fill)) - fee_u(fill, half)
                remaining -= half
                stop = entry                                            # move to breakeven
                partial_done = True
        else:
            trail = (best - p["trail_atr"] * atrj) if long else (best + p["trail_atr"] * atrj)
            stop = max(stop, trail) if long else min(stop, trail)
            if (long and lo <= stop) or (not long and hi >= stop):
                fill = stop * (1 - SLIP) if long else stop * (1 + SLIP)
                realized += remaining * ((fill - entry) if long else (entry - fill)) - fee_u(fill, remaining)
                return realized, j

        if (j - i) >= p["time_stop_bars"] and mfe_r < p["time_stop_min_r"]:  # time stop
            fill = cl * (1 - SLIP) if long else cl * (1 + SLIP)
            realized += remaining * ((fill - entry) if long else (entry - fill)) - fee_u(fill, remaining)
            return realized, j

    fill = c[-1] * (1 - SLIP) if long else c[-1] * (1 + SLIP)            # close at end of data
    realized += remaining * ((fill - entry) if long else (entry - fill)) - fee_u(fill, remaining)
    return realized, n - 1


# --------------------------------------------------------------------------- #
#  sequential backtest (one position at a time) -> trades + equity
# --------------------------------------------------------------------------- #
def backtest(bars, signal, mode, p):
    equity = START
    trades, eq_pts = [], [(bars.index[0], equity)]
    i, n = 1, len(bars)
    sig = signal.values
    while i < n - 1:
        s = sig[i]
        if s == 0:
            i += 1
            continue
        side = "long" if s > 0 else "short"
        r, jx = manage_trade(bars, i, side, mode, p)
        risk_amount = min(equity * RISK, equity * MAX_LEV * (bars["atr"].values[i] * (p["stop_mult"] if mode == "v2" else 1.5)) / bars["close"].values[i])
        pnl = r * (equity * RISK)
        equity += pnl
        trades.append({
            "side": side, "exit_time": bars.index[jx], "pnl": pnl,
            "r": r, "entry_time": bars.index[i],
        })
        eq_pts.append((bars.index[jx], equity))
        i = jx + 1
    return trades, eq_pts


# --------------------------------------------------------------------------- #
#  metrics / breakdowns
# --------------------------------------------------------------------------- #
def metrics(trades, eq_pts):
    if not trades:
        return {"trades": 0}
    pnls = np.array([t["pnl"] for t in trades])
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    eq = pd.Series([e for _, e in eq_pts], index=pd.to_datetime([t for t, _ in eq_pts], utc=True)).sort_index()
    daily = eq.resample("1D").last().ffill()
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {
        "trades": len(trades),
        "return_pct": round((eq.iloc[-1] / START - 1) * 100, 2),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if losses.sum() < 0 else None,
        "max_dd_pct": round(float(dd) * 100, 2),
        "sharpe": round(sharpe, 2),
        "avg_R": round(float(np.mean([t["r"] for t in trades])), 3),
    }


def split_side(trades, eq_pts, side):
    sub = [t for t in trades if t["side"] == side]
    if not sub:
        return {"trades": 0}
    pnl = sum(t["pnl"] for t in sub)
    wins = sum(1 for t in sub if t["pnl"] > 0)
    gp = sum(t["pnl"] for t in sub if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in sub if t["pnl"] < 0)
    return {"trades": len(sub), "pnl": round(pnl, 0), "win_rate": round(wins / len(sub) * 100, 1),
            "profit_factor": round(gp / gl, 3) if gl > 0 else None}


def monthly(trades):
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    df["m"] = pd.to_datetime(df["exit_time"], utc=True).dt.strftime("%Y-%m")
    return {m: round(g["pnl"].sum() / START * 100, 2) for m, g in df.groupby("m")}


def filter_attribution(bars, p):
    """For every trend-aligned bar whose trigger fires (a 'candidate'), check which
    regime filters it fails, and whether taking it (V2 management) would LOSE.
    Reports per-filter: bad (losing) trades removed vs good (winning) trades removed."""
    names = ["adx", "not_overextended", "atr_regime", "not_giant"]
    bad = {k: 0 for k in names}
    good = {k: 0 for k in names}
    rows = bars
    idx = np.arange(len(rows))
    trend = rows["trend4"].values
    for i in idx[200:-1]:
        tv = trend[i]
        if tv == 0:
            continue
        side = "long" if tv > 0 else "short"
        row = rows.iloc[i]
        if np.isnan(row["atr"]) or np.isnan(row["atr_floor"]):
            continue
        f = v2_filters(row, side, p)
        if not f["trigger"]:
            continue
        fails = [k for k in names if not f[k]]
        if not fails:
            continue                       # this candidate is actually taken by V2
        r, _ = manage_trade(rows, i, side, "v2", p)   # would it have won or lost?
        for k in fails:
            (bad if r < 0 else good)[k] += 1
    return {k: {"bad_removed": bad[k], "good_removed": good[k], "net": bad[k] - good[k]} for k in names}


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def run():
    ex = make_exchange(Settings())
    out = {}
    for sym in SYMBOLS:
        print(f"\n########## {sym} ##########")
        df15 = fetch_history_df(ex, sym, "15m", DAYS)
        df1h = fetch_history_df(ex, sym, "1h", DAYS)
        df4h = fetch_history_df(ex, sym, "4h", DAYS)

        # ----- V1 signals via the existing Strategy (1H trend, pullback) -----
        s1 = Settings()
        v1_prepared = Strategy(s1).prepare(df1h, df15)
        v1_prepared = v1_prepared.dropna(subset=["ema_slow", "atr", "vol_ma"])
        strat = Strategy(s1)
        v1_sig = pd.Series(0, index=v1_prepared.index)
        for ts, row in v1_prepared.iterrows():
            sg = strat.evaluate_row(row, symbol=sym)
            if sg:
                v1_sig.loc[ts] = 1 if sg.side == "long" else -1
        v1_bars = v1_prepared.rename(columns={})  # has open/high/low/close/atr
        t1, e1 = backtest(v1_bars, v1_sig, "v1", V2)

        # ----- V2 -----
        m2 = prepare_v2(df15, df4h, V2)
        m2["prev_range"] = m2["range"].shift(1)
        m2 = m2.dropna(subset=["atr", "ema50"])
        v2_sig = pd.Series([v2_signal_row(r, V2) for _, r in m2.iterrows()], index=m2.index)
        t2, e2 = backtest(m2, v2_sig, "v2", V2)

        out[sym] = {
            "V1": metrics(t1, e1), "V2": metrics(t2, e2),
            "V1_long": split_side(t1, e1, "long"), "V1_short": split_side(t1, e1, "short"),
            "V2_long": split_side(t2, e2, "long"), "V2_short": split_side(t2, e2, "short"),
            "V1_monthly": monthly(t1), "V2_monthly": monthly(t2),
            "filter_attribution": filter_attribution(m2, V2),
        }
        print("V1:", json.dumps(out[sym]["V1"]))
        print("V2:", json.dumps(out[sym]["V2"]))

        # ----- small realistic param grid (robustness, NOT cherry-picking) -----
        grid = {}
        for adx_t in (20, 25):
            for stop in (1.5, 2.0, 2.5):
                p = dict(V2); p["adx_thresh"] = adx_t; p["stop_mult"] = stop
                mg = prepare_v2(df15, df4h, p); mg["prev_range"] = mg["range"].shift(1)
                mg = mg.dropna(subset=["atr", "ema50"])
                sg = pd.Series([v2_signal_row(r, p) for _, r in mg.iterrows()], index=mg.index)
                tt, ee = backtest(mg, sg, "v2", p)
                grid[f"adx{adx_t}_stop{stop}"] = metrics(tt, ee)
        out[sym]["V2_grid"] = grid

    with open("results_v1_vs_v2.json", "w") as f:
        json.dump(out, f, indent=2, default=str)

    _print_report(out)
    return out


def _print_report(out):
    print("\n\n==================== V1 vs V2 SUMMARY ====================")
    hdr = f"{'symbol':9} {'ver':3} {'trades':>6} {'ret%':>8} {'win%':>6} {'PF':>6} {'maxDD%':>8} {'sharpe':>7} {'avgR':>6}"
    print(hdr); print("-" * len(hdr))
    for sym in SYMBOLS:
        for v in ("V1", "V2"):
            m = out[sym][v]
            if m.get("trades"):
                print(f"{sym:9} {v:3} {m['trades']:6} {m['return_pct']:8} {m['win_rate']:6} "
                      f"{str(m['profit_factor']):>6} {m['max_dd_pct']:8} {m['sharpe']:7} {m['avg_R']:6}")
    print("\n----- long vs short (V2) -----")
    for sym in SYMBOLS:
        print(f"{sym}: long {out[sym]['V2_long']}  |  short {out[sym]['V2_short']}")
    print("\n----- monthly return % (V2) -----")
    for sym in SYMBOLS:
        print(f"{sym}: {out[sym]['V2_monthly']}")
    print("\n----- which filter removed the most BAD trades (V2) -----")
    for sym in SYMBOLS:
        fa = out[sym]["filter_attribution"]
        ranked = sorted(fa.items(), key=lambda kv: kv[1]["net"], reverse=True)
        print(f"{sym}: " + " | ".join(f"{k}: -{v['bad_removed']}bad/-{v['good_removed']}good(net+{v['net']})" for k, v in ranked))
    print("\n----- V2 param grid (robustness check) -----")
    for sym in SYMBOLS:
        g = out[sym]["V2_grid"]
        print(f"{sym}:")
        for k, m in g.items():
            if m.get("trades"):
                print(f"   {k:14} trades {m['trades']:4} ret {m['return_pct']:7}%  PF {m['profit_factor']}  win {m['win_rate']}%  DD {m['max_dd_pct']}%")


if __name__ == "__main__":
    run()
