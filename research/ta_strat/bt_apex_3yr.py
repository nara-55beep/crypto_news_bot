"""
bt_apex_3yr.py — backtest the ACTUAL Apex ES VWAP ORB paper bot over 3 YEARS of 5m data
(S&P 500 = ES proxy, Nasdaq = NQ peer) from Dukascopy. Same faithful replay as bt_apex.py
(drives the bot's own _tick), just over the big window, reported per calendar year.

Perf: the bot only ever uses the CURRENT day (daily VWAP/OR, no cross-day lookback), so we
feed it the current day's bars only (O(day) not O(history)); the NQ peer lookup is memoized.
"""
from __future__ import annotations
import os, sys
from collections import Counter
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import apex_vwap_paper as A

CACHE = os.path.join(ROOT, "research", "ta_strat", "cache")
NY = "America/New_York"


def load_prepared(name):
    df = pd.read_csv(os.path.join(CACHE, f"apex3yr_{name}.csv"))
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    return A._prepare(df).reset_index(drop=True)


def main():
    es = load_prepared("es"); nq = load_prepared("nq")
    print(f"[data] ES {len(es):,} RTH 5m bars  {es['dt_ny'].iloc[0].date()} -> {es['dt_ny'].iloc[-1].date()} "
          f"({es['day'].nunique()} trading days) | NQ {len(nq):,} bars", flush=True)

    # memoize the NQ time->row lookup (built once for the single full-nq object)
    A._row_by_time = (lambda _cache={}: (lambda df: _cache.setdefault(
        id(df), {r["dt_utc"]: r for _, r in df.iterrows()})))()

    bot = A.ApexVWAPPaperBot()
    bot._save = lambda *a, **k: None
    bot._note = lambda *a, **k: None
    bot.reset(); bot.enabled = True
    bot._nq = nq

    trades = []          # (close_date, rec)
    eq_pts = []          # (date, equity) — close-day equity for drawdown
    n_before = 0
    for day, day_df in es.groupby("day", sort=True):
        day_df = day_df.reset_index(drop=True)
        for j in range(len(day_df)):
            bot._es = day_df.iloc[: j + 1]
            before = len(bot.history)
            bot._tick()
            if len(bot.history) > before:
                for k in range(len(bot.history) - before):
                    trades.append((day, bot.history[k]))
        eq_pts.append((day, bot.balance))

    if bot.pos is not None:
        bot._close(bot.price, "end_of_data")
        trades.append((es["day"].iloc[-1], bot.history[0]))
    report(trades, eq_pts, es)


def _yr_metrics(recs):
    pnl = np.array([r["pnl"] for r in recs], float)
    bal = A.START_BALANCE + np.cumsum(pnl)
    eq = np.concatenate([[A.START_BALANCE], bal])
    peak = np.maximum.accumulate(eq); dd = (eq - peak).min()
    wins = pnl[pnl > 0]
    pf = wins.sum() / abs(pnl[pnl < 0].sum()) if (pnl < 0).any() else float("inf")
    return len(recs), (pnl > 0).mean(), pnl.sum(), dd, pf


def report(trades, eq_pts, es):
    days = es["day"].nunique()
    print("\n" + "=" * 88)
    print("Apex ES VWAP ORB (paper) — 3-YEAR BACKTEST (faithful replay; S&P=ES, Nasdaq=NQ peer)")
    print("=" * 88)
    if not trades:
        print("NO TRADES."); return
    allrecs = [r for _, r in trades]
    n, win, profit, dd, pf = _yr_metrics(allrecs)
    # continuous trailing-drawdown (Apex $2k rule) from the day-close equity curve
    eqv = np.array([A.START_BALANCE] + [e[1] for e in eq_pts], float)
    peak = np.maximum.accumulate(eqv); worst_tdd = (eqv - peak).min()
    print(f"window           : {es['dt_ny'].iloc[0].date()} -> {es['dt_ny'].iloc[-1].date()}  ({days} trading days, ~3y)")
    print(f"account          : ${A.START_BALANCE:,.0f} Apex-style, 1 ES contract, costs incl (1tk slip + $1.24 RT)")
    print(f"TOTAL trades     : {n}   ({n/days:.2f}/day)")
    print(f"win rate         : {win*100:.1f}%")
    print(f"net profit       : ${profit:+,.2f}")
    print(f"ROI (on $50k)    : {profit/A.START_BALANCE*100:+.1f}%   over ~3y")
    print(f"profit factor    : {pf:.2f}")
    print(f"max drawdown     : ${worst_tdd:,.0f}  ({worst_tdd/A.START_BALANCE*100:.1f}% of $50k)")
    print(f"exit reasons     : {dict(Counter(r['reason'] for r in allrecs))}")
    print(f"Apex $2k trailing-DD ever breached (account fail)? "
          f"{'YES' if worst_tdd <= -A.MAX_EOD_DD else 'no'}")
    print(f"\n{'year':<8}{'trades':>8}{'win%':>7}{'profit$':>12}{'maxDD$':>11}{'PF':>7}")
    by_year = {}
    for d, r in trades:
        by_year.setdefault(pd.Timestamp(d).year, []).append(r)
    for y in sorted(by_year):
        n2, w2, p2, dd2, pf2 = _yr_metrics(by_year[y])
        print(f"{y:<8}{n2:>8}{w2*100:>6.0f}{p2:>12,.0f}{dd2:>11,.0f}{pf2:>7.2f}")
    pd.DataFrame(allrecs).to_csv(os.path.join(CACHE, "apex3yr_trades.csv"), index=False)
    print(f"\nsaved {n} trades -> cache/apex3yr_trades.csv")


if __name__ == "__main__":
    main()
