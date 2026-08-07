"""
nr7_paper_bot.py — the best-odds plan as a runnable PAPER BOT you can watch.
Strategy: NR7 narrow-range breakout on ES+NQ+CL, partial-managed (1/2 off at +1R, runner to 2R),
flat by close. Account: simulated $50k EOD-trailing Apex eval with the LOCK-THE-TRAIL SPRINT
(risk $big until the trail locks at peak >=$52,600, then $small to coast to +$3,000).
Mode 'core' = NR7 only (robust). Mode 'aggressive' = NR7 + NQ mean-reversion (faster, overfit-suspect).

Run it to (1) replay recent 1-month eval windows trade-by-trade, and (2) print the latest NR7 setups
the bot is watching right now. This is a faithful sim on the 3y data; wire to a live futures feed to
trade it forward.
"""
from __future__ import annotations
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut
from apex_strats2 import nr7_orb, vwap_fade, turtle_soup, eighty_twenty

RAW = {m: load_fut(m) for m in ["es", "nq", "cl"]}
START, TARGET, DD, BUF = 50000.0, 3000.0, 2500.0, 100.0
LOCK_PEAK = START + DD + BUF   # 52,600 -> floor locks at 50,100


def datestr(eday):
    return str((np.datetime64("1970-01-01") + np.timedelta64(int(eday), "D")).astype("datetime64[D]"))


def build(mode="core"):
    recs = []
    for m in ["es", "nq", "cl"]:
        for r in nr7_orb(RAW[m], m, manage="partial")[0]:
            r["_mkt"] = m; r["_strat"] = "NR7"; recs.append(r)
    if mode == "aggressive":
        for fn, nm in [(vwap_fade, "VWAP2s"), (turtle_soup, "TurtleSoup"), (eighty_twenty, "80-20")]:
            for r in fn(RAW["nq"], "nq", manage="partial")[0]:
                r["_mkt"] = "nq"; r["_strat"] = nm; recs.append(r)
    recs.sort(key=lambda r: (r["eday"], r["xday"]))
    return recs


def simulate_eval(recs, start_eday, big, small, horizon=30, verbose=False):
    """Replay one EOD eval from start_eday with lock-the-trail sprint. Returns (outcome, log)."""
    eq = peak = START; locked = False; floor = peak - DD; log = []; day0 = None
    for r in recs:
        if r["eday"] < start_eday:
            continue
        if day0 is None:
            day0 = r["eday"]
        if r["eday"] - start_eday > horizon:
            break
        Ruse = big if peak < LOCK_PEAK else small
        usd = r["pnl_R"] * Ruse
        eq += usd
        if eq > peak:
            peak = eq
            if not locked and peak >= LOCK_PEAK:
                locked = True; floor = START + BUF
            elif not locked:
                floor = peak - DD
        phase = "coast" if locked else "SPRINT"
        log.append((datestr(r["eday"]), r["_mkt"].upper(), r["_strat"], r["pnl_R"], usd, eq, phase))
        if eq <= floor:
            return ("BLOWUP", log)
        if eq >= START + TARGET:
            return ("PASS", log)
    return ("TOO_SLOW", log)


def replay_recent(recs, mode, big, small, n_windows=3):
    days = sorted(set(r["eday"] for r in recs))
    # start windows ~ every 21 trading days near the end of the data
    starts = days[-(21 * n_windows + 5)::21][:n_windows]
    print(f"\n=== REPLAY: last {n_windows} one-month eval windows  (mode={mode}, sprint ${big}/${small}) ===")
    for s in starts:
        outcome, log = simulate_eval(recs, s, big, small)
        if not log:
            continue
        endbal = log[-1][5]; days_used = 0
        # days from start to pass/last
        ed = sorted(set(l[0] for l in log))
        print(f"\n  Eval starting {datestr(s)} -> {outcome}  (final ${endbal:,.0f}, {len(log)} trades, {len(ed)} days)")
    # detailed trade log for the most recent window
    s = starts[-1]
    outcome, log = simulate_eval(recs, s, big, small)
    print(f"\n  --- trade-by-trade, eval starting {datestr(s)} ({mode}) ---")
    print(f"  {'date':<12}{'mkt':<5}{'setup':<11}{'R':>6}{'P&L$':>9}{'balance':>11}  phase")
    for (d, mk, st, R, usd, bal, ph) in log:
        print(f"  {d:<12}{mk:<5}{st:<11}{R:>+6.2f}{usd:>+9,.0f}{bal:>11,.0f}  {ph}")
    print(f"  RESULT: {outcome} at ${log[-1][5]:,.0f}")


def latest_setups(recs, k=8):
    print("\n=== LATEST NR7 setups the bot flagged (most recent in data) ===")
    nr7 = [r for r in recs if r["_strat"] == "NR7"][-k:]
    print(f"  {'date':<12}{'mkt':<5}{'R-mult':>8}{'P&L$@$600':>11}{'result'}")
    for r in nr7:
        res = "win" if r["pnl_R"] > 0 else "loss"
        print(f"  {datestr(r['eday']):<12}{r['_mkt'].upper():<5}{r['pnl_R']:>+8.2f}{r['pnl_R']*600:>+11,.0f}  {res}")


def main():
    print("NR7 PAPER BOT — best-odds Apex plan (EOD account + lock-the-trail sprint)\n" + "=" * 70)
    core = build("core"); aggr = build("aggressive")
    print(f"loaded: core NR7 = {len(core)} trades (ES+NQ+CL), aggressive = {len(aggr)} trades")
    replay_recent(core, "core (NR7 only)", 800, 200)
    replay_recent(aggr, "aggressive (NR7+NQ-MR)", 400, 150)
    latest_setups(core)


if __name__ == "__main__":
    main()
