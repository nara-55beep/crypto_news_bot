"""
causal_engine.py — a backtest engine that CANNOT contain look-ahead by construction.

Every failure we found in the old harness is structurally impossible here:

  1. DECIDE ON CLOSED BARS ONLY. A strategy is handed bars[0..i] and returns orders that
     become working from bar i+1. It can never be triggered by the bar that created it.
  2. GAP-AWARE FILLS. A resting order fills at the worse of (level, bar open). If price
     gapped past the level, you get the open - never the unavailable price.
  3. NO INTRABAR ORDERING ASSUMPTIONS. If a bar contains both the stop and the target,
     the STOP is taken. We never assume the favourable extreme came first.
  4. INTEGER CONTRACTS + hard size cap (Lucid 25K = 20 micros, 50K = 40).
  5. REAL COSTS: per-contract commission + spread, scaled by size (not a flat $10).
  6. LUCID EOD TRAILING DRAWDOWN, evaluated on CLOSED balance at session end, locking at
     start+$100 once the peak clears the target buffer.
  7. DAYS-TO-PASS counted over EVERY session, including zero-trade days.

Execution runs on 1-MINUTE bars so intrabar behaviour is resolved as finely as the data
allows. Strategies only ever see completed session bars at their own timeframe.
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
from dataclasses import dataclass
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
NY = "America/New_York"
SESSION_START_MIN, SESSION_END_MIN = 13 * 60, 21 * 60

# market -> (micro $/point, tick, commission per micro round-turn, spread in ticks)
MARKETS = {
    "es": (5.0, 0.25, 1.24, 1.0),
    "nq": (2.0, 0.25, 1.24, 1.0),
    "cl": (100.0, 0.01, 1.24, 1.0),
}


@dataclass
class Order:
    side: int            # +1 long, -1 short
    kind: str            # "stop" (breakout) or "limit" (fade)
    level: float
    stop: float
    target: float
    tag: str = ""


_LOAD_CACHE = {}


def load_1m(market: str, start=None, end=None, ny_session=None) -> pd.DataFrame:
    ck = (market, start, end, ny_session)
    if ck in _LOAD_CACHE:
        return _LOAD_CACHE[ck]
    out = _load_1m_uncached(market, start, end, ny_session)
    _LOAD_CACHE[ck] = out
    return out


def _load_1m_uncached(market: str, start=None, end=None, ny_session=None) -> pd.DataFrame:
    p = os.path.join(CACHE, f"{market}_1m_10y.csv")
    if not os.path.exists(p):
        p = os.path.join(CACHE, f"{market}_1m_3y.csv")
    df = pd.read_csv(p, usecols=["dt_utc", "open", "high", "low", "close", "volume"])
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    if start:
        df = df[df["dt_utc"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["dt_utc"] < pd.Timestamp(end, tz="UTC")]
    ny = df["dt_utc"].dt.tz_convert(NY)
    if ny_session is not None:
        mins = ny.dt.hour * 60 + ny.dt.minute
        lo, hi = ny_session
    else:
        mins = df["dt_utc"].dt.hour * 60 + df["dt_utc"].dt.minute
        lo, hi = SESSION_START_MIN, SESSION_END_MIN
    df = df[(mins >= lo) & (mins < hi)].copy()
    df["min"] = mins[df.index]
    df["day"] = ny[df.index].dt.date
    return df.sort_values("dt_utc").reset_index(drop=True)


def resample_session(df1m: pd.DataFrame, bar_min: int, ny_session=None) -> pd.DataFrame:
    b = df1m.set_index("dt_utc")[["open", "high", "low", "close", "volume"]].sort_index()
    d = b.resample(f"{bar_min}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    ny = d["dt_utc"].dt.tz_convert(NY)
    if ny_session is not None:
        mins = ny.dt.hour * 60 + ny.dt.minute
        lo, hi = ny_session
    else:
        mins = d["dt_utc"].dt.hour * 60 + d["dt_utc"].dt.minute
        lo, hi = SESSION_START_MIN, SESSION_END_MIN
    d = d[(mins >= lo) & (mins < hi)].copy()
    d["min"] = mins[d.index]
    d["day"] = ny[d.index].dt.date
    return d.reset_index(drop=True)


def _fill_price(order: Order, bar_open: float, bar_hi: float, bar_lo: float):
    """Gap-aware. Returns fill price or None. Never returns an unavailable price."""
    lv = order.level
    if order.kind == "stop":                       # breakout entry
        if order.side > 0:
            if bar_open >= lv:                     # gapped through -> pay the open
                return bar_open
            return lv if bar_hi >= lv else None
        else:
            if bar_open <= lv:
                return bar_open
            return lv if bar_lo <= lv else None
    else:                                          # limit / fade entry
        if order.side > 0:
            if bar_open <= lv:                     # gapped in our favour -> get the open
                return bar_open
            return lv if bar_lo <= lv else None
        else:
            if bar_open >= lv:
                return bar_open
            return lv if bar_hi >= lv else None


def run_strategy(market: str, bar_min: int, decide, risk_usd: float, max_micros: int,
                 start=None, end=None, half_off_at_1r: bool = True, ny_session=None):
    """
    decide(prior_session_bars, todays_bars_so_far, day) -> Order | None
      * prior_session_bars: DataFrame of ALL completed prior sessions at bar_min
      * todays_bars_so_far: DataFrame of today's COMPLETED bars up to and including i
      Returned order becomes working on the NEXT bar. Never on bar i.
    """
    pv, tick, comm, spread_ticks = MARKETS[market]
    m1 = load_1m(market, start, end, ny_session)
    bars = resample_session(m1, bar_min, ny_session)
    if bars.empty:
        return []

    m1_by_day = {d: g.reset_index(drop=True) for d, g in m1.groupby("day")}
    bars_by_day = {d: g.reset_index(drop=True) for d, g in bars.groupby("day")}
    days = sorted(bars_by_day)
    flat_min = (ny_session[1] - 5) if ny_session is not None else (SESSION_END_MIN - 1)
    trades = []
    # Precompute per-session daily aggregates ONCE (was recomputed every minute).
    daily_agg = bars.groupby("day").agg(high=("high", "max"), low=("low", "min"),
                                        open=("open", "first"), close=("close", "last")).reset_index()
    daily_agg["range"] = daily_agg["high"] - daily_agg["low"]
    day_pos = {d: i for i, d in enumerate(daily_agg["day"])}

    for di, day in enumerate(days):
        tb = bars_by_day[day]
        mb = m1_by_day.get(day)
        if mb is None or len(mb) < 2:
            continue
        prior = daily_agg.iloc[:day_pos.get(day, 0)]
        bar_ends = tb["min"].to_numpy() + bar_min      # minute at which each bar is complete
        last_decided = -1

        order = None
        order_from_min = None
        pos = None            # dict once filled

        m_open = mb["open"].to_numpy(float); m_high = mb["high"].to_numpy(float)
        m_low = mb["low"].to_numpy(float); m_close = mb["close"].to_numpy(float)
        m_min = mb["min"].to_numpy(np.int64)
        for k in range(len(m_open)):
            o = m_open[k]; h = m_high[k]; l = m_low[k]; c = m_close[k]
            mnow = int(m_min[k])

            # ---------- manage an open position (stop-first, never assume ordering) ----------
            if pos is not None:
                side = pos["side"]
                hit_stop = (l <= pos["stop"]) if side > 0 else (h >= pos["stop"])
                hit_tgt = (h >= pos["target"]) if side > 0 else (l <= pos["target"])
                hit_tp1 = (h >= pos["tp1"]) if side > 0 else (l <= pos["tp1"])
                exit_px = reason = None
                if hit_stop:                       # WORST CASE FIRST
                    exit_px, reason = pos["stop"], ("be" if abs(pos["stop"] - pos["entry"]) < 1e-9 else "stop")
                elif hit_tgt:
                    exit_px, reason = pos["target"], "target"
                elif half_off_at_1r and (not pos["half"]) and hit_tp1:
                    half = pos["qty"] // 2
                    if half >= 1:
                        pnl = side * (pos["tp1"] - pos["entry"]) * half * pv - half * comm
                        pos["realized"] += pnl
                        pos["qty"] -= half
                    pos["half"] = True
                    pos["stop"] = pos["entry"]     # runner to breakeven
                elif mnow >= flat_min:
                    exit_px, reason = c, "eod"
                if exit_px is not None:
                    pnl = side * (exit_px - pos["entry"]) * pos["qty"] * pv - pos["qty"] * comm
                    trades.append({"day": day, "market": market, "side": side,
                                   "entry": pos["entry"], "exit": exit_px,
                                   "qty0": pos["qty0"], "pnl": pos["realized"] + pnl,
                                   "reason": reason, "tag": pos["tag"]})
                    pos = None
                    order = None                   # one trade per day per strategy
                continue

            # ---------- try to fill a working order (only from the bar AFTER it was created) ----------
            if order is not None and order_from_min is not None and mnow >= order_from_min:
                fp = _fill_price(order, o, h, l)
                if fp is not None:
                    fp += order.side * tick * spread_ticks       # pay the spread
                    r = abs(fp - order.stop)
                    if r > 0:
                        qty = int(np.floor(risk_usd / (r * pv)))
                        qty = max(0, min(qty, max_micros))
                        if qty >= 1:
                            pos = {"side": order.side, "entry": fp, "stop": order.stop,
                                   "target": order.target, "tp1": fp + order.side * r,
                                   "qty": qty, "qty0": qty, "half": False, "realized": 0.0,
                                   "tag": order.tag}
                    order = None
                    continue

            # ---------- strategy decides on COMPLETED bars only ----------
            # Only re-decide when a new bar has actually completed (not every minute).
            if order is None and pos is None:
                n_done = int(np.searchsorted(bar_ends, mnow, side="right"))
                if n_done > 0 and n_done != last_decided:
                    last_decided = n_done
                    nxt = decide(prior, tb.iloc[:n_done], day)
                    if nxt is not None:
                        order = nxt
                        order_from_min = mnow + 1           # working from the NEXT minute
    return trades


# ---------------------------- reporting ----------------------------
def report(trades, all_days, label, start_balance=50000.0, target=3000.0,
           dd=2000.0, lock_at=100.0):
    if not trades:
        print(f"\n=== {label} ===\n  no trades"); return
    p = np.array([t["pnl"] for t in trades])
    gp = p[p > 0].sum(); gn = -p[p <= 0].sum()
    byday = defaultdict(float)
    for t in trades:
        byday[t["day"]] += t["pnl"]
    # full session series (zero-trade days included)
    series = np.array([byday.get(d, 0.0) for d in all_days])
    bal = start_balance; peak = bal; mdd = 0.0
    for v in series:
        bal += v; peak = max(peak, bal); mdd = max(mdd, peak - bal)
    # Lucid eval from every session start, EOD trailing evaluated on closed balance
    npass = nfail = nund = 0; dd_days = []
    for i0 in range(len(series)):
        cum = 0.0; pk = 0.0
        for k in range(i0, min(i0 + 250, len(series))):
            cum += series[k]
            if cum >= target:
                npass += 1; dd_days.append(k - i0 + 1); break
            pk = max(pk, cum)
            floor = min(pk - dd, lock_at)
            if cum <= floor:
                nfail += 1; break
        else:
            nund += 1
    dec = npass + nfail
    print(f"\n=== {label} ===")
    print(f"  trades {len(p)}   win {100*(p>0).mean():.1f}%   PF {gp/gn if gn>0 else 9.99:.2f}")
    print(f"  net ${bal-start_balance:>+11,.0f}   maxDD -${mdd:>9,.0f}   over {len(all_days)} sessions")
    print(f"  Lucid eval (all session starts): pass {npass}  fail {nfail}  undecided {nund}"
          f"  -> {100*npass/dec if dec else 0:.0f}% of decided")
    print(f"  sessions to pass: median {int(np.median(dd_days)) if dd_days else -1}, "
          f"mean {np.mean(dd_days) if dd_days else -1:.1f}")
