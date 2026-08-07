import argparse
import pathlib
import sys

import numpy as np


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research" / "ta_strat"
sys.path.insert(0, str(RESEARCH))

from apex_lib import load_fut
from apex_strats2 import gap_fade, nr7_orb, pdh_pdl_fade, stretch_orb, turtle_soup, vwap_fade, eighty_twenty
from bt_ict_sm_tf import resample


START_BAL = 50000.0
TARGET_EQ = 53000.0
DD = 2500.0
LOCK_PEAK = 52600.0
LOCK_FLOOR = 50100.0


def day_to_month(day):
    return str((np.datetime64("1970-01-01") + np.timedelta64(int(day), "D")).astype("datetime64[M]"))


def as_dollar(recs, label, risk):
    out = []
    for r in recs:
        x = dict(r)
        x["_src"] = label
        x["_usd"] = r["pnl_R"] * risk
        out.append(x)
    return out


def merge(*parts):
    recs = []
    for part in parts:
        recs.extend(part)
    recs.sort(key=lambda r: (r["eday"], r["xday"], r.get("_src", "")))
    return recs


def eval_months(recs):
    months = sorted({day_to_month(r["eday"]) for r in recs})
    rows = []
    for month in months:
        eq = START_BAL
        peak = START_BAL
        floor = START_BAL - DD
        locked = False
        passed = breached = False
        trades = 0
        for r in recs:
            if day_to_month(r["eday"]) != month:
                continue
            trades += 1
            eq += r["_usd"]
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
            if eq >= TARGET_EQ:
                passed = True
                break
        rows.append({"month": month, "passed": passed, "breached": breached, "pnl": eq - START_BAL, "trades": trades})
    return rows


def summary(name, rows):
    total = len(rows)
    passed = sum(r["passed"] for r in rows)
    breached = sum(r["breached"] for r in rows)
    worst = min(rows, key=lambda r: r["pnl"])
    fails = [r for r in rows if not r["passed"]]
    fail_text = ",".join(f"{r['month']}:{r['pnl']:.0f}" for r in fails[:12])
    print(f"{name:<45} pass={passed}/{total} breach={breached} worst={worst['month']}:{worst['pnl']:.0f} fails={fail_text}")
    return passed, -breached, sum(r["pnl"] for r in rows), worst["pnl"]


def build_streams():
    raw = {m: load_fut(m) for m in ["es", "nq", "cl"]}
    streams = {}
    d15 = {m: resample(raw[m], "15min") for m in raw}
    d5 = {m: resample(raw[m], "5min") for m in raw}
    d3 = {m: resample(raw[m], "3min") for m in raw}
    d1 = {m: resample(raw[m], "1min") for m in raw}

    streams["base_15m_nqmr_600"] = as_dollar(
        merge(
            vwap_fade(d15["nq"], "nq", manage="partial")[0],
            turtle_soup(d15["nq"], "nq", manage="partial")[0],
            eighty_twenty(d15["nq"], "nq", manage="partial")[0],
        ),
        "base_15m_nqmr_600",
        600.0,
    )
    streams["cl_gap_5m_2000"] = as_dollar(gap_fade(d5["cl"], "cl", manage="partial")[0], "cl_gap_5m_2000", 2000.0)
    streams["cl_pdhpdl_1m_1500"] = as_dollar(pdh_pdl_fade(d1["cl"], "cl", manage="partial")[0], "cl_pdhpdl_1m_1500", 1500.0)
    streams["nq_pdhpdl_5m_1250"] = as_dollar(pdh_pdl_fade(d5["nq"], "nq", manage="partial")[0], "nq_pdhpdl_5m_1250", 1250.0)
    streams["es_stretch_5m_2000"] = as_dollar(stretch_orb(d5["es"], "es", manage="partial")[0], "es_stretch_5m_2000", 2000.0)
    streams["nqmr_3m_1000"] = as_dollar(
        merge(
            vwap_fade(d3["nq"], "nq", manage="partial")[0],
            turtle_soup(d3["nq"], "nq", manage="partial")[0],
            eighty_twenty(d3["nq"], "nq", manage="partial")[0],
        ),
        "nqmr_3m_1000",
        1000.0,
    )
    streams["nr7_5m_500"] = as_dollar(
        merge(*(nr7_orb(d5[m], m, manage="partial")[0] for m in ["es", "nq", "cl"])),
        "nr7_5m_500",
        500.0,
    )
    return streams


def main():
    streams = build_streams()
    names = list(streams)
    rows = []
    for mask in range(1, 1 << len(names)):
        selected = [names[i] for i in range(len(names)) if mask & (1 << i)]
        recs = merge(*(streams[n] for n in selected))
        score = summary("+".join(selected), eval_months(recs))
        rows.append((score, selected))
    rows.sort(reverse=True)
    print("BEST")
    for score, selected in rows[:20]:
        recs = merge(*(streams[n] for n in selected))
        summary("+".join(selected), eval_months(recs))


if __name__ == "__main__":
    main()
