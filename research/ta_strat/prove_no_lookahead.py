"""
prove_no_lookahead.py — empirical proof that the engine cannot see the future.

Method (the standard "future corruption" test):
  1. Run a strategy over the full history and record its trades.
  2. Re-run with every bar AFTER a cutoff date replaced by garbage (prices shuffled and
     scaled). If any decision peeked ahead, results BEFORE the cutoff would change.
  3. Trades before the cutoff must be byte-identical.

If they match, the strategy genuinely only used information available at the time -
exactly like receiving live bars one at a time.
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import causal_engine as CE
from crabel_strategies import make_crabel_orb, make_nr_breakout

CUTOFF = "2025-01-01"


def corrupt_after(df: pd.DataFrame, cutoff: str) -> pd.DataFrame:
    """Destroy all information after the cutoff (keeps shape/times, ruins prices)."""
    d = df.copy()
    m = d["dt_utc"] >= pd.Timestamp(cutoff, tz="UTC")
    if not m.any():
        return d
    rng = np.random.default_rng(0)
    n = int(m.sum())
    base = float(d.loc[m, "close"].iloc[0])
    noise = base * (1.0 + rng.normal(0, 0.25, n))       # nonsense prices
    d.loc[m, "open"] = noise
    d.loc[m, "high"] = noise * 1.01
    d.loc[m, "low"] = noise * 0.99
    d.loc[m, "close"] = noise
    return d


def main():
    market = sys.argv[sys.argv.index("--market") + 1] if "--market" in sys.argv else "cl"
    real_loader = CE.load_1m

    def run_with(loader):
        CE.load_1m = loader
        try:
            return CE.run_strategy(market, 1, make_nr_breakout(market, 7), 200.0, 40,
                                   "2023-06-19", None)
        finally:
            CE.load_1m = real_loader

    print(f"market {market}, cutoff {CUTOFF}")
    a = run_with(real_loader)
    b = run_with(lambda mk, s=None, e=None, ns=None: corrupt_after(real_loader(mk, s, e, ns), CUTOFF))

    cut = pd.Timestamp(CUTOFF).date()
    a_pre = [t for t in a if t["day"] < cut]
    b_pre = [t for t in b if t["day"] < cut]

    print(f"  trades before cutoff: clean run {len(a_pre)}, corrupted-future run {len(b_pre)}")
    if len(a_pre) != len(b_pre):
        print("  *** LOOK-AHEAD DETECTED: trade counts differ ***"); return
    diffs = 0
    for x, y in zip(a_pre, b_pre):
        if (x["day"] != y["day"] or abs(x["entry"] - y["entry"]) > 1e-9
                or abs(x["pnl"] - y["pnl"]) > 1e-9 or x["reason"] != y["reason"]):
            diffs += 1
    print(f"  differing trades: {diffs}")
    print("  PASS - destroying the future changed nothing before the cutoff."
          if diffs == 0 else "  *** LOOK-AHEAD DETECTED ***")
    print(f"  (after the cutoff the runs naturally diverge: {len(a)-len(a_pre)} vs {len(b)-len(b_pre)} trades)")


if __name__ == "__main__":
    main()
