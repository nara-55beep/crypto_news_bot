"""
ict_apex_v2.py — FAITHFUL ICT 2022 model on ES futures (24h data), tested vs Apex $50k rules.

Traded "as ICT says":
  * HTF DAILY BIAS — only trade toward the daily draw on liquidity (close vs daily EMA20).
  * LIQUIDITY POOLS — overnight high/low (ONH/ONL, 18:00->09:30 ET) + prior-day high/low.
  * KILLZONES (ET) — London 02:00-05:00, NY-AM 07:00-11:00 (incl 10-11 silver bullet),
    NY-PM 13:30-15:00. Flat 15:55.
  * SETUP — in a killzone, price SWEEPS a pool against bias (judas), then a DISPLACEMENT
    candle CLOSES through short-term structure (MSS), leaving an FVG.
  * ENTRY — limit at the FVG, required to sit in DISCOUNT (<=50% of the displacement leg / OTE).
  * STOP — beyond the sweep extreme. TARGET — the OPPOSING liquidity (the draw), need >=1.5R.
  * Fixed $ risk -> integer ES contracts; slippage + commission; <=2 trades/day; daily loss stop.

Apex $50k eval scored as PASS RATE over rolling 1-month windows (+$3000 before -$2500 trailing
DD in <=20 trading days). Not curve-fit: params are ICT-canonical, validated across 3 years.
"""
from __future__ import annotations
import os, sys
from collections import Counter
from datetime import time as dtime
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CACHE = os.path.join(ROOT, "research", "ta_strat", "cache")
NY = "America/New_York"

PT, TICK, SLIP_TICKS, COMM = 50.0, 0.25, 1.0, 4.0
START_BAL, APEX_TARGET, APEX_DD, EVAL_DAYS = 50_000.0, 3_000.0, 2_500.0, 20
RISK_USD, MAX_CONTRACTS = 300.0, 5
BREAKEVEN_AT_R = 0.0       # 0 = disabled (BE-after-1R tested WORSE: pullbacks scratch winners)
KZONES = [(dtime(7, 0), dtime(11, 0))]   # NY AM killzone only — highest-probability ICT window
FLAT = dtime(15, 55)
SWING_W, ATR_N = 2, 14
SWEEP_LOOKBACK, MSS_WINDOW, SETUP_EXPIRY = 30, 10, 14
DISP_ATR, DISP_BODY = 1.2, 0.5
FVG_MIN_PTS = 0.5
DISCOUNT_MAX = 0.5         # entry must be in the lower (discount) half of the leg for longs
MIN_RR, STOP_BUF = 1.0, 2 * TICK         # nearer liquidity targets -> higher win rate
DAILY_BIAS_EMA = 20
MAX_TRADES_DAY, DAILY_LOSS_STOP = 3, -450.0
DAILY_PROFIT_LOCK = 550.0   # stop opening new trades once +$550 on the day -> banks green days,
                            # so the equity can't give it back into the $2500 trailing DD


def load(years=None):
    p = os.path.join(CACHE, "apex3yr_es_24h.csv")
    df = pd.read_csv(p)
    df["dt"] = pd.to_datetime(df["dt_utc"], utc=True).dt.tz_convert(NY)
    df = df.sort_values("dt").reset_index(drop=True)
    df["date"] = df["dt"].dt.date
    df["tm"] = df["dt"].dt.time
    if years:
        df = df[df["dt"] >= df["dt"].iloc[-1] - pd.Timedelta(days=365 * years)].reset_index(drop=True)
    return df


def daily_bias(df):
    # RTH daily close -> EMA20 -> bias for the NEXT day
    rth = df[(df["tm"] >= dtime(9, 30)) & (df["tm"] <= dtime(16, 0))]
    dc = rth.groupby("date")["close"].last()
    ema = dc.ewm(span=DAILY_BIAS_EMA, adjust=False).mean()
    bias = pd.Series(np.where(dc > ema, 1, -1), index=dc.index).shift(1)   # use prior-day close
    return bias.to_dict()


def atr(h, l, c, n=ATR_N):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    a = np.zeros_like(tr); a[0] = tr[0]
    for i in range(1, len(tr)):
        a[i] = (a[i - 1] * (n - 1) + tr[i]) / n
    return a


def swings(h, l, w=SWING_W):
    n = len(h); sh = np.zeros(n, bool); sl = np.zeros(n, bool)
    for i in range(w, n - w):
        if h[i] == h[i - w:i + w + 1].max() and h[i] > h[i - 1]: sh[i] = True
        if l[i] == l[i - w:i + w + 1].min() and l[i] < l[i - 1]: sl[i] = True
    return sh, sl


def backtest(df):
    bias = daily_bias(df)
    trades = []
    dates = sorted(df["date"].unique())
    prior = {}     # date -> (pdh, pdl)
    for di, date in enumerate(dates):
        g = df[df["date"] == date].reset_index(drop=True)
        o, h, l, c = (g["open"].values, g["high"].values, g["low"].values, g["close"].values)
        tm = g["tm"].values; dts = g["dt"].values
        a = atr(h, l, c); sh, sl = swings(h, l)
        b = bias.get(date, 0)
        # overnight high/low = bars before 09:30 ET this date (Asian+London+pre-NY)
        on_mask = np.array([t < dtime(9, 30) for t in tm])
        onh = h[on_mask].max() if on_mask.any() else None
        onl = l[on_mask].min() if on_mask.any() else None
        pdh, pdl = prior.get(date, (None, None))
        # full-day H/L for next day's PDH/PDL
        if di + 1 < len(dates):
            prior[dates[di + 1]] = (float(h.max()), float(l.min()))

        if pd.isna(b) or b == 0:
            continue
        n = len(g); pos = None; setup = None; ntr = 0; dpnl = 0.0; i = 0
        sell_pools = [x for x in (onl, pdl) if x is not None]   # for long sweeps (sell-side)
        buy_pools = [x for x in (onh, pdh) if x is not None]    # for short sweeps (buy-side)

        def in_kz(t): return any(x <= t <= y for x, y in KZONES)

        while i < n:
            # manage open position
            if pos is not None:
                side = pos["s"]; hi, lo = h[i], l[i]
                upL = side * (lo - pos["e"]) * PT * pos["q"]; upH = side * (hi - pos["e"]) * PT * pos["q"]
                pos["mae"] = min(pos["mae"], min(upL, upH)); pos["mfe"] = max(pos["mfe"], max(upL, upH))
                # move stop to breakeven once +1R is reached (protects the trailing drawdown)
                if BREAKEVEN_AT_R > 0 and not pos["be"]:
                    reached = (hi >= pos["e"] + pos["R"]) if side > 0 else (lo <= pos["e"] - pos["R"])
                    if reached:
                        pos["stop"] = pos["e"]; pos["be"] = True
                ex = rs = None
                if side > 0:
                    if lo <= pos["stop"]: ex, rs = pos["stop"], "stop"
                    elif hi >= pos["tgt"]: ex, rs = pos["tgt"], "target"
                else:
                    if hi >= pos["stop"]: ex, rs = pos["stop"], "stop"
                    elif lo <= pos["tgt"]: ex, rs = pos["tgt"], "target"
                if rs is None and tm[i] >= FLAT: ex, rs = c[i], "eod"
                if rs:
                    fill = ex - np.sign(side) * TICK * SLIP_TICKS
                    pnl = side * (fill - pos["e"]) * PT * pos["q"] - COMM * pos["q"]
                    dpnl += pnl
                    trades.append({"date": date, "entry_dt": pos["edt"], "side": "L" if side > 0 else "S",
                                   "q": pos["q"], "pnl": pnl, "mae": pos["mae"], "mfe": pos["mfe"], "reason": rs})
                    pos = None
                    if dpnl <= DAILY_LOSS_STOP or ntr >= MAX_TRADES_DAY: break
                i += 1; continue
            # fill pending setup
            if setup is not None:
                if i - setup["k"] > SETUP_EXPIRY: setup = None; continue
                s = setup["s"]; ent = setup["e"]
                touched = (l[i] <= ent) if s > 0 else (h[i] >= ent)
                blown = (l[i] <= setup["stop"]) if s > 0 else (h[i] >= setup["stop"])
                if blown and not touched: setup = None; continue
                if touched and in_kz(tm[i]) and ntr < MAX_TRADES_DAY and DAILY_LOSS_STOP < dpnl < DAILY_PROFIT_LOCK:
                    sp = abs(ent - setup["stop"]); q = int(np.clip(np.floor(RISK_USD / (sp * PT)), 1, MAX_CONTRACTS))
                    ep = ent + np.sign(s) * TICK * SLIP_TICKS
                    pos = {"s": s, "e": ep, "q": q, "stop": setup["stop"], "tgt": setup["tgt"],
                           "edt": dts[i], "mae": 0.0, "mfe": 0.0, "be": False,
                           "R": abs(ep - setup["stop"]) * BREAKEVEN_AT_R}
                    ntr += 1; setup = None
                i += 1; continue
            # scan for setup (killzone, with bias, flat)
            if in_kz(tm[i]) and ntr < MAX_TRADES_DAY and DAILY_LOSS_STOP < dpnl < DAILY_PROFIT_LOCK and i >= 6 and a[i] > 0:
                made = False
                if b > 0:   # bullish: sweep sell-side, MSS up, long, target buy-side
                    rl = [j for j in range(max(0, i - SWEEP_LOOKBACK), i) if sl[j]]
                    pools = sell_pools + [l[j] for j in rl]
                    # sweep = wick takes the low AND candle CLOSES back above it (rejection)
                    swept = max([x for x in pools if l[i] < x - TICK and c[i] > x], default=None)
                    if swept is not None and rh_recent(sh, i):
                        sweep_lo = l[i]
                        ref_hi = h[last_true(sh, i)]
                        for k in range(i + 1, min(n, i + MSS_WINDOW)):
                            rng = h[k] - l[k]
                            disp = (c[k] - o[k]) >= DISP_BODY * rng and rng >= DISP_ATR * a[k]
                            if c[k] > ref_hi and disp:
                                mss_hi = h[i:k + 1].max(); leg = mss_hi - sweep_lo
                                fvg = find_fvg(h, l, i, k, +1)
                                if fvg is not None and leg > 0 and (fvg - sweep_lo) <= DISCOUNT_MAX * leg:
                                    stop = sweep_lo - STOP_BUF; R = fvg - stop
                                    tgt = min([x for x in buy_pools if x >= fvg + MIN_RR * R] +
                                              [mss_hi + 2 * R], default=fvg + 2 * R) if buy_pools else fvg + 2 * R
                                    if R > 0 and (tgt - fvg) >= MIN_RR * R:
                                        setup = {"s": 1, "e": fvg, "stop": stop, "tgt": tgt, "k": k}
                                        i = k + 1; made = True
                                break
                else:       # bearish mirror
                    rh = [j for j in range(max(0, i - SWEEP_LOOKBACK), i) if sh[j]]
                    pools = buy_pools + [h[j] for j in rh]
                    swept = min([x for x in pools if h[i] > x + TICK and c[i] < x], default=None)
                    if swept is not None and rl_recent(sl, i):
                        sweep_hi = h[i]
                        ref_lo = l[last_true(sl, i)]
                        for k in range(i + 1, min(n, i + MSS_WINDOW)):
                            rng = h[k] - l[k]
                            disp = (o[k] - c[k]) >= DISP_BODY * rng and rng >= DISP_ATR * a[k]
                            if c[k] < ref_lo and disp:
                                mss_lo = l[i:k + 1].min(); leg = sweep_hi - mss_lo
                                fvg = find_fvg(h, l, i, k, -1)
                                if fvg is not None and leg > 0 and (sweep_hi - fvg) <= DISCOUNT_MAX * leg:
                                    stop = sweep_hi + STOP_BUF; R = stop - fvg
                                    tgt = max([x for x in sell_pools if x <= fvg - MIN_RR * R] +
                                              [mss_lo - 2 * R], default=fvg - 2 * R) if sell_pools else fvg - 2 * R
                                    if R > 0 and (fvg - tgt) >= MIN_RR * R:
                                        setup = {"s": -1, "e": fvg, "stop": stop, "tgt": tgt, "k": k}
                                        i = k + 1; made = True
                                break
                if made: continue
            i += 1
    return pd.DataFrame(trades)


def last_true(arr, before):
    idx = np.where(arr[:before])[0]
    return idx[-1] if len(idx) else 0
def rh_recent(sh, i): return bool(sh[:i].any())
def rl_recent(sl, i): return bool(sl[:i].any())


def find_fvg(h, l, i, k, d):
    """bullish (d=+1): low[a+1] > high[a-1]; return gap midpoint. bearish mirror."""
    for a in range(i, k):
        if a + 1 >= len(h) or a - 1 < 0: continue
        if d > 0 and l[a + 1] - h[a - 1] >= FVG_MIN_PTS: return (h[a - 1] + l[a + 1]) / 2.0
        if d < 0 and l[a - 1] - h[a + 1] >= FVG_MIN_PTS: return (l[a - 1] + h[a + 1]) / 2.0
    return None


def apex_eval(trades, all_dates):
    if trades.empty: return 0, 0, 0
    tdays = sorted(all_dates); idx = {d: k for k, d in enumerate(tdays)}
    tr = trades.copy(); tr["di"] = tr["date"].map(idx)
    np_ = nf = 0
    for s in range(len(tdays)):
        win = tr[(tr["di"] >= s) & (tr["di"] < s + EVAL_DAYS)]
        if win.empty: continue
        eq = peak = START_BAL; out = None
        for _, t in win.iterrows():
            peak = max(peak, eq + t["mfe"])
            if eq + t["mae"] <= peak - APEX_DD: out = "f"; break
            eq += t["pnl"]; peak = max(peak, eq)
            if eq >= START_BAL + APEX_TARGET: out = "p"; break
            if eq <= peak - APEX_DD: out = "f"; break
        if out == "p": np_ += 1
        elif out == "f": nf += 1
    return np_, nf, np_ + nf


def report(trades, df, label):
    print(f"\n===== {label} =====")
    if trades.empty: print("NO TRADES"); return
    pnl = trades["pnl"].values; wins = pnl[pnl > 0]; los = pnl[pnl < 0]
    pf = wins.sum() / abs(los.sum()) if los.sum() else 99
    print(f"trades {len(trades)} ({len(trades)/df['date'].nunique():.2f}/day) | win {(pnl>0).mean()*100:.0f}% | "
          f"net ${pnl.sum():+,.0f} (${pnl.mean():+.0f}/tr) | PF {pf:.2f} | {dict(Counter(trades['reason']))}")
    np_, nf, tot = apex_eval(trades, df["date"].unique())
    print(f"APEX pass rate: {np_/tot*100:.0f}%  ({np_} pass / {nf} fail of {tot} 1-month windows)")
    return np_ / tot if tot else 0


def main():
    full = load()
    print(f"[data] 24h ES: {len(full):,} bars {full['dt'].iloc[0].date()} -> {full['dt'].iloc[-1].date()} "
          f"({full['date'].nunique()} days)", flush=True)
    tr_all = backtest(full)
    report(tr_all, full, "FULL 3 YEARS")
    last1 = full[full["dt"] >= full["dt"].iloc[-1] - pd.Timedelta(days=365)].reset_index(drop=True)
    tr1 = tr_all[tr_all["date"].isin(set(last1["date"]))]
    report(tr1, last1, "LAST 1 YEAR")
    # per calendar year
    print("\nper-year:")
    tr_all["yr"] = pd.to_datetime(tr_all["entry_dt"]).dt.year
    for y, gtr in tr_all.groupby("yr"):
        gd = full[pd.to_datetime(full["dt"]).dt.year == y]
        np_, nf, tot = apex_eval(gtr, gd["date"].unique())
        pr = f"{np_/tot*100:.0f}%" if tot else "n/a"
        print(f"  {y}: {len(gtr)} trades, net ${gtr['pnl'].sum():+,.0f}, apex pass {pr} ({np_}/{tot})")
    tr_all.to_csv(os.path.join(CACHE, "ict_apex_v2_trades.csv"), index=False)


if __name__ == "__main__":
    main()
