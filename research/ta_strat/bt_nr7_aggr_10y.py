"""
bt_nr7_aggr_10y.py — 10-year backtest of the "NR7 Aggressive (NR7 + NQ reversion)" bot,
faithful to nr7_paper.NR7PaperBot + nr7_aggr_paper.NR7AggressivePaperBot.

Two engines on one $50k account:
  A) NR7 breakout on ES+NQ+CL (the robust core):
     - NR7 day = narrowest RTH range of last 7 sessions
     - next session: breakout of NR7 hi(long)/lo(short), first side hit, entry=level+-1tick,
       stop=opposite end -+1tick; 1 tick slippage; half off at +1R -> stop to BE, runner to +2R;
       flat 15:55; entries 09:30-14:00 ET; one pos per market/day
  B) NQ mean-reversion (author-flagged OVERFIT / in-sample):
     - VWAP-2sigma fade back to session mean; else Turtle-Soup of prior-day extreme
     - entries 09:45-14:00 ET; one reversion pos at a time; flat 15:55; no slippage, commission only

Data: cache/{es,nq,cl}_1m_10y.csv (Dukascopy 1m) -> resample 5m -> RTH 09:30-16:00 ET.
Sizing: FIXED risk per trade (default $200, integer micros, min 1) so it is directly
comparable to the Lucid basket test. Commission $0.50/micro round turn.

Usage: bt_nr7_aggr_10y.py [--risk 200] [--core-only] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
NY = "America/New_York"

MARKETS = {"es": (5.0, 0.25), "nq": (2.0, 0.25), "cl": (100.0, 0.01)}
MNQ_PV, MNQ_TICK = 2.0, 0.25
SESSION_START = 9 * 60 + 30      # 09:30
ENTRY_END = 14 * 60             # 14:00
MR_START = 9 * 60 + 45          # 09:45 reversion
FLAT_MIN = 15 * 60 + 55         # 15:55
STOP_BUF_TICKS = 1
SLIP_TICKS = 1.0
COMMISSION_RT = 0.50
K_BAND = 2.0
MIN_BARS = 15
START_BALANCE = 50_000.0
DEFAULT_RISK = 200.0


def load_5m_rth(market: str, start=None, end=None) -> pd.DataFrame:
    path = os.path.join(CACHE, f"{market}_1m_10y.csv")
    if not os.path.exists(path):
        path = os.path.join(CACHE, f"{market}_1m_3y.csv")
    df = pd.read_csv(path, usecols=["dt_utc", "open", "high", "low", "close", "volume"])
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    if start:
        df = df[df["dt_utc"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["dt_utc"] < pd.Timestamp(end, tz="UTC")]
    b = df.set_index("dt_utc")[["open", "high", "low", "close", "volume"]].sort_index()
    d = b.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    d["dt_ny"] = d["dt_utc"].dt.tz_convert(NY)
    mins = d["dt_ny"].dt.hour * 60 + d["dt_ny"].dt.minute
    d = d[(mins >= SESSION_START) & (mins <= 16 * 60)].reset_index(drop=True)
    d["min"] = (d["dt_ny"].dt.hour * 60 + d["dt_ny"].dt.minute).astype(int)
    d["day"] = d["dt_ny"].dt.date
    return d


def daily_bars(d: pd.DataFrame) -> pd.DataFrame:
    g = d.groupby("day")
    out = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(),
                        "open": g["open"].first(), "close": g["close"].last()}).reset_index()
    out["range"] = out["high"] - out["low"]
    return out


def qty_for(risk: float, r_points: float, pv: float) -> int:
    if r_points <= 0:
        return 0
    q = int(np.floor(risk / (r_points * pv)))
    return max(1, q)


# ---------------- NR7 breakout on one market ----------------
def sim_nr7(d: pd.DataFrame, market: str, risk: float) -> list[dict]:
    pv, tick = MARKETS[market]
    days = sorted(d["day"].unique())
    daybars = daily_bars(d).set_index("day")
    trades = []
    day_index = {day: i for i, day in enumerate(daybars.index)}
    ordered_days = list(daybars.index)
    for day in days:
        i = day_index.get(day)
        if i is None or i < 7:
            continue
        prior = daybars.iloc[i - 7:i]        # 7 completed prior sessions
        nr7day = prior.iloc[-1]
        prior6 = prior.iloc[-6:] if len(prior) >= 6 else prior.iloc[:-1]
        # NR7 = last completed day narrowest of prior 7 (its range < min of the 6 before it)
        ref = daybars.iloc[i - 7:i - 1]      # 6 days before the NR7 candidate
        cand = daybars.iloc[i - 1]           # most recent completed day
        if len(ref) < 6 or not (cand["range"] < ref["range"].min()):
            continue
        hi, lo = float(cand["high"]), float(cand["low"])
        today = d[d["day"] == day].reset_index(drop=True)
        fired = False
        for _, bar in today.iterrows():
            m = int(bar["min"])
            if fired or m < SESSION_START or m > ENTRY_END:
                continue
            side = 0
            if float(bar["high"]) >= hi:
                side = 1; entry = hi + STOP_BUF_TICKS * tick; stop = lo - STOP_BUF_TICKS * tick
            elif float(bar["low"]) <= lo:
                side = -1; entry = lo - STOP_BUF_TICKS * tick; stop = hi + STOP_BUF_TICKS * tick
            if side == 0:
                continue
            r = abs(entry - stop)
            if r <= 0:
                continue
            q = qty_for(risk, r, pv)
            fill = entry + side * tick * SLIP_TICKS
            tp1 = fill + side * r
            target = fill + side * 2 * r
            trades.append(_manage_nr7(today, bar.name, side, fill, stop, tp1, target, r, q, pv, tick, day, market))
            fired = True
    return [t for t in trades if t]


def _manage_nr7(today, start_idx, side, entry, stop, tp1, target, r, qty, pv, tick, day, market):
    half_done = False
    realized = 0.0
    q = qty
    rows = today.iloc[start_idx:]
    for _, bar in rows.iterrows():
        hi, lo, cl, m = float(bar["high"]), float(bar["low"]), float(bar["close"]), int(bar["min"])
        if side > 0:
            stopped = lo <= stop; tp1_hit = hi >= tp1; tgt_hit = hi >= target
        else:
            stopped = hi >= stop; tp1_hit = lo <= tp1; tgt_hit = lo >= target if False else (lo <= target)
        if stopped:
            xpx = stop - side * tick * SLIP_TICKS
            pnl = side * (xpx - entry) * q * pv - q * COMMISSION_RT
            return {"day": day, "mkt": market, "eng": "NR7", "pnl": realized + pnl,
                    "reason": "be" if half_done else "stop"}
        if not half_done and tp1_hit:
            half = q // 2
            if half >= 1:
                xpx = tp1 - side * tick * SLIP_TICKS
                realized += side * (xpx - entry) * half * pv - half * COMMISSION_RT
                q -= half
            half_done = True
            stop = entry     # runner to breakeven
        if tgt_hit:
            xpx = target - side * tick * SLIP_TICKS
            pnl = side * (xpx - entry) * q * pv - q * COMMISSION_RT
            return {"day": day, "mkt": market, "eng": "NR7", "pnl": realized + pnl, "reason": "target"}
        if m >= FLAT_MIN:
            pnl = side * (cl - entry) * q * pv - q * COMMISSION_RT
            return {"day": day, "mkt": market, "eng": "NR7", "pnl": realized + pnl, "reason": "eod"}
    # ran out of bars
    cl = float(rows.iloc[-1]["close"])
    pnl = side * (cl - entry) * q * pv - q * COMMISSION_RT
    return {"day": day, "mkt": market, "eng": "NR7", "pnl": realized + pnl, "reason": "eod"}


# ---------------- NQ mean-reversion (overfit add-on) ----------------
def sim_reversion(d: pd.DataFrame, risk: float) -> list[dict]:
    days = sorted(d["day"].unique())
    db = daily_bars(d).set_index("day")
    day_pos = {day: i for i, day in enumerate(db.index)}
    trades = []
    for day in days:
        today = d[d["day"] == day].reset_index(drop=True)
        if len(today) < MIN_BARS:
            continue
        i = day_pos.get(day)
        pdh = pdl = None
        if i is not None and i >= 1:
            prev = db.iloc[i - 1]
            pdh, pdl = float(prev["high"]), float(prev["low"])
        tp_all = (today["high"] + today["low"] + today["close"]) / 3.0
        open_pos = None
        for j in range(len(today)):
            bar = today.iloc[j]
            m = int(bar["min"])
            # manage open reversion
            if open_pos is not None:
                res = _manage_mr_bar(open_pos, bar, m)
                if res is not None:
                    trades.append(res)
                    open_pos = None
            # scan for new
            if open_pos is None and MR_START <= m <= ENTRY_END:
                close = float(bar["close"])
                side = 0; strat = entry = stop = target = None
                if j + 1 >= MIN_BARS:
                    tp = tp_all.iloc[:j + 1]
                    mean = float(tp.mean()); sd = float(tp.std())
                    if sd > 0:
                        if close >= mean + K_BAND * sd:
                            side, strat, entry, stop, target = -1, "VWAP2s", close, mean + (K_BAND + 1) * sd, mean
                        elif close <= mean - K_BAND * sd:
                            side, strat, entry, stop, target = 1, "VWAP2s", close, mean - (K_BAND + 1) * sd, mean
                if side == 0 and pdh is not None:
                    if float(bar["high"]) > pdh and close < pdh:
                        side, strat, entry, stop = -1, "Turtle", close, float(bar["high"]) + 2 * MNQ_TICK
                        target = entry - 2 * (stop - entry)
                    elif float(bar["low"]) < pdl and close > pdl:
                        side, strat, entry, stop = 1, "Turtle", close, float(bar["low"]) - 2 * MNQ_TICK
                        target = entry + 2 * (entry - stop)
                if side != 0:
                    r = abs(entry - stop)
                    if r > 0 and abs(target - entry) > 0:
                        q = qty_for(risk, r, MNQ_PV)
                        open_pos = {"side": side, "entry": entry, "stop": stop, "target": target,
                                    "qty": q, "strat": strat, "day": day}
        if open_pos is not None:   # close at last bar eod
            last = today.iloc[-1]
            cl = float(last["close"]); side = open_pos["side"]
            pnl = side * (cl - open_pos["entry"]) * open_pos["qty"] * MNQ_PV - open_pos["qty"] * COMMISSION_RT
            trades.append({"day": day, "mkt": "nq", "eng": "REV", "pnl": pnl, "reason": "eod", "strat": open_pos["strat"]})
    return trades


def _manage_mr_bar(p, bar, m):
    side = p["side"]
    hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
    xpx = rsn = None
    if side > 0:
        if lo <= p["stop"]: xpx, rsn = p["stop"], "stop"
        elif hi >= p["target"]: xpx, rsn = p["target"], "target"
    else:
        if hi >= p["stop"]: xpx, rsn = p["stop"], "stop"
        elif lo <= p["target"]: xpx, rsn = p["target"], "target"
    if rsn is None and m >= FLAT_MIN:
        xpx, rsn = cl, "eod"
    if rsn:
        pnl = side * (xpx - p["entry"]) * p["qty"] * MNQ_PV - p["qty"] * COMMISSION_RT
        return {"day": p["day"], "mkt": "nq", "eng": "REV", "pnl": pnl, "reason": rsn, "strat": p["strat"]}
    return None


def run(risk=DEFAULT_RISK, core_only=False, start=None, end=None):
    frames = {m: load_5m_rth(m, start, end) for m in MARKETS}
    for m, f in frames.items():
        print(f"  {m}: {len(f):,} 5m RTH bars, {f['day'].min()} -> {f['day'].max()}")
    trades = []
    for m in MARKETS:
        trades += sim_nr7(frames[m], m, risk)
    ncore = len(trades)
    print(f"  NR7 core trades: {ncore}")
    if not core_only:
        rev = sim_reversion(frames["nq"], risk)
        print(f"  NQ reversion trades: {len(rev)}")
        trades += rev
    trades.sort(key=lambda t: (t["day"], t["eng"]))
    return trades


def report(trades, label):
    if not trades:
        print("no trades"); return
    pnls = np.array([t["pnl"] for t in trades])
    wins = int((pnls > 0).sum())
    gp = pnls[pnls > 0].sum(); gn = -pnls[pnls <= 0].sum()
    pf = gp / gn if gn > 0 else float("inf")
    bal = START_BALANCE; peak = bal; mdd = 0.0
    monthly = defaultdict(float); yearly = defaultdict(float)
    for t in trades:
        bal += t["pnl"]
        d = pd.Timestamp(t["day"])
        monthly[(d.year, d.month)] += t["pnl"]; yearly[d.year] += t["pnl"]
        peak = max(peak, bal); mdd = max(mdd, peak - bal)
    negm = sum(1 for v in monthly.values() if v < 0)
    print(f"\n================ {label} ================")
    print(f"trades {len(trades)}  win {100*wins/len(trades):.1f}%  PF {pf:.2f}")
    print(f"${START_BALANCE:,.0f} -> ${bal:,.2f}  ({(bal/START_BALANCE-1)*100:+.1f}%)  maxDD -${mdd:,.2f}")
    print(f"losing months {negm}/{len(monthly)}")
    by_eng = defaultdict(list)
    for t in trades:
        by_eng[t["eng"]].append(t["pnl"])
    for eng, v in sorted(by_eng.items()):
        v = np.array(v); g1 = v[v > 0].sum(); g2 = -v[v <= 0].sum()
        print(f"  {eng}: {len(v)} trades  win {100*(v>0).mean():.1f}%  PF {g1/g2 if g2>0 else 9.99:.2f}  net {v.sum():+,.0f}")
    print("  per year:")
    for y in sorted(yearly):
        print(f"    {y}: {yearly[y]:+11,.0f}")


def main():
    a = sys.argv[1:]
    risk = float(a[a.index("--risk") + 1]) if "--risk" in a else DEFAULT_RISK
    core_only = "--core-only" in a
    start = a[a.index("--start") + 1] if "--start" in a else None
    end = a[a.index("--end") + 1] if "--end" in a else None
    lbl = f"NR7 {'CORE only' if core_only else '+ NQ reversion (AGGRESSIVE)'} risk=${risk:.0f}"
    trades = run(risk, core_only, start, end)
    report(trades, lbl)
    # save daily pnl for the eval sim
    byday = defaultdict(float)
    for t in trades:
        byday[t["day"]] += t["pnl"]
    out = pd.DataFrame([{"day": d, "pnl": v} for d, v in sorted(byday.items())])
    tag = "core" if core_only else "aggr"
    out.to_csv(os.path.join(CACHE, f"nr7_{tag}_daily.csv"), index=False)
    print(f"\nsaved nr7_{tag}_daily.csv ({len(out)} trading days)")


if __name__ == "__main__":
    main()
