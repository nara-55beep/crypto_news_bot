"""
audit_remaining_gaps.py — quantify the two differences that are NOT strategy-logic:

  1) CONTRACT CAP: neither the backtest nor the paper bot caps position size, but real
     Lucid caps micros (25K: 20 micros, 50K: 40). How many backtest trades wanted more?
     Those trades would be smaller in reality -> less profit than the backtest shows.

  2) MISSED-ENTRY RISK: the live bot refuses entries on stale bars ("spent" rule). The
     backtest always fills. Measures how much of total profit sits in trades whose signal
     bar is EARLY in the session (most exposed to a mid-session restart burning the day).
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bt_lucid_10y import run, MARKETS, COMPONENTS, RISK_USD

CAPS = {"25K @ $100 risk": (20, 100.0), "50K @ $200 risk": (40, 200.0)}


def main():
    events = run()
    closes = [e for e in events if e["kind"] == "close"]
    print(f"\n{len(closes)} trades over 10 years\n")

    print("=== 1) CONTRACT-CAP IMPACT (real Lucid limits, not modelled anywhere) ===")
    for label, (cap, risk) in CAPS.items():
        over = 0; lost = 0.0; tot = 0.0
        for e in closes:
            pv, _tick = MARKETS[COMPONENTS[e["key"]]["m"]]
            r = e["r_points"]
            qty = risk / max(r * pv, 1e-9)
            scaled = e["trade_total"] * (risk / RISK_USD)
            tot += scaled
            if qty > cap:
                over += 1
                capped = scaled * (cap / qty)      # forced smaller position
                lost += (scaled - capped)
        print(f"  {label:<20} trades over cap: {over:>5} / {len(closes)}  ({100*over/len(closes):>4.1f}%)"
              f"   profit lost to cap: ${lost:>10,.0f}  of ${tot:>10,.0f}  ({100*lost/tot if tot else 0:>4.1f}%)")

    print("\n=== 2) EXPOSURE TO A MISSED ENTRY (mid-session restart / feed lag) ===")
    print("  Live burns a component's day if its signal bar already passed.")
    print("  Average profit per trade by component (what one missed day costs):")
    by = {}
    for e in closes:
        by.setdefault(e["key"], []).append(e["trade_total"])
    for k, v in sorted(by.items()):
        a = np.array(v)
        print(f"    {k:<13} {len(a):>5} trades   avg ${a.mean():>+7.2f}   total ${a.sum():>+11,.0f}")
    allp = np.array([e["trade_total"] for e in closes])
    print(f"\n  => one missed component-day costs ~${allp.mean():.2f} on average")
    print(f"  => a full missed session (all 5) costs ~${allp.mean()*len(by):.2f}")


if __name__ == "__main__":
    main()
