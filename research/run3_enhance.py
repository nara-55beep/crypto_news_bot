"""
run3_enhance.py — strengthen the only survivor (Donchian breakout trend) and stress it
harder:
  (1) REGIME FILTER: take longs only when BTC > 200d MA — does it cure the bear bleed?
  (2) DIVERSIFY: one Donchian trend sleeve per liquid major/large-cap, equal weight.
      Trend-following is far less survivorship-sensitive than cross-sectional (a coin
      that dies just triggers an exit, it doesn't reward hindsight).
  (3) VOL-TARGET: scale the BTC sleeve to a target annual vol to control drawdown.
  (4) ROLLING WALK-FORWARD: per-calendar-year OOS Sharpe — consistency, not one split.
"""
import data, engine as E, strategies as S
import numpy as np, pandas as pd
pd.set_option("display.width", 220)

prices = data.load_prices()
close = E.close_panel(prices)
regimes = E.btc_regimes(close["BTC"].dropna())
SPLIT = pd.Timestamp("2022-06-01")
TREND_SET = [c for c in close.columns if data.BUCKET.get(c) in ("major", "large")]


def metrics_line(r, name):
    print("  " + E.fmt(E.equity_metrics(r, name)))


def regime_filter(w, coin, ma=200):
    """Zero out the weight on days BTC (the market) is below its `ma`-day average."""
    on = (close["BTC"] > close["BTC"].rolling(ma).mean()).astype(float)
    return w.mul(on, axis=0)


def vol_target(r, target=0.60, lookback=20, cap=3.0):
    """Scale a return stream so its trailing realized vol ≈ target (annualized)."""
    rv = r.rolling(lookback).std() * np.sqrt(E.ANN)
    lev = (target / rv).clip(upper=cap).shift(1).fillna(0.0)
    return r * lev


def multi_trend(coins, entry=50, exit=25, ma_filter=False):
    """Equal-weight (1/N) long-only Donchian sleeve per coin → diversified trend book."""
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for c in coins:
        wc = S.donchian(close, c, entry, exit, allow_short=False)[c]
        if ma_filter:
            on = (close[c] > close[c].rolling(200).mean()).astype(float)
            wc = wc * on
        w[c] = wc / len(coins)
    return w


print(f"trend set ({len(TREND_SET)}): {TREND_SET}\n")

# (1) regime filter on BTC Donchian
print("(1) BTC Donchian 50/25 — plain vs 200dMA regime filter")
w_plain = S.donchian(close, "BTC", 50, 25)
w_filt = regime_filter(w_plain, "BTC")
r_plain = E.portfolio_backtest(w_plain, close, 10)
r_filt = E.portfolio_backtest(w_filt, close, 10)
for tag, r in (("plain", r_plain), ("MA-filtered", r_filt)):
    print(f"  -- {tag} --")
    metrics_line(r[r.index < SPLIT], "IS")
    metrics_line(r[r.index >= SPLIT], "OOS")
print("  regime breakdown (MA-filtered):")
print(E.regime_breakdown(r_filt, regimes).to_string(index=False))

# (2) diversified multi-asset trend
print("\n(2) DIVERSIFIED TREND (Donchian 50/25 per major+large, 1/N, long-only)")
w_div = multi_trend(TREND_SET, 50, 25, ma_filter=False)
r_div = E.portfolio_backtest(w_div, close, 10)
metrics_line(r_div, "MultiTrend [full]")
metrics_line(r_div[r_div.index < SPLIT], "MultiTrend [IS]")
metrics_line(r_div[r_div.index >= SPLIT], "MultiTrend [OOS]")
print("  + per-coin 200dMA filter:")
w_divf = multi_trend(TREND_SET, 50, 25, ma_filter=True)
r_divf = E.portfolio_backtest(w_divf, close, 10)
metrics_line(r_divf[r_divf.index < SPLIT], "MultiTrend-F [IS]")
metrics_line(r_divf[r_divf.index >= SPLIT], "MultiTrend-F [OOS]")
print("  regime breakdown (MultiTrend plain):")
print(E.regime_breakdown(r_div, regimes).to_string(index=False))

# (3) vol-targeting the BTC sleeve and the diversified book
print("\n(3) VOL-TARGET 60% annual")
r_btc_vt = vol_target(r_plain, 0.60)
r_div_vt = vol_target(r_div, 0.60)
metrics_line(r_btc_vt, "BTC Donchian volTgt [full]")
metrics_line(r_btc_vt[r_btc_vt.index >= SPLIT], "BTC Donchian volTgt [OOS]")
metrics_line(r_div_vt, "MultiTrend volTgt [full]")
metrics_line(r_div_vt[r_div_vt.index >= SPLIT], "MultiTrend volTgt [OOS]")

# (4) rolling walk-forward — per-year OOS Sharpe (consistency)
print("\n(4) PER-YEAR Sharpe (walk-forward consistency):")
for name, r in (("BTC Donchian", r_plain), ("BTC Donch+MA", r_filt),
                ("MultiTrend", r_div), ("MultiTrend volTgt", r_div_vt)):
    yr = r.groupby(r.index.year).apply(
        lambda x: (x.mean()*E.ANN)/(x.std()*np.sqrt(E.ANN)) if x.std() > 0 else np.nan)
    print(f"  {name:<20} " + "  ".join(f"{y}:{v:>5.2f}" for y, v in yr.items()))
