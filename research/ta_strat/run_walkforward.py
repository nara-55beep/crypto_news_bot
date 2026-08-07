"""
run_walkforward.py — gold-standard anti-overfit test. Rolling walk-forward: on each
TRAIN window pick the best params for a strategy family, then trade them on the next
unseen TEST window. Concatenate all TEST trades -> the only honest performance estimate.

Focus on the only families with a theoretical prayer intraday (low-frequency / structural),
evaluated at the user's REAL cost (Lighter ~0 fee; market order pays ~half-spread). We
test 0 bps (pure maker / zero-fee ideal) and 1 bps (realistic maker-ish) per side.

A family "passes" only if concatenated TEST ROI > 0 at >=1 bps with >=30 test trades.
"""
from __future__ import annotations
import sys, os, warnings, itertools
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind
import strategies as S
from backtest import StratParams, run_backtest

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
BAR_MIN = {"15m": 15, "5m": 5, "1m": 1}
BPH = {"15m": 4, "5m": 12, "1m": 60}


def load(tf):
    df = pd.read_csv(os.path.join(CACHE, f"BTC_{tf}.csv"), index_col=0, parse_dates=True)
    return df[~df.index.duplicated(keep="last")].sort_index()


def eval_signal(df, sig, atr, overrides, cost, tf):
    p = StratParams(sizing="risk", risk_frac=0.03, lev_cap=10, slip_bps=cost,
                    funding_bps_8h=1.0, bars_per_hour=BPH[tf])
    for k, v in overrides.items():
        setattr(p, k, v)
    tr, eq, m = run_backtest(df, sig, atr, p)
    return tr, m


# parameter grids per family (kept SMALL to avoid grid-overfit)
def family_grid(name, df, tf):
    """yield (param_label, signal_array, overrides) for each grid point."""
    bm = BAR_MIN[tf]
    if name == "orb":
        for anchor in (0, 8, 13):
            for stop in (1.0, 1.5):
                for tr_ in (1.5, 2.5):
                    sig, ov = S.orb(df, or_minutes=30, anchor_hour=anchor,
                                    vol_mult=1.3, atr_stop=stop, target_r=tr_, bar_min=bm)
                    yield (f"a{anchor}_s{stop}_t{tr_}", sig, ov)
    elif name == "session_mom":
        for anchor in (0, 8, 13):
            sig, ov = S.session_momentum(df, anchor_hour=anchor, bar_min=bm)
            yield (f"anchor{anchor}", sig, ov)
    elif name == "donchian_adx":
        for n in (20, 55):
            for adxmin in (20, 25):
                sig, ov = S.donchian_breakout(df, n=n, trend_ema=200)
                # add adx filter
                adx_, _, _ = ind.adx(df["high"], df["low"], df["close"], 14)
                sig = sig * (adx_.values > adxmin)
                yield (f"n{n}_adx{adxmin}", sig, ov)
    elif name == "tsmom_hold":
        for lb in (12, 24, 48):
            for hold in (24, 48, 96):
                sig, ov = S.intraday_tsmom(df, lookback=lb, hold_bars=hold)
                yield (f"lb{lb}_h{hold}", sig, ov)


def walk_forward(tf, name, train_days=120, test_days=45, cost_train=1.0):
    df_full = load(tf)
    atr_full = ind.atr(df_full["high"], df_full["low"], df_full["close"], 14)
    start = df_full.index[0]; end = df_full.index[-1]
    # rolling folds
    folds = []
    t0 = start + pd.Timedelta(days=train_days)
    while t0 + pd.Timedelta(days=test_days) <= end:
        folds.append((t0 - pd.Timedelta(days=train_days), t0, t0 + pd.Timedelta(days=test_days)))
        t0 += pd.Timedelta(days=test_days)
    results = {0.0: [], 1.0: []}
    for (tr_start, split, te_end) in folds:
        train = df_full[(df_full.index >= tr_start) & (df_full.index < split)]
        test = df_full[(df_full.index >= split) & (df_full.index < te_end)]
        if len(train) < 200 or len(test) < 50:
            continue
        atr_tr = atr_full.reindex(train.index).values
        atr_te = atr_full.reindex(test.index).values
        # pick best param on TRAIN (by ROI at cost_train)
        best = None
        for label, sig_full, ov in family_grid(name, df_full, tf):
            sig_tr = pd.Series(sig_full, index=df_full.index).reindex(train.index).values
            trd, m = eval_signal(train, sig_tr, atr_tr, ov, cost_train, tf)
            score = m.get("roi", -99) if m.get("n_trades", 0) >= 3 else -99
            if best is None or score > best[0]:
                best = (score, label, sig_full, ov)
        if best is None:
            continue
        _, label, sig_full, ov = best
        sig_te = pd.Series(sig_full, index=df_full.index).reindex(test.index).values
        for cost in (0.0, 1.0):
            trd, m = eval_signal(test, sig_te, atr_te, ov, cost, tf)
            if m.get("n_trades", 0) > 0:
                results[cost].append((m["roi"], m["n_trades"], m["win_rate"], label))
    return results


def aggregate(results):
    """compound test-fold ROIs (sequential), sum trades."""
    out = {}
    for cost, folds in results.items():
        if not folds:
            out[cost] = None; continue
        eq = 1.0; ntr = 0; wsum = 0
        for roi, n, wr, lab in folds:
            eq *= (1 + roi); ntr += n; wsum += wr * n
        out[cost] = dict(roi=eq - 1, n=ntr, win=wsum / ntr if ntr else 0, folds=len(folds))
    return out


def main():
    tfs = sys.argv[1:] or ["15m", "5m"]
    families = ["orb", "session_mom", "donchian_adx", "tsmom_hold"]
    print("WALK-FORWARD (train 120d -> test 45d, rolling). Best param picked on train,")
    print("traded on unseen test. Aggregated TEST performance is the honest estimate.\n")
    passed = []
    for tf in tfs:
        if not os.path.exists(os.path.join(CACHE, f"BTC_{tf}.csv")):
            continue
        print(f"{'='*92}\n{tf}\n{'='*92}")
        print(f"{'family':<16}{'folds':>6} | {'TEST @0bps':>26} | {'TEST @1bps':>26}")
        print(f"{'':<16}{'':>6} | {'ROI%':>8}{'trades':>8}{'win%':>8} | {'ROI%':>8}{'trades':>8}{'win%':>8}")
        for fam in families:
            try:
                res = walk_forward(tf, fam)
            except Exception as e:
                print(f"  {fam:<14} ERROR {type(e).__name__}: {e}"); continue
            agg = aggregate(res)
            a0, a1 = agg.get(0.0), agg.get(1.0)
            def f(a):
                return (f"{a['roi']*100:>8.0f}{a['n']:>8}{a['win']*100:>8.0f}" if a
                        else f"{'--':>8}{'--':>8}{'--':>8}")
            nf = a1['folds'] if a1 else (a0['folds'] if a0 else 0)
            print(f"  {fam:<14}{nf:>6} | {f(a0)} | {f(a1)}")
            if a1 and a1["roi"] > 0 and a1["n"] >= 30:
                passed.append((tf, fam, a1))
    print(f"\n{'='*70}\nWALK-FORWARD SURVIVORS (test ROI>0 at 1bps, >=30 trades):")
    if passed:
        for tf, fam, a in passed:
            print(f"  {fam} ({tf}): OOS ROI {a['roi']*100:.0f}%, {a['n']} trades, win {a['win']*100:.0f}%")
    else:
        print("  NONE. No intraday family survives honest walk-forward at realistic cost.")


if __name__ == "__main__":
    main()
