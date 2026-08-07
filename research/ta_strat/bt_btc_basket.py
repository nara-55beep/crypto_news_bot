"""
bt_btc_basket.py — apply the EXACT Lucid 5-engine basket logic to BTC.

The Lucid basket is a FUTURES strategy with daily RTH sessions + flat-by-close.
BTC is 24/7, so we map the "session" to the UTC calendar day: VWAP resets at
00:00 UTC, daily bars = UTC dates, force-flat near 23:59 UTC. Engines (deduped to
BTC's single market):
  BTC 3m VWAP fade 2.5s | BTC 5m VWAP fade 2.5s | BTC 30m Turtle Soup 10 | BTC 30m NR7

Same signal + management code as bt_lucid_10y (imported). $200 fixed risk, fractional
units. Reports GROSS and NET at crypto taker-fee levels (the cost cliff matters on BTC).

Usage: bt_btc_basket.py [--data cache/BTC_1m_max.csv] [--fee_bps 6]
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CACHE = os.path.join(HERE, "cache")

from bt_lucid_10y import sig_vwap, sig_turtle, sig_nr7, manage, START_BALANCE

BTC_PV = 1.0
BTC_TICK = 1.0
RISK = 200.0

ENGINES = [
    ("BTC_VWAP3",   "vwap",   3),
    ("BTC_VWAP5",   "vwap",   5),
    ("BTC_TURTLE30", "turtle", 30),
    ("BTC_NR7_30",  "nr7",    30),
]


def load_btc(path: str, start=None, end=None) -> pd.DataFrame:
    df = pd.read_csv(path)
    tcol = "dt" if "dt" in df.columns else "dt_utc"
    df["dt_utc"] = pd.to_datetime(df[tcol], utc=True)
    if start:
        df = df[df["dt_utc"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["dt_utc"] < pd.Timestamp(end, tz="UTC")]
    return df[["dt_utc", "open", "high", "low", "close", "volume"]].drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def make_days_btc(df1m: pd.DataFrame, bar_min: int) -> list[dict]:
    """24/7: resample to bar_min, group by UTC calendar day, minute-of-UTC-day for flat."""
    base = df1m.set_index("dt_utc")[["open", "high", "low", "close", "volume"]].sort_index()
    d = base.resample(f"{bar_min}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    d["min"] = (d["dt_utc"].dt.hour * 60 + d["dt_utc"].dt.minute).astype(int)
    d["day"] = d["dt_utc"].dt.date
    days = []
    for day, g in d.groupby("day", sort=True):
        days.append({
            "day": day, "ts": g["dt_utc"].to_numpy(),
            "op": g["open"].to_numpy(float), "hi": g["high"].to_numpy(float),
            "lo": g["low"].to_numpy(float), "cl": g["close"].to_numpy(float),
            "vol": g["volume"].to_numpy(float), "mins": g["min"].to_numpy(int),
        })
    return days


def sim_engine(days, kind, bar_min, fee_bps):
    flat_min = 24 * 60 - bar_min      # flat near end of UTC day
    daily_hist = []
    trades = []
    for day in days:
        sig = None
        if kind == "vwap":
            sig = sig_vwap(day)
        elif kind == "turtle":
            sig = sig_turtle(day, daily_hist, BTC_TICK)
        elif kind == "nr7":
            sig = sig_nr7(day, daily_hist, BTC_TICK)
        if sig is not None:
            e, side, entry, stop, target = sig
            r = abs(entry - stop)
            evs = manage(day, e, side, entry, stop, target, BTC_PV, BTC_TICK, flat_min, kind, day["day"])
            # recompute cost as crypto % fee on notional (manage's futures cost is wrong for BTC)
            if evs:
                qty = RISK / max(r, 1e-9)
                notional = qty * entry
                fee = notional * (fee_bps / 10000.0)     # round-trip fee
                close_ev = [x for x in evs if x.get("kind") == "close"]
                if close_ev:
                    ce = close_ev[-1]
                    gross = ce["trade_total"] + 0  # trade_total already had futures cost subtracted; recompute gross
                    # reconstruct gross: side*(exit-entry)*qty*pv for the pieces is complex;
                    # simpler: gross ~ trade_total + futures_cost_that_was_applied. We stored r_points.
                    trades.append({"day": day["day"], "kind": kind, "gross_before_ourfee": ce["trade_total"],
                                   "fee": fee, "r": r})
        daily_hist.append((day["day"], float(day["hi"].max()), float(day["lo"].min()),
                           float(day["cl"][-1]), float(day["op"][0])))
    return trades


def run(path, start=None, end=None, fee_bps=6.0):
    df = load_btc(path, start, end)
    print(f"  BTC: {len(df):,} 1m bars, {df['dt_utc'].min()} -> {df['dt_utc'].max()}")
    all_tr = []
    per = {}
    for key, kind, bar_min in ENGINES:
        days = make_days_btc(df, bar_min)
        tr = sim_engine(days, kind, bar_min, fee_bps)
        for t in tr:
            t["key"] = key
        per[key] = tr
        all_tr += tr
    return df, all_tr, per


def report(df, all_tr, per, fee_bps):
    if not all_tr:
        print("no trades"); return
    # NOTE: bt_lucid_10y.manage subtracts a FUTURES cost (RISK*(2*tick/r+0.05)); for BTC that
    # tick=1 term is tiny, so gross_before_ourfee ~= gross minus ~0.05*RISK=$10 flat. We add the
    # crypto % fee on top and also show a pure-gross view by adding back the ~$10 flat.
    gbase = np.array([t["gross_before_ourfee"] + 10.0 for t in all_tr])   # add back ~flat futures cost
    # fee per trade at 6bps was stored; fee scales linearly with bps -> unit fee = fee/6
    unit_fee = np.array([t["fee"] / 6.0 for t in all_tr])
    n = len(all_tr)
    print(f"\n================ BTC basket: {df['dt_utc'].min().date()} -> {df['dt_utc'].max().date()} ================")
    print(f"trades {n}  (3 years)")
    print("\n  === COST CLIFF: round-trip fee sweep (the decisive test for crypto) ===")
    print(f"  {'fee (bps rt)':>13} {'win%':>6} {'PF':>6} {'total$':>12} {'avg$/trade':>11}")
    for bps in (0, 4, 6, 8, 10, 12, 15, 20, 30):
        arr = gbase - unit_fee * bps
        w = (arr > 0).mean(); gp = arr[arr > 0].sum(); gn = -arr[arr <= 0].sum()
        pf = gp / gn if gn > 0 else float("inf")
        flag = "  <- DEAD" if arr.sum() <= 0 else ("  maker/Lighter zone" if bps <= 4 else "")
        print(f"  {bps:>13} {100*w:>5.1f}% {pf:>6.2f} {arr.sum():>+12,.0f} {arr.mean():>+11.2f}{flag}")
    net = gbase - unit_fee * fee_bps
    print("\nper engine (net):")
    for key, tr in per.items():
        if not tr:
            print(f"  {key:<13} 0 trades"); continue
        a = np.array([t["gross_before_ourfee"] + 10.0 - t["fee"] for t in tr])
        print(f"  {key:<13} {len(a):>4} trades  win {100*(a>0).mean():>4.1f}%  net ${a.sum():>+10,.0f}")
    print("\nper year (net):")
    yr = defaultdict(float)
    for t in all_tr:
        yr[pd.Timestamp(t["day"]).year] += t["gross_before_ourfee"] + 10.0 - t["fee"]
    for y in sorted(yr):
        print(f"  {y}: ${yr[y]:>+11,.0f}")


def main():
    a = sys.argv[1:]
    path = a[a.index("--data") + 1] if "--data" in a else os.path.join(CACHE, "BTC_1m_max.csv")
    fee = float(a[a.index("--fee_bps") + 1]) if "--fee_bps" in a else 6.0
    df, all_tr, per = run(path, fee_bps=fee)
    report(df, all_tr, per, fee)


if __name__ == "__main__":
    main()
