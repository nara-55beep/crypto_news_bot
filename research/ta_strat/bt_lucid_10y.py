"""
bt_lucid_10y.py — 10-year backtest of the Lucid 50K Continuous basket, replicating
lucid_pass_paper.py / lucid_continuous_paper.py EXACTLY:

  ES 3m VWAP fade 2.5s | NQ 3m VWAP fade 2.5s | CL 5m VWAP fade 2.5s
  NQ 30m Turtle Soup 10 | CL 30m NR7 breakout

Rules replicated from the live bot:
  - session bars only, UTC 13:00-20:59 (bar-start minute in [780, 1260))
  - one trade per component per NY day (fired_keys)
  - $200 fixed risk, fractional micro sizing qty = 200 / (r_points * pv)
  - TP1 at 1R for half, stop -> breakeven, runner to target
    (VWAP target = VWAP itself = 2.5R; turtle/NR7 target = 2R)
  - same-bar priority: stop, then target, then TP1 (conservative)
  - EOD flat on the last bar of the session for the component's timeframe
  - cost per trade = 200 * (2*tick/r_points + 0.05)   (2 ticks RT + $10)
  - continuous account: start $50k, static $48k floor, no daily stop, no target stop

Data: cache/{es,nq,cl}_1m_10y.csv (Dukascopy USA500/USATECH/LIGHT 1m mid-price).
Usage: bt_lucid_10y.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--csv out_prefix]
"""
from __future__ import annotations
import os, sys, math
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
NY = "America/New_York"

RISK_USD = 200.0
COST_FLAT_FRAC = 0.05          # * RISK -> $10 per trade
SESSION_START_MIN = 13 * 60
SESSION_END_MIN = 21 * 60
VWAP_K = 2.5
VWAP_MIN_BARS = 15
TURTLE_LOOKBACK = 10
TURTLE_RECENCY = 4
TURTLE_BUF_TICKS = 8
START_BALANCE = 50_000.0
FLOOR = 48_000.0

MARKETS = {  # market -> (micro $/pt, tick)
    "es": (5.0, 0.25),
    "nq": (2.0, 0.25),
    "cl": (100.0, 0.01),
}
COMPONENTS = {
    "ES_VWAP3":   {"m": "es", "kind": "vwap",   "bar_min": 3},
    "NQ_VWAP3":   {"m": "nq", "kind": "vwap",   "bar_min": 3},
    "CL_VWAP5":   {"m": "cl", "kind": "vwap",   "bar_min": 5},
    "NQ_TURTLE30": {"m": "nq", "kind": "turtle", "bar_min": 30},
    "CL_NR7_30":  {"m": "cl", "kind": "nr7",    "bar_min": 30},
}


def load_market(m: str, start=None, end=None) -> pd.DataFrame:
    path = os.path.join(CACHE, f"{m}_1m_10y.csv")
    if not os.path.exists(path):
        path = os.path.join(CACHE, f"{m}_1m_3y.csv")
        print(f"  [warn] {m}_1m_10y.csv missing, falling back to 3y file")
    df = pd.read_csv(path, usecols=["dt_utc", "open", "high", "low", "close", "volume"])
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    if start is not None:
        df = df[df["dt_utc"] >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        df = df[df["dt_utc"] < pd.Timestamp(end, tz="UTC")]
    return df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def make_days(df1m: pd.DataFrame, bar_min: int) -> list[dict]:
    """Resample to bar_min (label/closed=left), filter session, group by NY day.
    Returns list of day dicts in chronological order."""
    base = df1m.set_index("dt_utc")[["open", "high", "low", "close", "volume"]].sort_index()
    d = base.resample(f"{bar_min}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    mins = d["dt_utc"].dt.hour * 60 + d["dt_utc"].dt.minute
    d = d[(mins >= SESSION_START_MIN) & (mins < SESSION_END_MIN)].reset_index(drop=True)
    d["utcmin"] = (d["dt_utc"].dt.hour * 60 + d["dt_utc"].dt.minute).astype(int)
    d["day"] = d["dt_utc"].dt.tz_convert(NY).dt.date
    days = []
    for day, g in d.groupby("day", sort=True):
        days.append({
            "day": day,
            "ts": g["dt_utc"].to_numpy(),
            "op": g["open"].to_numpy(float), "hi": g["high"].to_numpy(float),
            "lo": g["low"].to_numpy(float), "cl": g["close"].to_numpy(float),
            "vol": g["volume"].to_numpy(float), "mins": g["utcmin"].to_numpy(int),
        })
    return days


# ---------------- signals: return (entry_idx, side, entry, stop, target) or None ----------------

def sig_vwap(day: dict) -> tuple | None:
    hi, lo, cl, vol = day["hi"], day["lo"], day["cl"], day["vol"]
    n = len(hi)
    if n - 1 < VWAP_MIN_BARS:
        return None
    tp = (hi + lo + cl) / 3.0
    v = np.where(vol > 0, vol, 1.0)
    cum_v = cum_pv = cum_p2v = 0.0
    for i in range(n):
        cum_v += v[i]; cum_pv += tp[i] * v[i]; cum_p2v += tp[i] * tp[i] * v[i]
        if cum_v <= 0 or i < VWAP_MIN_BARS:
            continue
        vwap = cum_pv / cum_v
        var = max(cum_p2v / cum_v - vwap * vwap, 0.0)
        sig = math.sqrt(var)
        if sig <= 0:
            continue
        up = vwap + VWAP_K * sig
        dn = vwap - VWAP_K * sig
        if hi[i] >= up:
            return (i, -1, up, vwap + (VWAP_K + 1.0) * sig, vwap)
        if lo[i] <= dn:
            return (i, 1, dn, vwap - (VWAP_K + 1.0) * sig, vwap)
    return None


def sig_turtle(day: dict, daily_hist: list, tick: float) -> tuple | None:
    if len(daily_hist) < TURTLE_LOOKBACK + TURTLE_RECENCY:
        return None
    look = daily_hist[-TURTLE_LOOKBACK:]
    lows = np.array([x[2] for x in look])   # (day, high, low, ...)
    highs = np.array([x[1] for x in look])
    prior_low = float(lows.min()); prior_high = float(highs.max())
    n_hist = len(daily_hist)
    lo_age = n_hist - 1 - int(np.argmin(lows))   # replicates live quirk (always >= 4 here)
    hi_age = n_hist - 1 - int(np.argmax(highs))
    hi, lo = day["hi"], day["lo"]
    for i in range(len(hi)):
        if lo[i] < prior_low and lo_age >= TURTLE_RECENCY:
            d = 1
            entry = prior_low + TURTLE_BUF_TICKS * tick
            stop = float(lo[:i + 1].min()) - tick
        elif hi[i] > prior_high and hi_age >= TURTLE_RECENCY:
            d = -1
            entry = prior_high - TURTLE_BUF_TICKS * tick
            stop = float(hi[:i + 1].max()) + tick
        else:
            continue
        r = abs(entry - stop)
        if r <= 0:
            continue
        target = entry + d * 2.0 * r
        for j in range(i, len(hi)):
            if (d > 0 and hi[j] >= entry) or (d < 0 and lo[j] <= entry):
                return (j, d, entry, stop, target)
        return None   # armed but never filled today
    return None


def sig_nr7(day: dict, daily_hist: list, tick: float) -> tuple | None:
    if len(daily_hist) < 7:
        return None
    last = daily_hist[-1]
    prior6 = daily_hist[-7:-1]
    last_range = last[1] - last[2]
    if last_range >= min(x[1] - x[2] for x in prior6):
        return None
    hi_lvl, lo_lvl = last[1], last[2]
    hi, lo = day["hi"], day["lo"]
    for i in range(len(hi)):
        if hi[i] >= hi_lvl + tick:
            entry = hi_lvl + tick; stop = lo_lvl - tick
            r = abs(entry - stop)
            if r <= 0:
                return None
            return (i, 1, entry, stop, entry + 2.0 * r)
        if lo[i] <= lo_lvl - tick:
            entry = lo_lvl - tick; stop = hi_lvl + tick
            r = abs(entry - stop)
            if r <= 0:
                return None
            return (i, -1, entry, stop, entry - 2.0 * r)
    return None


# ---------------- management: replicate LucidPassPaperBot._manage ----------------

def manage(day: dict, e: int, side: int, entry: float, stop0: float, target: float,
           pv: float, tick: float, flat_min: int, key: str, ny_day) -> list[dict]:
    """Returns cash events: [{ts, cash, kind}], trade pnl total incl cost on final."""
    r = abs(entry - stop0)
    qty0 = RISK_USD / max(r * pv, 1e-9)
    qty = qty0
    tp1 = entry + side * r
    cost = RISK_USD * ((2.0 * tick / r) + COST_FLAT_FRAC)
    stop = stop0
    partial_done = False
    realized = 0.0
    events = []
    hi, lo, cl, mins, ts = day["hi"], day["lo"], day["cl"], day["mins"], day["ts"]

    def close_all(px, ts_i, reason):
        nonlocal realized
        pnl = side * (px - entry) * qty * pv
        total = realized + pnl - cost
        events.append({"ts": ts[ts_i], "cash": pnl - cost, "kind": "close",
                       "key": key, "day": ny_day, "reason": reason, "trade_total": total,
                       "side": side, "entry": entry, "exit": px, "r_points": r})
        return total

    for j in range(e, len(hi)):
        h, l, c, m = hi[j], lo[j], cl[j], int(mins[j])
        if side > 0:
            stopped = l <= stop
            tp1_hit = h >= tp1
            target_hit = h >= target
        else:
            stopped = h >= stop
            tp1_hit = l <= tp1
            target_hit = l <= target
        if not partial_done:
            if stopped:
                close_all(stop, j, "stop"); return events
            if target_hit:
                close_all(target, j, "target"); return events
            if tp1_hit:
                half = min(qty0 * 0.5, qty)
                pnl = side * (tp1 - entry) * half * pv
                realized += pnl
                events.append({"ts": ts[j], "cash": pnl, "kind": "partial", "key": key, "day": ny_day})
                qty -= half
                partial_done = True
                stop = entry
                if m >= flat_min:
                    close_all(c, j, "eod"); return events
                continue
        else:
            if stopped:
                close_all(stop, j, "be" if abs(stop - entry) < 1e-9 else "stop"); return events
            if target_hit:
                close_all(target, j, "target"); return events
        if m >= flat_min:
            close_all(c, j, "eod"); return events
    close_all(cl[-1], len(hi) - 1, "eod")   # data ran out (gap) -> close at last close
    return events


def sim_component(key: str, days: list[dict]) -> list[dict]:
    c = COMPONENTS[key]
    pv, tick = MARKETS[c["m"]]
    flat_min = SESSION_END_MIN - c["bar_min"]
    daily_hist: list[tuple] = []   # (day, high, low, close, open)
    all_events = []
    for day in days:
        kind = c["kind"]
        sig = None
        if kind == "vwap":
            sig = sig_vwap(day)
        elif kind == "turtle":
            sig = sig_turtle(day, daily_hist, tick)
        elif kind == "nr7":
            sig = sig_nr7(day, daily_hist, tick)
        if sig is not None:
            e, side, entry, stop, target = sig
            all_events += manage(day, e, side, entry, stop, target, pv, tick, flat_min, key, day["day"])
        daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                           float(day["cl"][-1]), float(day["op"][0])))
    return all_events


def run(start=None, end=None):
    frames = {}
    for m in MARKETS:
        df = load_market(m, start, end)
        frames[m] = df
        if df.empty:
            print(f"  {m}: NO DATA in window — component skipped")
        else:
            print(f"  {m}: {len(df):,} 1m bars "
                  f"{df['dt_utc'].iloc[0]} -> {df['dt_utc'].iloc[-1]}")
    events = []
    for key, c in COMPONENTS.items():
        if frames[c["m"]].empty:
            continue
        days = make_days(frames[c["m"]], c["bar_min"])
        ev = sim_component(key, days)
        ntr = sum(1 for x in ev if x["kind"] == "close")
        print(f"  {key}: {ntr} trades over {len(days)} sessions")
        events += ev
    events.sort(key=lambda x: pd.Timestamp(x["ts"]).value)
    return events


def report(events, label):
    closes = [e for e in events if e["kind"] == "close"]
    if not closes:
        print("no trades"); return
    totals = np.array([e["trade_total"] for e in closes])
    wins = int((totals > 0).sum())
    gross_pos = totals[totals > 0].sum()
    gross_neg = -totals[totals <= 0].sum()
    pf = gross_pos / gross_neg if gross_neg > 0 else float("inf")

    bal = START_BALANCE
    peak = bal
    max_dd = 0.0
    died = None
    monthly = defaultdict(float)
    yearly = defaultdict(float)
    for e in events:
        bal += e["cash"]
        d = pd.Timestamp(e["ts"]).tz_convert(NY)
        monthly[(d.year, d.month)] += e["cash"]
        yearly[d.year] += e["cash"]
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)
        if died is None and bal <= FLOOR:
            died = d.date()

    n_neg_months = sum(1 for v in monthly.values() if v < 0)
    print(f"\n================ {label} ================")
    print(f"trades {len(closes)}  win {100*wins/len(closes):.1f}%  PF {pf:.2f}")
    print(f"${START_BALANCE:,.0f} -> ${bal:,.2f}   ({(bal/START_BALANCE-1)*100:+.1f}%)")
    print(f"max drawdown -${max_dd:,.2f}   losing months {n_neg_months}/{len(monthly)}")
    if died:
        print(f"*** $48,000 FLOOR BREACHED {died} — continuous bot would have STOPPED there ***")
    print("\nper year:")
    by_year_trades = defaultdict(list)
    for e in closes:
        by_year_trades[pd.Timestamp(e["ts"]).tz_convert(NY).year].append(e["trade_total"])
    for y in sorted(yearly):
        yr_months = [v for (yy, mm), v in monthly.items() if yy == y]
        neg = sum(1 for v in yr_months if v < 0)
        t = np.array(by_year_trades.get(y, [0.0]))
        gp = t[t > 0].sum(); gn = -t[t <= 0].sum()
        pf_y = gp / gn if gn > 0 else float("inf")
        print(f"  {y}: {yearly[y]:+12,.2f}   {len(t):4d} trades  win {100*(t>0).mean():5.1f}%  "
              f"PF {pf_y:5.2f}   losing months {neg}/{len(yr_months)}")
    print("\nper component:")
    bykey = defaultdict(list)
    for e in closes:
        bykey[e["key"]].append(e["trade_total"])
    for k, v in sorted(bykey.items()):
        v = np.array(v)
        gp = v[v > 0].sum(); gn = -v[v <= 0].sum()
        print(f"  {k:12s} {len(v):5d} trades  win {100*(v>0).mean():5.1f}%  "
              f"PF {gp/gn if gn>0 else float('inf'):5.2f}  net {v.sum():+12,.2f}")
    print("\nworst 6 months:")
    for (y, m), v in sorted(monthly.items(), key=lambda kv: kv[1])[:6]:
        print(f"  {y}-{m:02d}: {v:+10,.2f}")
    return {"events": events}


def main():
    args = sys.argv[1:]
    start = end = None
    if "--start" in args:
        start = args[args.index("--start") + 1]
    if "--end" in args:
        end = args[args.index("--end") + 1]
    label = f"Lucid continuous basket {start or 'data-start'} -> {end or 'data-end'}"
    events = run(start, end)
    out = report(events, label)
    if "--csv" in args:
        prefix = args[args.index("--csv") + 1]
        rows = []
        bal = START_BALANCE
        for e in out["events"]:
            bal += e["cash"]
            rows.append({"ts": e["ts"], "cash": e["cash"], "balance": bal,
                         "kind": e["kind"], "key": e.get("key"), "reason": e.get("reason", "")})
        pd.DataFrame(rows).to_csv(os.path.join(CACHE, f"{prefix}_events.csv"), index=False)
        print(f"saved {prefix}_events.csv")


if __name__ == "__main__":
    main()
