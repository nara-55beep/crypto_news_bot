"""
replay_live_days.py — replay the EXACT days the live Lucid bot traded (2026-07-09..21)
through the independent backtest engine, using the SAME data the live bot saw
(seed CSVs + data/lucid_live_bridge_*.csv), and compare trade-for-trade.

Answers: is the live underperformance a live-vs-backtest execution gap, or was this
period simply a bad stretch for the strategy?

Usage: replay_live_days.py [--start 2026-07-09] [--end 2026-07-22]
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(os.path.dirname(HERE)) if False else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ = os.path.dirname(HERE)          # ...\crypto_news_bot\research
PROJ = os.path.dirname(PROJ)          # ...\crypto_news_bot
CACHE = os.path.join(HERE, "cache")
NY = "America/New_York"

from bt_lucid_10y import COMPONENTS, MARKETS, make_days, sim_component, START_BALANCE

LIVE_START = "2026-07-09"
LIVE_END = "2026-07-22"
SEED_TAIL_DAYS = 90     # enough history for Turtle(14 sessions) / NR7(7 sessions)


def load_seed_plus_bridge(market: str) -> pd.DataFrame:
    """Reproduce exactly what the live bot loaded: 3y seed + live bridge rows."""
    seed = pd.read_csv(os.path.join(CACHE, f"{market}_1m_3y.csv"),
                       usecols=["dt_utc", "open", "high", "low", "close", "volume"])
    seed["dt_utc"] = pd.to_datetime(seed["dt_utc"], utc=True)
    cutoff = seed["dt_utc"].max() - pd.Timedelta(days=SEED_TAIL_DAYS)
    seed = seed[seed["dt_utc"] >= cutoff]

    bpath = os.path.join(PROJ, "data", f"lucid_live_bridge_{market}_1m.csv")
    bridge = pd.read_csv(bpath, usecols=["dt_utc", "open", "high", "low", "close", "volume"])
    bridge["dt_utc"] = pd.to_datetime(bridge["dt_utc"], utc=True)

    out = pd.concat([seed, bridge], ignore_index=True)
    out = out.drop_duplicates("dt_utc", keep="last").sort_values("dt_utc").reset_index(drop=True)
    return out


def main():
    a = sys.argv[1:]
    start = a[a.index("--start") + 1] if "--start" in a else LIVE_START
    end = a[a.index("--end") + 1] if "--end" in a else LIVE_END
    s_d, e_d = pd.Timestamp(start).date(), pd.Timestamp(end).date()

    raw = {m: load_seed_plus_bridge(m) for m in MARKETS}
    for m, d in raw.items():
        print(f"  {m}: {len(d):,} 1m bars, {d['dt_utc'].min().date()} -> {d['dt_utc'].max().date()}")

    all_trades = []
    per_comp = {}
    for key, c in COMPONENTS.items():
        days = make_days(raw[c["m"]], c["bar_min"])
        ev = sim_component(key, days)          # full history so Turtle/NR7 lookback is right
        closes = [e for e in ev if e["kind"] == "close"]
        # keep only the live window
        win = [e for e in closes if s_d <= e["day"] <= e_d]
        per_comp[key] = win
        all_trades += win

    all_trades.sort(key=lambda t: (t["day"], t["key"]))
    tot = np.array([t["trade_total"] for t in all_trades]) if all_trades else np.array([0.0])
    wins = int((tot > 0).sum())
    n = len(all_trades)

    print(f"\n================ BACKTEST ENGINE replayed on {start}..{end} ================")
    print(f"trades {n}  wins {wins} ({100*wins/n:.0f}%)  net ${tot.sum():+,.2f}  avg ${tot.mean():+.2f}/trade")
    print("\nper component:")
    for k, v in per_comp.items():
        if not v:
            print(f"  {k:<13} 0 trades")
            continue
        p = np.array([x["trade_total"] for x in v])
        print(f"  {k:<13} {len(p):>2} trades  win {100*(p>0).mean():>3.0f}%  net ${p.sum():>+9.2f}")

    print("\nper day:")
    byday = defaultdict(list)
    for t in all_trades:
        byday[t["day"]].append(t)
    for day in sorted(byday):
        p = np.array([x["trade_total"] for x in byday[day]])
        print(f"  {day}: {len(p)} trades net ${p.sum():+9.2f}   " +
              " ".join(f"{x['key'][:9]}:{x['trade_total']:+.0f}({x['reason']})" for x in byday[day]))

    print("\n--- COMPARISON vs LIVE BOT (28 trades, 57% win, +$245.17) ---")
    print(f"  backtest engine on same days: {n} trades, {100*wins/n:.0f}% win, ${tot.sum():+,.2f}")
    print(f"  live bot                    : 28 trades, 57% win, $+245.17")
    print(f"  DIFFERENCE                  : {n-28:+d} trades, ${tot.sum()-245.17:+,.2f}")


if __name__ == "__main__":
    main()
