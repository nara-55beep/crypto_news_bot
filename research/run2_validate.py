"""
run2_validate.py — the part that actually matters. For the candidates that survived
run1, run:
  (A) IN-SAMPLE vs OUT-OF-SAMPLE split (train ≤2022-06, test >2022-06)
  (B) PARAMETER-GRID robustness (is the edge a broad plateau or one lucky cell?)
  (C) REGIME breakdown (bull / bear / sideways)
  (D) COST stress (5/10/20/30 bps) + short-financing drag for long/short
  (E) SURVIVORSHIP check: restrict cross-sectional universe to liquid large/mid only
We are trying to BREAK each strategy. A survivor must hold up everywhere.
"""
import data, engine as E, strategies as S
import numpy as np, pandas as pd
pd.set_option("display.width", 220)

prices = data.load_prices()
close = E.close_panel(prices)
regimes = E.btc_regimes(close["BTC"].dropna())
SPLIT = pd.Timestamp("2022-06-01")          # train ≤ split < test
LIQUID = [c for c in close.columns if data.BUCKET.get(c) in ("major", "large", "mid")]


def oos(name, w, cost=10.0, short_fin_apr=0.0):
    """Print full / in-sample / out-of-sample metrics for one weight matrix."""
    r = E.portfolio_backtest(w, close, cost_bps=cost)
    if short_fin_apr:                                   # daily drag on gross SHORT notional
        short_notional = w.shift(1).clip(upper=0).abs().sum(axis=1)
        r = r - short_notional * (short_fin_apr / E.ANN)
    full = E.equity_metrics(r, name + " [full]")
    ins = E.equity_metrics(r[r.index < SPLIT], name + " [IS ≤22-06]")
    out = E.equity_metrics(r[r.index >= SPLIT], name + " [OOS>22-06]")
    for m in (full, ins, out):
        print("  " + E.fmt(m))
    return r, out


def grid_donchian(entries, exits, cost=10.0):
    print("\n(B) DONCHIAN robustness — OOS(>22-06) Sharpe across params:")
    tbl = pd.DataFrame(index=[f"e{e}" for e in entries], columns=[f"x{x}" for x in exits], dtype=float)
    for e in entries:
        for x in exits:
            w = S.donchian(close, "BTC", e, x, allow_short=False)
            r = E.portfolio_backtest(w, close, cost_bps=cost)
            m = E.equity_metrics(r[r.index >= SPLIT])
            tbl.loc[f"e{e}", f"x{x}"] = m.get("Sharpe", np.nan)
    print(tbl.to_string())
    vals = tbl.values.astype(float).ravel()
    print(f"   grid: median Sharpe {np.nanmedian(vals):.2f}, %>0.5 = "
          f"{100*np.mean(vals>0.5):.0f}%, min {np.nanmin(vals):.2f}, max {np.nanmax(vals):.2f}")


def grid_xsmom(lookbacks, qs, universe, tag, rebal=7, cost=10.0, short_fin=0.0):
    print(f"\n(B) XS-MOMENTUM robustness [{tag}] — OOS(>22-06) Sharpe (lookback × q):")
    tbl = pd.DataFrame(index=[f"L{l}" for l in lookbacks], columns=[f"q{int(q*100)}" for q in qs], dtype=float)
    for l in lookbacks:
        for q in qs:
            w = S.xs_momentum(close, l, 0, q, rebal, long_short=True, universe=universe)
            r = E.portfolio_backtest(w, close, cost_bps=cost)
            if short_fin:
                sn = w.shift(1).clip(upper=0).abs().sum(axis=1)
                r = r - sn * (short_fin / E.ANN)
            m = E.equity_metrics(r[r.index >= SPLIT])
            tbl.loc[f"L{l}", f"q{int(q*100)}"] = m.get("Sharpe", np.nan)
    print(tbl.to_string())
    vals = tbl.values.astype(float).ravel()
    print(f"   grid: median Sharpe {np.nanmedian(vals):.2f}, %>0.5 = "
          f"{100*np.mean(vals>0.5):.0f}%, min {np.nanmin(vals):.2f}, max {np.nanmax(vals):.2f}")


print(f"panel {close.shape[1]} coins | split {SPLIT.date()} | liquid universe = {len(LIQUID)} coins\n")

print("="*70, "\n(A) IN/OUT-OF-SAMPLE — CANDIDATE 1: BTC Donchian 50/25 long-only")
r_don, _ = oos("BTC Donchian 50/25", S.donchian(close, "BTC", 50, 25, allow_short=False))

print("\n(A) CANDIDATE 2: XS-Mom L30 q30 weekly LONG/SHORT — FULL universe (survivorship-prone)")
r_xs_full, _ = oos("XS-Mom L30 q30 LS full", S.xs_momentum(close, 30, 0, 0.30, 7, long_short=True))

print("\n(A) CANDIDATE 2b: same but LIQUID universe (large/mid only) + 20% APR short-financing")
r_xs_liq, _ = oos("XS-Mom L30 q30 LS liquid", S.xs_momentum(close, 30, 0, 0.30, 7, long_short=True, universe=LIQUID),
                  short_fin_apr=0.20)

print("\n(A) CANDIDATE 3: XS-Mom L30 q30 weekly LONG-ONLY, liquid universe")
r_xs_lo, _ = oos("XS-Mom L30 q30 LongOnly liq", S.xs_momentum(close, 30, 0, 0.30, 7, long_short=False, universe=LIQUID))

# (B) robustness grids
grid_donchian([30, 40, 50, 60, 80, 100], [10, 15, 20, 25, 30, 40])
grid_xsmom([20, 30, 45, 60, 90], [0.20, 0.30, 0.40], LIQUID, "liquid LS", short_fin=0.20)

# (C) regimes for the two cleanest candidates
print("\n(C) REGIME BREAKDOWN — BTC Donchian 50/25:")
print(E.regime_breakdown(r_don, regimes).to_string(index=False))
print("\n(C) REGIME BREAKDOWN — XS-Mom L30 q30 LS liquid (20% short fin):")
print(E.regime_breakdown(r_xs_liq, regimes).to_string(index=False))

# (D) cost stress
print("\n(D) COST STRESS — OOS(>22-06) Sharpe:")
for cost in (5, 10, 20, 30):
    rd = E.portfolio_backtest(S.donchian(close, "BTC", 50, 25), close, cost_bps=cost)
    rx = E.portfolio_backtest(S.xs_momentum(close, 30, 0, 0.30, 7, True, universe=LIQUID), close, cost_bps=cost)
    md = E.equity_metrics(rd[rd.index >= SPLIT]); mx = E.equity_metrics(rx[rx.index >= SPLIT])
    print(f"   {cost:>2}bps  Donchian {md.get('Sharpe'):>5}   XS-Mom-liq {mx.get('Sharpe'):>5}")
