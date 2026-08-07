"""
ict_apex.py — an ICT futures strategy (ES 5m) tested against real Apex $50k eval rules.

STRATEGY (ICT 2022 model on ES, NY killzone):
  * NY AM/PM killzone only; flat by 15:55 ET; <=3 trades/day; daily loss stop.
  * Liquidity sweep of a pool (prior-day H/L or an intraday fractal swing).
  * Market-structure shift (MSS): a bar CLOSES back through the most recent opposing
    swing after the sweep (displacement).
  * Entry at the fair-value gap (FVG midpoint) left by the displacement leg.
  * Stop beyond the sweep extreme; target 2R (partial 1R + breakeven trail optional).
  * Fixed $ risk/trade -> integer ES contracts ($50/pt). Slippage + commission included.

APEX $50k eval rules tested: reach +$3,000 (PASS) before equity falls $2,500 below its
trailing peak (FAIL), within 20 trading days, >=1 trade. Reported as a PASS RATE over
every possible 1-month start (rolling) and per calendar month — one lucky month proves
nothing.
"""
from __future__ import annotations
import os, sys
from collections import Counter
from datetime import time as dtime
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CACHE = os.path.join(ROOT, "research", "ta_strat", "cache")
NY = "America/New_York"

# ---- futures + Apex ----
PT = 50.0                  # ES $ per point
TICK = 0.25
SLIP_TICKS = 1.0
COMMISSION_RT = 4.0        # $ per contract round trip
START_BAL = 50_000.0
APEX_TARGET = 3_000.0
APEX_TRAIL_DD = 2_500.0
EVAL_DAYS = 20
# ---- strategy params (ICT convention + Apex risk math, not fit to the data) ----
RISK_USD = 300.0
MAX_CONTRACTS = 5
KZ = [(dtime(9, 30), dtime(12, 0)), (dtime(13, 30), dtime(15, 0))]   # NY AM + early PM
FLAT = dtime(15, 55)
SWING_W = 2
SWEEP_LOOKBACK = 40        # bars to look back for the pool being swept
MSS_WINDOW = 12            # bars after sweep to get the MSS
FVG_MIN_PTS = 0.75
STOP_BUF_TICKS = 2
TARGET_R = 2.0
TREND_EMA = 100            # 5m EMA (~8h) HTF trend filter: longs above, shorts below
MAX_TRADES_DAY = 3
DAILY_LOSS_STOP = -600.0
SETUP_EXPIRY = 16          # bars after MSS for the limit to fill


def load_es(years=1):
    df = pd.read_csv(os.path.join(CACHE, "apex3yr_es.csv"))
    df["dt"] = pd.to_datetime(df["dt_utc"], utc=True).dt.tz_convert(NY)
    df = df.sort_values("dt").reset_index(drop=True)
    df["date"] = df["dt"].dt.date
    df["t"] = df["dt"].dt.time
    # RTH only
    df = df[(df["t"] >= dtime(9, 30)) & (df["t"] <= dtime(16, 0))].reset_index(drop=True)
    # higher-timeframe trend filter (continuous EMA on the 5m close ~ 8h) — ICT "trade toward
    # the draw on liquidity": only longs above it, shorts below it. Computed before slicing.
    df["ema"] = df["close"].ewm(span=TREND_EMA, adjust=False).mean()
    if years:
        cutoff = df["dt"].iloc[-1] - pd.Timedelta(days=365 * years)
        df = df[df["dt"] >= cutoff].reset_index(drop=True)
    return df


def in_kz(t):
    return any(a <= t <= b for a, b in KZ)


def swings(highs, lows, w=SWING_W):
    n = len(highs); sh = np.zeros(n, bool); sl = np.zeros(n, bool)
    for i in range(w, n - w):
        if highs[i] == max(highs[i - w:i + w + 1]) and highs[i] > highs[i - 1]:
            sh[i] = True
        if lows[i] == min(lows[i - w:i + w + 1]) and lows[i] < lows[i - 1]:
            sl[i] = True
    return sh, sl


def backtest(df):
    trades = []
    pdh = pdl = None
    for date, g in df.groupby("date", sort=True):
        g = g.reset_index(drop=True)
        o, h, l, c = g["open"].values, g["high"].values, g["low"].values, g["close"].values
        ema = g["ema"].values
        tt = g["t"].values; dts = g["dt"].values
        sh, sl = swings(h, l)
        # liquidity pools available at bar i: PDH/PDL + intraday swings formed before i
        n = len(g)
        n_trades_day = 0; day_pnl = 0.0
        pos = None; setup = None; i = 0
        while i < n:
            # ---- manage an open position ----
            if pos is not None:
                side = pos["side"]; hi, lo = h[i], l[i]
                # MAE/MFE in $
                pos["mae"] = min(pos["mae"], side * (lo - pos["entry"]) * PT * pos["qty"] if side > 0 else side * (hi - pos["entry"]) * PT * pos["qty"])
                pos["mfe"] = max(pos["mfe"], side * (hi - pos["entry"]) * PT * pos["qty"] if side > 0 else side * (lo - pos["entry"]) * PT * pos["qty"])
                exit_px = reason = None
                if side > 0:
                    if lo <= pos["stop"]: exit_px, reason = pos["stop"], "stop"
                    elif hi >= pos["target"]: exit_px, reason = pos["target"], "target"
                else:
                    if hi >= pos["stop"]: exit_px, reason = pos["stop"], "stop"
                    elif lo <= pos["target"]: exit_px, reason = pos["target"], "target"
                if reason is None and tt[i] >= FLAT:
                    exit_px, reason = c[i], "eod"
                if reason:
                    fill = exit_px - np.sign(pos["side"]) * TICK * SLIP_TICKS
                    pnl = pos["side"] * (fill - pos["entry"]) * PT * pos["qty"] - COMMISSION_RT * pos["qty"]
                    day_pnl += pnl
                    trades.append({"date": date, "entry_dt": pos["entry_dt"], "exit_dt": dts[i],
                                   "side": "long" if pos["side"] > 0 else "short", "qty": pos["qty"],
                                   "entry": round(pos["entry"], 2), "exit": round(fill, 2), "pnl": pnl,
                                   "mae": pos["mae"], "mfe": pos["mfe"], "reason": reason})
                    pos = None
                    if day_pnl <= DAILY_LOSS_STOP or n_trades_day >= MAX_TRADES_DAY:
                        break
                i += 1; continue

            # ---- try to fill a pending setup ----
            if setup is not None:
                if i - setup["mss_i"] > SETUP_EXPIRY:
                    setup = None; continue
                s = setup["side"]; ent = setup["entry"]
                touched = (l[i] <= ent) if s > 0 else (h[i] >= ent)
                # invalidate if stop breached before fill
                blown = (l[i] <= setup["stop"]) if s > 0 else (h[i] >= setup["stop"])
                if blown and not touched:
                    setup = None; continue
                if touched and in_kz(tt[i]) and n_trades_day < MAX_TRADES_DAY and day_pnl > DAILY_LOSS_STOP:
                    stop_pts = abs(ent - setup["stop"])
                    qty = int(np.clip(np.floor(RISK_USD / (stop_pts * PT)), 1, MAX_CONTRACTS))
                    entry_px = ent + np.sign(s) * TICK * SLIP_TICKS
                    pos = {"side": s, "entry": entry_px, "qty": qty, "stop": setup["stop"],
                           "target": entry_px + s * TARGET_R * stop_pts, "entry_dt": dts[i],
                           "mae": 0.0, "mfe": 0.0}
                    n_trades_day += 1; setup = None
                    i += 1; continue
                i += 1; continue

            # ---- scan for sweep -> MSS -> FVG (only in killzone, when flat) ----
            if in_kz(tt[i]) and n_trades_day < MAX_TRADES_DAY and day_pnl > DAILY_LOSS_STOP and i >= 6:
                # recent intraday pools + prior-day H/L
                rh_idx = [j for j in range(max(0, i - SWEEP_LOOKBACK), i) if sh[j]]
                rl_idx = [j for j in range(max(0, i - SWEEP_LOOKBACK), i) if sl[j]]
                pool_highs = [h[j] for j in rh_idx] + ([pdh] if pdh else [])
                pool_lows = [l[j] for j in rl_idx] + ([pdl] if pdl else [])
                # SHORT: sweep a high then MSS down
                swept_hi = max([x for x in pool_highs if h[i] > x + TICK], default=None)
                swept_lo = min([x for x in pool_lows if l[i] < x - TICK], default=None)
                made = False
                if swept_hi is not None and rl_idx and c[i] < ema[i]:    # short only WITH downtrend
                    # MSS down within window: close below most-recent swing low formed before sweep
                    ref_low = l[rl_idx[-1]]
                    for k in range(i, min(n, i + MSS_WINDOW)):
                        if c[k] < ref_low:
                            # FVG (bearish) in i..k: bar a-1 low > bar a+1 high
                            fvg = None
                            for a in range(i, k):
                                if a + 1 < n and l[a - 1] - h[a + 1] >= FVG_MIN_PTS:
                                    fvg = (l[a - 1] + h[a + 1]) / 2.0; break
                            if fvg is not None:
                                stop = h[i:k + 1].max() + STOP_BUF_TICKS * TICK
                                if stop > fvg + 0.5:
                                    setup = {"side": -1, "entry": fvg, "stop": stop, "mss_i": k}
                                    i = k + 1; made = True
                            break
                if not made and swept_lo is not None and rh_idx and c[i] > ema[i]:    # long only WITH uptrend
                    ref_high = h[rh_idx[-1]]
                    for k in range(i, min(n, i + MSS_WINDOW)):
                        if c[k] > ref_high:
                            fvg = None
                            for a in range(i, k):
                                if a + 1 < n and l[a + 1] - h[a - 1] >= FVG_MIN_PTS:
                                    fvg = (h[a - 1] + l[a + 1]) / 2.0; break
                            if fvg is not None:
                                stop = l[i:k + 1].min() - STOP_BUF_TICKS * TICK
                                if fvg - stop > 0.5:
                                    setup = {"side": 1, "entry": fvg, "stop": stop, "mss_i": k}
                                    i = k + 1; made = True
                            break
                if made:
                    continue
            i += 1
        # update prior-day H/L for next day
        pdh, pdl = float(h.max()), float(l.min())
    return pd.DataFrame(trades)


def apex_eval(trades, all_dates):
    """For each possible 1-month start (every trading day), simulate a fresh $50k eval and
    return PASS/FAIL. PASS = +$3000 reached before -$2500 trailing DD, within 20 trading days."""
    if trades.empty:
        return None
    tdays = sorted(all_dates)
    day_idx = {d: k for k, d in enumerate(tdays)}
    tr = trades.copy()
    tr["di"] = tr["date"].map(day_idx)
    results = []
    for s in range(0, len(tdays)):
        end = s + EVAL_DAYS
        win = tr[(tr["di"] >= s) & (tr["di"] < end)]
        if win.empty:
            continue
        eq = START_BAL; peak = START_BAL; outcome = "none"
        for _, t in win.iterrows():
            # trailing peak tracks the intraday HIGH-water mark; DD checked vs intraday LOW;
            # the profit TARGET is counted on realized balance (Apex counts closed balance).
            peak = max(peak, eq + t["mfe"])
            if eq + t["mae"] <= peak - APEX_TRAIL_DD:
                outcome = "fail"; break
            eq += t["pnl"]; peak = max(peak, eq)
            if eq >= START_BAL + APEX_TARGET:
                outcome = "pass"; break
            if eq <= peak - APEX_TRAIL_DD:
                outcome = "fail"; break
        results.append({"start": tdays[s], "outcome": outcome, "n_trades": len(win)})
    return pd.DataFrame(results)


def main():
    years = float(sys.argv[1]) if len(sys.argv) > 1 else 1
    df = load_es(years=years)
    print(f"[data] ES 5m RTH: {len(df):,} bars  {df['dt'].iloc[0].date()} -> {df['dt'].iloc[-1].date()} "
          f"({df['date'].nunique()} trading days, ~{years}y)", flush=True)
    trades = backtest(df)
    print("\n" + "=" * 78)
    print("ICT ES futures strategy — backtest + Apex $50k eval")
    print("=" * 78)
    if trades.empty:
        print("NO TRADES."); return
    pnl = trades["pnl"].values
    wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() else float("inf")
    print(f"trades           : {len(trades)}  ({len(trades)/df['date'].nunique():.2f}/day)")
    print(f"win rate         : {(pnl>0).mean()*100:.1f}%")
    print(f"net P&L          : ${pnl.sum():+,.0f}   (avg ${pnl.mean():+.0f}/trade)")
    print(f"profit factor    : {pf:.2f}")
    print(f"exit reasons     : {dict(Counter(trades['reason']))}")
    eq = START_BAL + np.cumsum(pnl); peak = np.maximum.accumulate(np.concatenate([[START_BAL], eq]))
    print(f"max drawdown     : ${(np.concatenate([[START_BAL],eq])-peak).min():,.0f}")

    res = apex_eval(trades, df["date"].unique())
    real = res[res["outcome"] != "none"]
    npass = (real["outcome"] == "pass").sum(); nfail = (real["outcome"] == "fail").sum()
    nn = len(real)
    print(f"\nAPEX $50k EVAL (+${APEX_TARGET:.0f} before -${APEX_TRAIL_DD:.0f} trailing DD, in {EVAL_DAYS}d):")
    print(f"  rolling 1-month windows tested : {nn}")
    print(f"  PASS rate : {npass/nn*100:.0f}%   ({npass} pass / {nfail} fail / "
          f"{(real['outcome']=='none').sum()} neither)")
    # per calendar month
    print(f"\n  per calendar month (start-of-month eval):")
    tr = trades.copy(); tr["ym"] = pd.to_datetime(tr["entry_dt"]).dt.to_period("M")
    for ym, g in tr.groupby("ym"):
        e = START_BAL; pk = START_BAL; out = "no target"
        for _, t in g.iterrows():
            pk = max(pk, e + t["mfe"])
            if e + t["mae"] <= pk - APEX_TRAIL_DD: out = "FAIL (DD)"; break
            e += t["pnl"]; pk = max(pk, e)
            if e >= START_BAL + APEX_TARGET: out = "PASS"; break
            if e <= pk - APEX_TRAIL_DD: out = "FAIL (DD)"; break
        print(f"    {ym}: {len(g):>2} trades, month P&L ${g['pnl'].sum():+6.0f}  -> {out}")
    trades.to_csv(os.path.join(CACHE, "ict_apex_trades.csv"), index=False)


if __name__ == "__main__":
    main()
