"""
audit_feed_trade_impact.py — does the live feed's tick loss actually change TRADES?

Runs the identical backtest engine twice over the same days:
  A) on HISTORICAL Dukascopy .bi5 bars  (what the 10y backtest / +1302% was built on)
  B) on LIVE BRIDGE bars                (what the paper bot actually traded)
and compares the resulting trades side by side.
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
CACHE = os.path.join(HERE, "cache")
sys.path.insert(0, HERE); sys.path.insert(0, PROJ)

from lucid_pass_paper import _duka_ticks, _duka_rows_to_1m, DUKA_INSTRUMENTS
from bt_lucid_10y import COMPONENTS, MARKETS, make_days, sim_component

DAYS = ["2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29"]
SEED_TAIL = 60


def hist_bars(inst):
    out = []
    for day in DAYS:
        for h in range(13, 21):
            rows = _duka_ticks(inst, pd.Timestamp(day, tz="UTC").replace(hour=h))
            if rows:
                out.append(_duka_rows_to_1m(rows))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def bridge_bars(market):
    df = pd.read_csv(os.path.join(PROJ, "data", f"lucid_live_bridge_{market}_1m.csv"))
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    return df


def seed(market):
    df = pd.read_csv(os.path.join(CACHE, f"{market}_1m_3y.csv"),
                     usecols=["dt_utc", "open", "high", "low", "close", "volume"])
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    return df[df["dt_utc"] >= df["dt_utc"].max() - pd.Timedelta(days=SEED_TAIL)]


def build(market, inst, which):
    tail = seed(market)
    new = hist_bars(inst) if which == "hist" else bridge_bars(market)
    cols = ["dt_utc", "open", "high", "low", "close", "volume"]
    df = pd.concat([tail[cols], new[cols]], ignore_index=True)
    return df.drop_duplicates("dt_utc", keep="last").sort_values("dt_utc").reset_index(drop=True)


def trades_for(which):
    raw = {m: build(m, inst, which) for m, inst in
           [(mk, ins) for _s, (mk, ins) in DUKA_INSTRUMENTS.items()]}
    out = []
    d0, d1 = pd.Timestamp(DAYS[0]).date(), pd.Timestamp(DAYS[-1]).date()
    for key, c in COMPONENTS.items():
        days = make_days(raw[c["m"]], c["bar_min"])
        ev = sim_component(key, days)
        for e in ev:
            if e["kind"] == "close" and d0 <= e["day"] <= d1:
                out.append({"day": e["day"], "key": key, "pnl": e["trade_total"],
                            "reason": e["reason"], "entry": round(e["entry"], 2)})
    return sorted(out, key=lambda x: (x["day"], x["key"]))


def main():
    print("Running engine on HISTORICAL tick-file bars ...")
    A = trades_for("hist")
    print("Running engine on LIVE BRIDGE bars ...")
    B = trades_for("bridge")

    sa = sum(t["pnl"] for t in A); sb = sum(t["pnl"] for t in B)
    print(f"\n{'':<12}{'HISTORICAL (backtest source)':>32}{'LIVE BRIDGE (what bot traded)':>32}")
    print(f"{'trades':<12}{len(A):>32}{len(B):>32}")
    print(f"{'net P&L':<12}{sa:>+32,.2f}{sb:>+32,.2f}")
    print(f"{'difference':<12}{'':<32}{sb - sa:>+32,.2f}")

    print("\nper-day / per-strategy comparison:")
    keys = sorted({(t["day"], t["key"]) for t in A} | {(t["day"], t["key"]) for t in B})
    print(f"  {'day':<12}{'strategy':<14}{'HIST pnl':>11}{'LIVE pnl':>11}   note")
    for day, key in keys:
        a = next((t for t in A if t["day"] == day and t["key"] == key), None)
        b = next((t for t in B if t["day"] == day and t["key"] == key), None)
        av = f"{a['pnl']:+.2f}" if a else "-- none --"
        bv = f"{b['pnl']:+.2f}" if b else "-- none --"
        note = ""
        if a and not b: note = "TRADE MISSED BY LIVE FEED"
        elif b and not a: note = "EXTRA TRADE FROM LIVE FEED"
        elif a and b and abs(a["pnl"] - b["pnl"]) > 1:
            note = f"different outcome ({a['reason']} vs {b['reason']})"
        print(f"  {str(day):<12}{key:<14}{av:>11}{bv:>11}   {note}")


if __name__ == "__main__":
    main()
