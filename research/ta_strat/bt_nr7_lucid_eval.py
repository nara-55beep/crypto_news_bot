"""
bt_nr7_lucid_eval.py — Lucid 50K eval-pass sim for the NR7 daily-pnl series
(nr7_core_daily.csv / nr7_aggr_daily.csv from bt_nr7_aggr_10y.py).

Lucid 50K: +$3,000 target, $2,000 EOD trailing (locks at start once peak>=+$2,000).
Builds a COMPLETE business-day series (zero-fill no-trade days) so "days to pass"
is real calendar time, then starts an eval on every trading day.
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd
from collections import defaultdict

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
TARGET = 3_000.0
DD = 2_000.0
HORIZON = 200


def load_full_daily(tag: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(CACHE, f"nr7_{tag}_daily.csv"))
    df["day"] = pd.to_datetime(df["day"])
    full = pd.date_range(df["day"].min(), df["day"].max(), freq="B")
    s = df.set_index("day")["pnl"].reindex(full, fill_value=0.0)
    return pd.DataFrame({"day": s.index, "pnl": s.values})


def sim_eval(pnl: np.ndarray, i0: int):
    cum = 0.0; peak = 0.0
    for k in range(i0, min(i0 + HORIZON, len(pnl))):
        cum += pnl[k]
        if cum >= TARGET:
            return "pass", k - i0 + 1
        peak = max(peak, cum)
        floor = min(peak - DD, 0.0)
        if cum <= floor:
            return "fail", k - i0 + 1
    return "undecided", min(HORIZON, len(pnl) - i0)


def run(tag: str, label: str):
    d = load_full_daily(tag)
    pnl = d["pnl"].to_numpy()
    days = d["day"].dt.date.to_numpy()
    by_year = defaultdict(lambda: {"pass": 0, "fail": 0, "undecided": 0, "days": []})
    for i0 in range(len(pnl)):
        outcome, nd = sim_eval(pnl, i0)
        y = days[i0].year
        by_year[y][outcome] += 1
        if outcome == "pass":
            by_year[y]["days"].append(nd)
    print(f"\n=== {label}: Lucid 50K eval, start every trading day (+$3k / $2k trailing) ===")
    print(f"  {'year':>6} {'pass%':>7} {'fail%':>7} {'undec%':>7} {'med days to pass':>17}")
    tp = tf = tu = 0; allc = []
    for y in sorted(by_year):
        s = by_year[y]; n = s["pass"] + s["fail"] + s["undecided"]
        med = int(np.median(s["days"])) if s["days"] else -1
        print(f"  {y:>6} {100*s['pass']/n:>6.0f}% {100*s['fail']/n:>6.0f}% {100*s['undecided']/n:>6.0f}% {med:>17}")
        tp += s["pass"]; tf += s["fail"]; tu += s["undecided"]; allc += s["days"]
    n = tp + tf + tu
    print(f"  {'ALL':>6} {100*tp/n:>6.0f}% {100*tf/n:>6.0f}% {100*tu/n:>6.0f}% "
          f"{int(np.median(allc)) if allc else -1:>17}")
    return {"pass": tp, "fail": tf, "undecided": tu, "med_days": int(np.median(allc)) if allc else -1}


if __name__ == "__main__":
    run("core", "NR7 CORE only")
    run("aggr", "NR7 + reversion (AGGRESSIVE)")
