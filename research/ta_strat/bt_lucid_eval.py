"""
bt_lucid_eval.py — Lucid 50K eval-pass + funded-survival simulation for the 5-strategy
basket, driven by bt_lucid_10y's trade events.

Real Lucid 50K rules modeled:
  * start $50,000, pass at +$3,000
  * $2,000 END-OF-DAY trailing drawdown: floor = eod_peak - 2000, checked at EOD only,
    locks at $50,000 (start) once eod_peak >= $52,000
  * no time limit (horizon capped for reporting)

For every possible start day we simulate:
  EVAL:   days until pass / fail-at-floor (or undecided within horizon)
  FUNDED: same trailing rule, trade until floor breach; record extracted PnL at death
          (PnL accumulated before breach = what you could have withdrawn in total)

Usage: bt_lucid_eval.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np
import pandas as pd

from bt_lucid_10y import run, NY

TARGET = 3_000.0
DD = 2_000.0
HORIZON = 120          # trading days cap for the eval stats
FUNDED_HORIZON = 500   # trading days cap for funded survival


def daily_pnl(events) -> tuple[list, np.ndarray]:
    by_day = defaultdict(float)
    for e in events:
        d = pd.Timestamp(e["ts"]).tz_convert(NY).date()
        by_day[d] += e["cash"]
    days = sorted(by_day)
    return days, np.array([by_day[d] for d in days])


def sim_eval(pnl: np.ndarray, i0: int) -> tuple[str, int]:
    """Start an eval on day i0. Returns (outcome, days_used)."""
    cum = 0.0
    peak = 0.0
    for k in range(i0, min(i0 + HORIZON, len(pnl))):
        cum += pnl[k]
        if cum >= TARGET:
            return "pass", k - i0 + 1
        peak = max(peak, cum)
        floor = min(peak - DD, 0.0)    # locks at start (0) once peak >= +2000
        if cum <= floor:
            return "fail", k - i0 + 1
    return "undecided", min(HORIZON, len(pnl) - i0)


def sim_funded(pnl: np.ndarray, i0: int) -> tuple[float, int, bool]:
    """Run a funded account from day i0 until EOD trailing floor breach.
    Returns (pnl_at_death_or_end, days, died)."""
    cum = 0.0
    peak = 0.0
    for k in range(i0, min(i0 + FUNDED_HORIZON, len(pnl))):
        cum += pnl[k]
        peak = max(peak, cum)
        floor = min(peak - DD, 0.0)
        if cum <= floor:
            return cum, k - i0 + 1, True
    return cum, min(FUNDED_HORIZON, len(pnl) - i0), False


def main():
    args = sys.argv[1:]
    start = end = None
    if "--start" in args:
        start = args[args.index("--start") + 1]
    if "--end" in args:
        end = args[args.index("--end") + 1]
    events = run(start, end)
    days, pnl = daily_pnl(events)
    print(f"\n{len(days)} trading days {days[0]} -> {days[-1]}")

    # -------- eval pass simulation, grouped by start-year --------
    by_year = defaultdict(lambda: {"pass": 0, "fail": 0, "undecided": 0, "days": []})
    for i0 in range(len(days)):
        outcome, ndays = sim_eval(pnl, i0)
        y = days[i0].year
        by_year[y][outcome] += 1
        if outcome == "pass":
            by_year[y]["days"].append(ndays)
    print("\n=== Lucid 50K EVAL: start on every trading day, +$3k target, $2k EOD-trailing ===")
    print(f"  {'year':>6} {'starts':>7} {'pass%':>7} {'fail%':>7} {'undec%':>7} {'med days to pass':>17}")
    tot = {"pass": 0, "fail": 0, "undecided": 0}
    all_days = []
    for y in sorted(by_year):
        s = by_year[y]
        n = s["pass"] + s["fail"] + s["undecided"]
        med = int(np.median(s["days"])) if s["days"] else -1
        print(f"  {y:>6} {n:>7} {100*s['pass']/n:>6.0f}% {100*s['fail']/n:>6.0f}% "
              f"{100*s['undecided']/n:>6.0f}% {med:>17}")
        for k in tot:
            tot[k] += s[k]
        all_days += s["days"]
    n = sum(tot.values())
    print(f"  {'ALL':>6} {n:>7} {100*tot['pass']/n:>6.0f}% {100*tot['fail']/n:>6.0f}% "
          f"{100*tot['undecided']/n:>6.0f}% {int(np.median(all_days)) if all_days else -1:>17}")

    # -------- funded survival --------
    print("\n=== FUNDED 50K: same EOD trailing, trade until death ===")
    print(f"  {'start-year':>10} {'n':>6} {'died%':>7} {'med days alive':>15} {'med $ at death/end':>19} {'mean $':>10}")
    fy = defaultdict(lambda: {"pnl": [], "days": [], "died": 0})
    for i0 in range(len(days)):
        p, nd, died = sim_funded(pnl, i0)
        y = days[i0].year
        fy[y]["pnl"].append(p)
        fy[y]["days"].append(nd)
        fy[y]["died"] += int(died)
    for y in sorted(fy):
        s = fy[y]
        n = len(s["pnl"])
        print(f"  {y:>10} {n:>6} {100*s['died']/n:>6.0f}% {int(np.median(s['days'])):>15} "
              f"{np.median(s['pnl']):>19,.0f} {np.mean(s['pnl']):>10,.0f}")


if __name__ == "__main__":
    main()
