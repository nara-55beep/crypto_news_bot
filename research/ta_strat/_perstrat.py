import sys, os
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath("research/ta_strat/x")))
sys.path.insert(0, "research/ta_strat")
from apex_lib import load_fut
from bt_ict_sm_tf import resample
from apex_strats2 import nr7_orb, vwap_fade, turtle_soup, eighty_twenty
RISK=400.0
RAW={m:load_fut(m) for m in ["es","nq","cl"]}
def dd(recs):
    recs=sorted(recs,key=lambda r:(r["eday"],r["xday"]))
    bal=50000.0;peak=bal;mdd=0.0
    for r in recs:
        bal+=r["pnl_R"]*RISK;peak=max(peak,bal);mdd=min(mdd,bal-peak)
    return bal-50000.0,mdd
for tname,rule in [("5m","5min")]:
    print(f"=== {tname} per-strategy 3y (fixed $400/trade) ===")
    blocks={}
    for m in ["es","nq","cl"]:
        df=resample(RAW[m],rule)
        blocks[f"nr7_{m}"]=nr7_orb(df,m,manage="partial")[0]
    dfn=resample(RAW["nq"],rule)
    blocks["vwap2s_nq"]=vwap_fade(dfn,"nq",manage="partial")[0]
    blocks["turtle_nq"]=turtle_soup(dfn,"nq",manage="partial")[0]
    blocks["8020_nq"]=eighty_twenty(dfn,"nq",manage="partial")[0]
    nr7all=[];mrall=[]
    for k,r in blocks.items():
        net,mdd=dd(r)
        print(f"  {k:<12} n={len(r):>4}  net ${net:+,.0f}  maxDD ${mdd:,.0f}")
        (nr7all if k.startswith("nr7") else mrall).extend(r)
    n,d=dd(nr7all); print(f"  --> NR7 only (ES+NQ+CL): n={len(nr7all)} net ${n:+,.0f} maxDD ${d:,.0f}")
    n,d=dd(mrall); print(f"  --> NQ-MR only:          n={len(mrall)} net ${n:+,.0f} maxDD ${d:,.0f}")
