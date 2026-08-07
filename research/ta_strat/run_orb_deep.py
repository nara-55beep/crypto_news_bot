"""
run_orb_deep.py — adversarial validation of the ORB result. Question every way it could
be a fluke:
  1. TRUE HOLDOUT: pick params on first 60% only, report untouched last 40%.
  2. COST STRESS: 0,1,2,3,5 bps/side.
  3. OUTLIER TEST: what's ROI if you delete the top-3 winning trades? (real edge survives)
  4. PER-FOLD CONSISTENCY: is profit broad or one lucky window?
  5. LEVERAGE/DD: drawdown, liquidations, max consecutive losses, effective leverage.
"""
from __future__ import annotations
import sys, os, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind
import strategies as S
from backtest import StratParams, run_backtest

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
BPH = {"15m": 4, "5m": 12, "1m": 60}


def load(tf):
    df = pd.read_csv(os.path.join(CACHE, f"BTC_{tf}.csv"), index_col=0, parse_dates=True)
    return df[~df.index.duplicated(keep="last")].sort_index()


def bt(df, sig, atr, ov, cost, tf, risk=0.03, lev=10):
    p = StratParams(sizing="risk", risk_frac=risk, lev_cap=lev, slip_bps=cost,
                    funding_bps_8h=1.0, bars_per_hour=BPH[tf])
    for k, v in ov.items():
        setattr(p, k, v)
    return run_backtest(df, sig, atr, p)


CANDS = [(a, s, t) for a in (0, 8, 13) for s in (1.0, 1.5) for t in (1.5, 2.0, 2.5)]


def make(df, tf, anchor, stop, tgt):
    bm = {"15m": 15, "5m": 5, "1m": 1}[tf]
    return S.orb(df, or_minutes=30, anchor_hour=anchor, vol_mult=1.3,
                 atr_stop=stop, target_r=tgt, bar_min=bm)


def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else "15m"
    df = load(tf)
    atr = ind.atr(df["high"], df["low"], df["close"], 14)
    n = len(df); split = int(n * 0.6)
    train = df.iloc[:split]; holdout = df.iloc[split:]
    atr_tr = atr.iloc[:split].values; atr_ho = atr.iloc[split:].values
    print(f"{tf}: train {train.index[0].date()}->{train.index[-1].date()} | "
          f"HOLDOUT {holdout.index[0].date()}->{holdout.index[-1].date()} (untouched)\n")

    # 1. pick best params on TRAIN only (at 1 bps)
    best = None
    for (a, s, t) in CANDS:
        sig_full, ov = make(df, tf, a, s, t)
        sig_tr = pd.Series(sig_full, index=df.index).iloc[:split].values
        _, _, m = bt(train, sig_tr, atr_tr, ov, 1.0, tf)
        if m.get("n_trades", 0) >= 10:
            sc = m["roi"]
            if best is None or sc > best[0]:
                best = (sc, a, s, t, sig_full, ov)
    _, a, s, t, sig_full, ov = best
    print(f"Best-on-TRAIN params: anchor={a} UTC, stop={s}ATR, target={t}R  (train ROI {best[0]*100:.0f}%)\n")

    sig_ho = pd.Series(sig_full, index=df.index).iloc[split:].values

    # 2. COST STRESS on holdout
    print("HOLDOUT cost stress:")
    print(f"  {'cost':>4} | {'trades':>6}{'win%':>6}{'ROI%':>8}{'PF':>6}{'maxDD%':>7}{'Sharpe':>7}{'liq':>4}")
    rows = {}
    for cost in (0.0, 1.0, 2.0, 3.0, 5.0):
        tr, eq, m = bt(holdout, sig_ho, atr_ho, ov, cost, tf)
        rows[cost] = (tr, m)
        if m.get("n_trades", 0):
            print(f"  {cost:>4.0f} | {m['n_trades']:>6}{m['win_rate']*100:>6.0f}{m['roi']*100:>8.0f}"
                  f"{str(m['profit_factor']):>6}{m['max_dd']*100:>7.0f}{m['sharpe']:>7}{m['liquidations']:>4}")

    # 3. OUTLIER TEST at 1 bps
    tr, m = rows[1.0]
    if len(tr):
        pnl = tr["pnl_frac"].values
        order = np.argsort(pnl)[::-1]
        eq_all = np.prod(1 + pnl) - 1
        eq_no3 = np.prod(1 + np.delete(pnl, order[:3])) - 1
        top3_share = (np.sort(tr["pnl_usd"].values)[::-1][:3].sum() /
                      tr[tr["pnl_usd"] > 0]["pnl_usd"].sum()) if (tr["pnl_usd"] > 0).any() else 0
        print(f"\nOUTLIER TEST @1bps: full ROI {eq_all*100:.0f}% | "
              f"drop top-3 winners -> {eq_no3*100:.0f}% | top-3 = {top3_share*100:.0f}% of gross profit")

    # 4. PER-FOLD consistency on holdout (split holdout into 4)
    print("\nPER-QUARTER of holdout @1bps:")
    q = len(holdout) // 4
    for i in range(4):
        sub = holdout.iloc[i*q:(i+1)*q]
        sig_sub = pd.Series(sig_full, index=df.index).reindex(sub.index).values
        atr_sub = atr.reindex(sub.index).values
        _, _, ms = bt(sub, sig_sub, atr_sub, ov, 1.0, tf)
        if ms.get("n_trades", 0):
            print(f"  Q{i+1} {sub.index[0].date()}->{sub.index[-1].date()}: "
                  f"{ms['n_trades']} tr, win {ms['win_rate']*100:.0f}%, ROI {ms['roi']*100:.0f}%")
        else:
            print(f"  Q{i+1}: no trades")

    # 5. LEVERAGE sensitivity on holdout @1bps
    print("\nLEVERAGE (holdout @1bps, risk-based sizing, cap varies):")
    for lev in (3, 5, 10, 20):
        tr, eq, m = bt(holdout, sig_ho, atr_ho, ov, 1.0, tf, lev=lev)
        avglev = tr["lev"].mean() if len(tr) else 0
        print(f"  cap {lev:>2}x (avg used {avglev:>4.1f}x): ROI {m['roi']*100:>6.0f}%  "
              f"maxDD {m['max_dd']*100:>5.0f}%  liq {m['liquidations']}")


if __name__ == "__main__":
    main()
