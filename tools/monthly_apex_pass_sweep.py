import argparse
import itertools
import os
import pathlib
import sys
from collections import defaultdict

import numpy as np


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research" / "ta_strat"
sys.path.insert(0, str(RESEARCH))

from apex_lib import load_fut
from apex_strats2 import (
    dual_thrust,
    eighty_twenty,
    gap_fade,
    hourly_reversion,
    lw_breakout,
    nr7_orb,
    pdh_pdl_fade,
    pivot_fade,
    sigma_intraday,
    stretch_orb,
    turtle_soup,
    vwap_fade,
)
from bt_ict_sm_tf import resample


START_BAL = 50000.0
TARGET = 3000.0
DD = 2500.0
LOCK_PEAK = START_BAL + DD + 100.0
LOCK_FLOOR = START_BAL + 100.0


def day_to_month(day):
    return str((np.datetime64("1970-01-01") + np.timedelta64(int(day), "D")).astype("datetime64[M]"))


def tag(recs, label):
    out = []
    for r in recs:
        x = dict(r)
        x["_src"] = label
        out.append(x)
    return out


def merge(*parts):
    recs = []
    for part in parts:
        recs.extend(part)
    recs.sort(key=lambda r: (r["eday"], r["xday"]))
    return recs


def monthly_eval(recs, risk, big=None, small=None, switch=2600.0):
    months = sorted({day_to_month(r["eday"]) for r in recs})
    rows = []
    for month in months:
        month_recs = [r for r in recs if day_to_month(r["eday"]) == month]
        eq = START_BAL
        peak = START_BAL
        floor = START_BAL - DD
        locked = False
        passed = False
        breached = False
        pass_day = None
        for r in month_recs:
            risk_use = big if big is not None and peak < START_BAL + switch else (small if big is not None else risk)
            eq += r["pnl_R"] * risk_use
            if eq > peak:
                peak = eq
                if not locked:
                    if peak >= LOCK_PEAK:
                        locked = True
                        floor = LOCK_FLOOR
                    else:
                        floor = peak - DD
            if eq <= floor:
                breached = True
                break
            if eq >= START_BAL + TARGET:
                passed = True
                pass_day = r["xday"]
                break
        pnl = eq - START_BAL
        rows.append({
            "month": month,
            "passed": passed,
            "breached": breached,
            "pnl": pnl,
            "trades": len(month_recs),
            "pass_day": pass_day,
        })
    return rows


def score_rows(rows):
    pass_count = sum(1 for r in rows if r["passed"])
    breach_count = sum(1 for r in rows if r["breached"])
    total_pnl = sum(r["pnl"] for r in rows)
    worst = min((r["pnl"] for r in rows), default=0.0)
    min_trades = min((r["trades"] for r in rows), default=0)
    return pass_count, -breach_count, total_pnl, worst, min_trades


def make_candidates(raw, tfs):
    candidates = []
    for tf_name, rule in tfs:
        dfs = {m: resample(raw[m], rule) for m in raw}
        nr7 = merge(*(tag(nr7_orb(dfs[m], m, manage="partial")[0], f"{m}:nr7") for m in ["es", "nq", "cl"]))
        nq_mr = merge(
            tag(vwap_fade(dfs["nq"], "nq", manage="partial")[0], "nq:vwap2"),
            tag(turtle_soup(dfs["nq"], "nq", manage="partial")[0], "nq:turtle"),
            tag(eighty_twenty(dfs["nq"], "nq", manage="partial")[0], "nq:80-20"),
        )
        candidates.append((f"{tf_name}:nr7", nr7))
        candidates.append((f"{tf_name}:nr7+nq_mr", merge(nr7, nq_mr)))
        candidates.append((f"{tf_name}:nq_mr", nq_mr))

        broad_parts = [nr7, nq_mr]
        for m in ["es", "nq", "cl"]:
            df = dfs[m]
            families = [
                ("lw", lw_breakout(df, m, manage="partial")[0]),
                ("dual", dual_thrust(df, m, manage="partial")[0]),
                ("stretch", stretch_orb(df, m, manage="partial")[0]),
                ("stretch_nr7", stretch_orb(df, m, nr7_only=True, manage="partial")[0]),
                ("gap", gap_fade(df, m, manage="partial")[0]),
                ("pivot", pivot_fade(df, m, manage="partial")[0]),
                ("pdhpdl", pdh_pdl_fade(df, m, manage="partial")[0]),
                ("hourly", hourly_reversion(df, m, manage="partial")[0]),
                ("sigma", sigma_intraday(df, m, manage="partial")[0]),
            ]
            for name, recs in families:
                part = tag(recs, f"{m}:{name}")
                broad_parts.append(part)
                candidates.append((f"{tf_name}:{m}:{name}", part))
        candidates.append((f"{tf_name}:all_broad", merge(*broad_parts)))
    return candidates


def print_result(label, mode, rows):
    pass_count = sum(1 for r in rows if r["passed"])
    breach_count = sum(1 for r in rows if r["breached"])
    total = len(rows)
    total_pnl = sum(r["pnl"] for r in rows)
    worst = min(rows, key=lambda r: r["pnl"]) if rows else None
    fail_months = [r for r in rows if not r["passed"]]
    fail_text = ",".join(f"{r['month']}:{r['pnl']:.0f}" for r in fail_months[:8])
    print(
        f"{label:<28} {mode:<18} pass={pass_count:>2}/{total:<2} breach={breach_count:<2} "
        f"pnl_sum={total_pnl:>8.0f} worst={worst['month'] if worst else ''}:{worst['pnl'] if worst else 0:>7.0f} "
        f"fails={fail_text}"
    )


def main():
    parser = argparse.ArgumentParser(description="Calendar-month Apex pass sweep across local futures strategies.")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--risks", default="300,400,500,600,800,1000,1250,1500,2000")
    parser.add_argument("--sprints", default="600:150,800:200,1000:200,1250:250,1500:300")
    parser.add_argument("--months", default="", help="Comma-separated months to print best candidates for, e.g. 2023-07,2026-01.")
    args = parser.parse_args()

    raw = {m: load_fut(m) for m in ["es", "nq", "cl"]}
    tfs = [("1m", "1min"), ("3m", "3min"), ("5m", "5min"), ("15m", "15min")]
    candidates = make_candidates(raw, tfs)
    risks = [float(x) for x in args.risks.split(",") if x]
    sprints = []
    for raw_pair in args.sprints.split(","):
        if not raw_pair:
            continue
        big, small = raw_pair.split(":")
        sprints.append((float(big), float(small)))

    results = []
    for label, recs in candidates:
        if not recs:
            continue
        for risk in risks:
            rows = monthly_eval(recs, risk)
            results.append((score_rows(rows), label, f"flat{risk:.0f}", rows))
        for big, small in sprints:
            rows = monthly_eval(recs, small, big=big, small=small)
            results.append((score_rows(rows), label, f"sprint{big:.0f}/{small:.0f}", rows))
    results.sort(key=lambda x: x[0], reverse=True)
    print("MONTHLY_APEX_PASS_SWEEP start=50000 target=3000 dd=2500")
    for _, label, mode, rows in results[: args.top]:
        print_result(label, mode, rows)
    perfect = [x for x in results if x[0][0] == len(x[3]) and x[0][1] == 0]
    print(f"PERFECT count={len(perfect)}")
    if perfect:
        for _, label, mode, rows in perfect[:10]:
            print_result("PERFECT " + label, mode, rows)
    focus_months = [x for x in args.months.split(",") if x]
    for month in focus_months:
        month_rows = []
        for _, label, mode, rows in results:
            row = next((r for r in rows if r["month"] == month), None)
            if row:
                month_rows.append((row["passed"], -row["breached"], row["pnl"], label, mode, row))
        month_rows.sort(reverse=True)
        print(f"MONTH_FOCUS {month}")
        for passed, _, pnl, label, mode, row in month_rows[:20]:
            print(
                f"  {label:<28} {mode:<18} passed={row['passed']} breached={row['breached']} "
                f"pnl={pnl:>8.0f} trades={row['trades']}"
            )


if __name__ == "__main__":
    main()
