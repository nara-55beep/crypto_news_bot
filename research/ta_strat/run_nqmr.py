import sys, numpy as np
sys.path.insert(0, '.')
from causal_engine import run_strategy, report, load_1m
from nqmr_strategies import make_vwap_fade, make_turtle_soup, make_eighty_twenty

MKT, BM, RISK, CAP = 'nq', 15, 600.0, 40
NY_SESSION = (9*60+30, 16*60)          # 09:30-16:00 ET cash session (bot flattens 15:55)

sess = sorted(set(load_1m(MKT, None, None, NY_SESSION)["day"]))
print(f"### NQ 15m MEAN REVERSION — causal test ###")
print(f"    {len(sess)} sessions, {sess[0]} .. {sess[-1]}, ${RISK:.0f} risk, 40-micro cap\n")

parts = [
    ("VWAP 2-sigma fade",        make_vwap_fade(MKT)),
    ("Turtle Soup (20-session)", make_turtle_soup(MKT)),
    ("80-20 reversal",           make_eighty_twenty(MKT)),
]
all_tr = []
for label, fn in parts:
    tr = run_strategy(MKT, BM, fn, RISK, CAP, None, None, ny_session=NY_SESSION)
    report(tr, sess, label)
    all_tr += tr
all_tr.sort(key=lambda t: t["day"])
report(all_tr, sess, "FULL BUNDLE (all 3 combined)")
