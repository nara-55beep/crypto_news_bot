import sys, numpy as np
sys.path.insert(0, '.')
from causal_engine import run_strategy, report, load_1m
from crabel_strategies import make_crabel_orb, make_nr_breakout, make_id_nr4

MKT = sys.argv[sys.argv.index('--market')+1] if '--market' in sys.argv else 'cl'
START = sys.argv[sys.argv.index('--start')+1] if '--start' in sys.argv else None
RISK, CAP, BM = 200.0, 40, 1     # 1-min decisions => orders rest from just after the open

sess = sorted(set(load_1m(MKT, START, None)["day"]))
print(f"### CRABEL on {MKT.upper()} — {len(sess)} sessions ({sess[0]} .. {sess[-1]}) ###")

tests = [
    ("ORB stretch(10), no filter",        make_crabel_orb(MKT, 10, "none")),
    ("ORB stretch(10) + NR4 filter",      make_crabel_orb(MKT, 10, "nr4")),
    ("ORB stretch(10) + NR7 filter",      make_crabel_orb(MKT, 10, "nr7")),
    ("ORB stretch(10) + ID/NR4 filter",   make_crabel_orb(MKT, 10, "id_nr4")),
    ("ORB stretch(10) + 2BarNR filter",   make_crabel_orb(MKT, 10, "nr2")),
    ("ORB stretch(5), no filter",         make_crabel_orb(MKT, 5, "none")),
    ("NR4 range breakout",                make_nr_breakout(MKT, 4)),
    ("NR7 range breakout",                make_nr_breakout(MKT, 7)),
    ("ID/NR4 range breakout",             make_id_nr4(MKT)),
]
for label, fn in tests:
    tr = run_strategy(MKT, BM, fn, RISK, CAP, START, None)
    report(tr, sess, label)
