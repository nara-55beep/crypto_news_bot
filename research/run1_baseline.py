"""
run1_baseline.py — first pass: real metrics on the FULL sample for the core strategy
families vs buy-&-hold. This is in-sample/exploratory — validation (OOS, walk-forward,
robustness) comes in run2. Costs included.
"""
import data, engine as E, strategies as S
import pandas as pd
pd.set_option("display.width", 200)

prices = data.load_prices()
close = E.close_panel(prices)
btc = close["BTC"].dropna()
regimes = E.btc_regimes(btc)
COST = 10.0   # bps per unit turnover (≈ Binance spot taker 0.10%); stress-tested later

print(f"panel: {close.shape[1]} coins, {close.index[0].date()} → {close.index[-1].date()}\n")

results = []

def run(name, w, position_coin=None):
    r = E.portfolio_backtest(w, close, cost_bps=COST)
    m = E.equity_metrics(r, name)
    print(E.fmt(m))
    results.append((name, r, m))
    return r

print("=== BENCHMARKS ===")
run("BuyHold BTC", S.buy_hold(close, "BTC"))
run("BuyHold ETH", S.buy_hold(close, "ETH"))

print("\n=== SINGLE-ASSET TREND (BTC) ===")
run("BTC MA 50/200 long-only", S.ma_cross(close, "BTC", 50, 200, allow_short=False))
run("BTC MA 20/100 long-only", S.ma_cross(close, "BTC", 20, 100, allow_short=False))
run("BTC MA 50/200 long/short", S.ma_cross(close, "BTC", 50, 200, allow_short=True))
run("BTC Donchian 50/25 LO", S.donchian(close, "BTC", 50, 25, allow_short=False))
run("BTC TSMOM 90 long/short", S.tsmom(close, "BTC", 90, allow_short=True))
run("BTC TSMOM 90 long-only", S.tsmom(close, "BTC", 90, allow_short=False))

print("\n=== SINGLE-ASSET TREND (ETH) ===")
run("ETH MA 50/200 long-only", S.ma_cross(close, "ETH", 50, 200, allow_short=False))
run("ETH Donchian 50/25 LO", S.donchian(close, "ETH", 50, 25, allow_short=False))

print("\n=== CROSS-SECTIONAL MOMENTUM (alt universe) ===")
run("XS-Mom L60 q30 wk LS", S.xs_momentum(close, 60, 0, 0.30, 7, long_short=True))
run("XS-Mom L60 q30 wk LongOnly", S.xs_momentum(close, 60, 0, 0.30, 7, long_short=False))
run("XS-Mom L30 q30 wk LS", S.xs_momentum(close, 30, 0, 0.30, 7, long_short=True))
run("XS-Mom L90 q20 wk LS", S.xs_momentum(close, 90, 0, 0.20, 7, long_short=True))

print("\n=== SHORT-TERM REVERSAL (alt universe) ===")
run("XS-Rev L3 q20 2d LS", S.xs_reversal(close, 3, 0.20, 2, long_short=True))
run("XS-Rev L1 q20 1d LS", S.xs_reversal(close, 1, 0.20, 1, long_short=True))
run("XS-Rev L7 q20 wk LongOnly", S.xs_reversal(close, 7, 0.20, 7, long_short=False))

print("\n=== TOP BY SHARPE ===")
ranked = sorted(results, key=lambda x: x[2].get("Sharpe", -9), reverse=True)
for name, r, m in ranked[:8]:
    print(E.fmt(m))
