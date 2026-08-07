import sys, numpy as np, pandas as pd
sys.path.insert(0, '.')
from causal_engine import run_strategy, report, load_1m, resample_session
from causal_strategies import make_vwap_fade, make_nr7_breakout, make_orb

START = sys.argv[sys.argv.index('--start')+1] if '--start' in sys.argv else '2023-06-19'
RISK, CAP = 200.0, 40

def sessions(mkt):
    return sorted(set(load_1m(mkt, START, None)["day"]))

tests = [
    ("CONTROL: ES VWAP fade (known fake)", "es", 3, make_vwap_fade("es")),
    ("NR7 breakout CL (30m decision)",     "cl", 30, make_nr7_breakout("cl")),
    ("NR7 breakout ES (30m decision)",     "es", 30, make_nr7_breakout("es")),
    ("Opening-range breakout ES (5m)",     "es", 5, make_orb("es", 6)),
]
for label, mkt, bm, fn in tests:
    tr = run_strategy(mkt, bm, fn, RISK, CAP, START, None)
    report(tr, sessions(mkt), label)
