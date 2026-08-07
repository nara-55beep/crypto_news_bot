"""
run_screen.py — first-pass screen: every strategy x every timeframe x a COST GRID,
with an out-of-sample split. Realistic leverage sizing. Prints which strategies have
a real, cost-robust, OOS-surviving edge (almost none, per prior research — we verify).

Usage: venv/Scripts/python.exe research/ta_strat/run_screen.py [tf...]
"""
from __future__ import annotations
import sys, os, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind
import strategies as S
from backtest import StratParams, run_backtest, fmt_metrics

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
BAR_MIN = {"15m": 15, "5m": 5, "1m": 1}
BARS_PER_HOUR = {"15m": 4, "5m": 12, "1m": 60}
TEST_DAYS = 365
COST_GRID = [0.0, 1.0, 2.0, 3.0, 5.0]      # bps per side
RISK_FRAC = 0.03
LEV_CAP = 20.0


def load(tf):
    df = pd.read_csv(os.path.join(CACHE, f"BTC_{tf}.csv"), index_col=0, parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def run_strategy_grid(name, fn, kwargs, df, atr, tf, oos_frac=0.30):
    bar_min = BAR_MIN[tf]
    # inject bar_min for session/orb strategies that need it
    import inspect
    sig_params = inspect.signature(fn).parameters
    kw = dict(kwargs)
    if "bar_min" in sig_params:
        kw["bar_min"] = bar_min
    signal, overrides = fn(df, **kw)
    rows = []
    n = len(df)
    split_i = int(n * (1 - oos_frac))
    for cost in COST_GRID:
        p = StratParams(sizing="risk", risk_frac=RISK_FRAC, lev_cap=LEV_CAP,
                        slip_bps=cost, funding_bps_8h=1.0,
                        bars_per_hour=BARS_PER_HOUR[tf])
        for k, v in overrides.items():
            setattr(p, k, v)
        tr, eq, m = run_backtest(df, signal, atr, p)
        # OOS slice: trades entered after split date
        if len(tr):
            split_t = df.index[split_i]
            tr_oos = tr[tr["entry_time"] >= split_t]
            oos_roi = None; oos_wr = None; oos_n = len(tr_oos)
            if oos_n > 0:
                # recompute compounding ROI on the OOS sub-ledger
                eq0 = 100.0; e = eq0
                for f in tr_oos["pnl_frac"].values:
                    e *= (1 + f)
                oos_roi = e / eq0 - 1
                oos_wr = (tr_oos["pnl_usd"] > 0).mean()
        else:
            oos_roi = None; oos_wr = None; oos_n = 0
        rows.append({"strat": name, "tf": tf, "cost": cost, **m,
                     "oos_n": oos_n, "oos_roi": oos_roi, "oos_wr": oos_wr})
    return rows


def main():
    tfs = sys.argv[1:] or ["15m", "5m"]
    all_rows = []
    for tf in tfs:
        if not os.path.exists(os.path.join(CACHE, f"BTC_{tf}.csv")):
            print(f"[{tf}] no data yet, skip"); continue
        df_full = load(tf)
        end = df_full.index[-1]
        test_start = end - pd.Timedelta(days=TEST_DAYS)
        # compute indicators on full data (warmup), then slice to test window
        atr_full = ind.atr(df_full["high"], df_full["low"], df_full["close"], 14)
        df = df_full[df_full.index >= test_start].copy()
        atr = atr_full.reindex(df.index).values
        print(f"\n{'='*120}\n{tf} bars | test window {df.index[0]} -> {df.index[-1]} "
              f"({len(df):,} bars, {(df.index[-1]-df.index[0]).days}d)\n{'='*120}")
        print(f"{'strategy':<14}{'cost':>5} | {'trades':>6}{'win%':>6}{'ROI%':>9}"
              f"{'PF':>6}{'maxDD%':>7}{'Shrp':>6}{'liq':>4} | {'OOSn':>5}{'OOSroi%':>9}")
        for name, (fn, kwargs) in S.REGISTRY.items():
            try:
                rows = run_strategy_grid(name, fn, kwargs, df, atr, tf)
            except Exception as e:
                print(f"  {name:<14} ERROR: {type(e).__name__}: {e}")
                continue
            for r in rows:
                all_rows.append(r)
                if r.get("n_trades", 0) == 0:
                    if r["cost"] == 0.0:
                        print(f"  {name:<12}{r['cost']:>5.0f} | {'--- no trades ---':>40}")
                    continue
                oroi = f"{r['oos_roi']*100:>8.0f}" if r['oos_roi'] is not None else "    n/a"
                print(f"  {name:<12}{r['cost']:>5.0f} | {r['n_trades']:>6}{r['win_rate']*100:>6.0f}"
                      f"{r['roi']*100:>9.0f}{str(r['profit_factor']):>6}{r['max_dd']*100:>7.0f}"
                      f"{r['sharpe']:>6}{r['liquidations']:>4} | {r['oos_n']:>5}{oroi}")
    out = pd.DataFrame(all_rows)
    out.to_csv(os.path.join(CACHE, "screen_results.csv"), index=False)
    print(f"\nSaved {len(out)} rows -> cache/screen_results.csv")

    # ---- honest verdict: which survive cost>=2bps AND positive OOS ----
    print(f"\n{'='*70}\nSURVIVORS (full-period ROI>0 at >=2bps AND OOS ROI>0):")
    surv = out[(out["cost"] >= 2.0) & (out["roi"] > 0) & (out["oos_roi"].fillna(-1) > 0)
               & (out["n_trades"] >= 30)]
    if len(surv) == 0:
        print("  NONE. No strategy is robustly profitable after >=2bps cost + OOS.")
    else:
        for _, r in surv.sort_values("oos_roi", ascending=False).iterrows():
            print(f"  {r['strat']:<14}{r['tf']:>4} @{r['cost']:.0f}bps  ROI {r['roi']*100:>6.0f}%"
                  f"  OOSroi {r['oos_roi']*100:>6.0f}%  win {r['win_rate']*100:.0f}%  PF {r['profit_factor']}")


if __name__ == "__main__":
    main()
