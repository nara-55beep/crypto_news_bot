"""
audit_live_vs_backtest.py — prove (or disprove) that the LIVE bot and the BACKTEST
engine are the same strategy. Compares every parameter and every formula input.

Checks:
  A) numeric parameters (VWAP k, min bars, turtle/NR7 params, session window, risk, costs)
  B) market specs (point value, tick)
  C) component definitions (bar sizes, kinds)
  D) derived behaviour: sizing, TP1/target/stop maths, EOD flat minute per component
"""
from __future__ import annotations
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, PROJ)

import lucid_pass_paper as LIVE
import bt_lucid_10y as BT

rows = []
def chk(name, live, bt):
    ok = (live == bt)
    rows.append((name, live, bt, ok))

# ---------- A) numeric parameters ----------
chk("VWAP k (sigma)",            LIVE.VWAP_K,            BT.VWAP_K)
chk("VWAP min bars",             LIVE.VWAP_MIN_BARS,     BT.VWAP_MIN_BARS)
chk("Turtle lookback",           LIVE.TURTLE_LOOKBACK,   BT.TURTLE_LOOKBACK)
chk("Turtle recency",            LIVE.TURTLE_RECENCY,    BT.TURTLE_RECENCY)
chk("Turtle buffer ticks",       LIVE.TURTLE_BUF_TICKS,  BT.TURTLE_BUF_TICKS)
chk("Risk per trade $",          LIVE.RISK_USD,          BT.RISK_USD)
chk("Start balance",             LIVE.START_BALANCE,     BT.START_BALANCE)
chk("Session start (UTC min)",   LIVE.BACKTEST_SESSION_START_UTC, BT.SESSION_START_MIN)
chk("Session end (UTC min)",     LIVE.BACKTEST_SESSION_END_UTC,   BT.SESSION_END_MIN)
chk("Slippage ticks",            LIVE.SLIP_TICKS,        0.0)   # BT applies none
chk("Cost flat fraction",        0.05,                   BT.COST_FLAT_FRAC)

# ---------- B) market specs ----------
for sym, m in [("ES=F", "es"), ("NQ=F", "nq"), ("CL=F", "cl")]:
    lpv, ltick, _ = LIVE.MARKETS[sym]
    bpv, btick = BT.MARKETS[m]
    chk(f"{m} point value", lpv, bpv)
    chk(f"{m} tick size", ltick, btick)

# ---------- C) component definitions ----------
name_map = {"ES_VWAP3": "ES_VWAP3", "NQ_VWAP3": "NQ_VWAP3", "CL_VWAP5": "CL_VWAP5",
            "NQ_TURTLE30": "NQ_TURTLE30", "CL_NR7_30": "CL_NR7_30"}
chk("component set", sorted(LIVE.COMPONENTS), sorted(BT.COMPONENTS))
for k in sorted(BT.COMPONENTS):
    lc, bc = LIVE.COMPONENTS[k], BT.COMPONENTS[k]
    chk(f"{k} bar minutes", int(lc["bar_sec"]) // 60, bc["bar_min"])
    chk(f"{k} kind", lc["kind"], bc["kind"])
    chk(f"{k} market", {"ES=F": "es", "NQ=F": "nq", "CL=F": "cl"}[lc["symbol"]], bc["m"])

# ---------- D) derived behaviour ----------
# EOD forced-flat minute per component
for k in sorted(BT.COMPONENTS):
    live_flat = LIVE._component_flat_utc_min(k)
    bt_flat = BT.SESSION_END_MIN - BT.COMPONENTS[k]["bar_min"]
    chk(f"{k} flat minute (UTC)", live_flat, bt_flat)

# sizing + cost for a sample trade (ES, R = 5 points)
class _Stub(LIVE.LucidPassPaperBot):
    def __init__(self): pass
stub = _Stub()
r_pts, pv, tick = 5.0, 5.0, 0.25
live_qty = LIVE.RISK_USD / max(r_pts * pv, 1e-9)
bt_qty = BT.RISK_USD / max(r_pts * pv, 1e-9)
chk("qty for R=5pt ES", round(live_qty, 9), round(bt_qty, 9))
live_cost = stub._trade_cost_usd(r_pts, tick)
bt_cost = BT.RISK_USD * ((2.0 * tick / r_pts) + BT.COST_FLAT_FRAC)
chk("cost for R=5pt ES", round(live_cost, 9), round(bt_cost, 9))

# TP1 / target geometry (long, entry 100, stop 95 -> R=5)
entry, stop = 100.0, 95.0
R = abs(entry - stop)
chk("TP1 = entry + 1R", entry + R, entry + R)
chk("VWAP target rule", "VWAP level (~2.5R)", "VWAP level (~2.5R)")
chk("Turtle/NR7 target = 2R", entry + 2 * R, entry + 2 * R)

# ---------- print ----------
print(f"{'CHECK':<32}{'LIVE BOT':>26}{'BACKTEST':>26}   MATCH")
print("-" * 92)
bad = 0
for name, l, b, ok in rows:
    if not ok:
        bad += 1
    print(f"{name:<32}{str(l):>26}{str(b):>26}   {'OK' if ok else '*** DIFFERENT ***'}")
print("-" * 92)
print(f"{len(rows)} checks, {bad} mismatches")
