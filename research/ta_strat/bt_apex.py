"""
bt_apex.py — backtest the ACTUAL "Apex ES VWAP ORB (paper)" bot exactly as it trades.

It drives apex_vwap_paper.ApexVWAPPaperBot's own _tick() bar-by-bar over the same ES=F /
NQ=F 5m data the live bot uses (Yahoo, ~60 days max for 5m). No reimplementation; the bot's
own OR-break -> pullback -> rejection logic, NQ/VWAP/EMA confirmation, 2.5R target, 1R trail,
EOD-flat, $50k Apex rules, slippage (1 tick) and commission ($1.24 RT) all apply as-is.

Replay = feed the bot the prepared ES df truncated to each successive completed bar and call
_tick(), reproducing the live poll loop (which refetches the full df and acts on the newest
completed bar). NQ is sliced to <= the current bar (peer lookup is by exact timestamp -> no
look-ahead). Indicators are causal (EMA/ATR/VWAP-cumulative), so slicing the prepared df is
correct as-of each bar.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import apex_vwap_paper as A


def load():
    es = A._prepare(A._fetch_yahoo(A.ES_SYMBOL))
    nq = A._prepare(A._fetch_yahoo(A.NQ_SYMBOL))
    return es.reset_index(drop=True), nq.reset_index(drop=True)


def main():
    es, nq = load()
    print(f"[data] ES {len(es)} RTH 5m bars  {es['dt_ny'].iloc[0]} -> {es['dt_ny'].iloc[-1]}  "
          f"({es['day'].nunique()} trading days) | NQ {len(nq)} bars", flush=True)

    bot = A.ApexVWAPPaperBot()
    bot._save = lambda *a, **k: None
    bot._note = lambda *a, **k: None
    bot.reset(); bot.enabled = True

    # Peer lookup in _scan_signal is by EXACT timestamp (never future NQ bars), and NQ
    # indicators are causal, so passing the full prepared NQ frame introduces no look-ahead.
    bot._nq = nq
    eq_curve = [(es["dt_utc"].iloc[0], A.START_BALANCE)]
    close_dates = []                          # real bar date each time a trade closes
    for i in range(len(es)):
        cur_ts = es["dt_utc"].iloc[i]
        bot._es = es.iloc[: i + 1]
        before = len(bot.history)
        bot._tick()
        for _ in range(len(bot.history) - before):
            close_dates.append(es["dt_ny"].iloc[i].date())
        eq_curve.append((cur_ts, bot.equity()))
    bot._close_dates = close_dates

    if bot.pos is not None:
        bot._close(bot.price, "end_of_data")
    report(bot, es, eq_curve)


def report(bot, es, eq_curve):
    trades = list(reversed(bot.history))     # chronological
    days = es["day"].nunique()
    start = A.START_BALANCE
    print("\n" + "=" * 80)
    print("Apex ES VWAP ORB (paper) — BACKTEST (faithful replay of the actual bot)")
    print("=" * 80)
    if not trades:
        print("NO TRADES over the window."); return
    pnl = np.array([t["pnl"] for t in trades], float)
    bal = start + np.cumsum(pnl)
    eqv = np.array([e[1] for e in eq_curve], float)
    peak = np.maximum.accumulate(eqv); ddpct = ((eqv - peak) / peak).min()
    wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    final = bal[-1]
    # Apex prop rules
    hit_target = bool((bal >= A.TARGET_BALANCE).any())
    eod_dd_breach = bool((eqv <= start - A.MAX_EOD_DD).any())
    from collections import Counter
    print(f"window           : {es['dt_ny'].iloc[0].date()} -> {es['dt_ny'].iloc[-1].date()}  "
          f"({days} trading days)  [Yahoo 5m max ~60d]")
    print(f"account          : ${start:,.0f} Apex-style (target ${A.TARGET_BALANCE:,.0f}, EOD DD ${A.MAX_EOD_DD:,.0f})")
    print(f"trades           : {len(trades)}   ({len(trades)/days:.2f}/day)")
    print(f"win rate         : {len(wins)/len(trades)*100:.1f}%")
    print(f"net profit       : ${final-start:+,.2f}")
    print(f"ROI (on $50k)    : {(final/start-1)*100:+.2f}%")
    print(f"final balance    : ${final:,.2f}")
    print(f"max drawdown     : {ddpct*100:.2f}%  (${(eqv-peak).min():,.0f})")
    print(f"profit factor    : {pf:.2f}")
    print(f"avg trade        : ${pnl.mean():+,.2f}   best ${pnl.max():+,.2f}  worst ${pnl.min():+,.2f}")
    print(f"exit reasons     : {dict(Counter(t['reason'] for t in trades))}")
    print(f"\nApex prop check  : pass target ${A.TARGET_BALANCE:,.0f}? {'YES' if hit_target else 'no'}   "
          f"| blew $2k EOD drawdown? {'YES (account failed)' if eod_dd_breach else 'no'}")
    cd = getattr(bot, "_close_dates", [])
    if cd:
        wk = Counter(str(pd.Timestamp(d).to_period("W")) for d in cd)
        print(f"trades by week   : {dict(sorted(wk.items()))}")
        print(f"active weeks     : {len(wk)} of ~{days//5} (spread vs clustered)")
    pd.DataFrame(trades).to_csv(os.path.join(ROOT, "research", "ta_strat", "cache", "apex_bt_trades.csv"), index=False)
    print(f"saved {len(trades)} trades -> cache/apex_bt_trades.csv")


if __name__ == "__main__":
    main()
