"""
run4_final.py — lock the recommendation.
  (1) Confirm the MA-FILTERED Donchian is a broad plateau across params (OOS Sharpe).
  (2) Build the production book: equal-weight BTC+ETH, Donchian long-only + 200dMA
      filter. Full IS/OOS metrics, per-asset trade stats, regimes, per-year, cost stress.
"""
import data, engine as E, strategies as S
import numpy as np, pandas as pd
pd.set_option("display.width", 220)

prices = data.load_prices()
close = E.close_panel(prices)
regimes = E.btc_regimes(close["BTC"].dropna())
SPLIT = pd.Timestamp("2022-06-01")


def filtered_donchian(coin, entry, exit):
    w = S.donchian(close, coin, entry, exit, allow_short=False)[coin]
    on = (close[coin] > close[coin].rolling(200).mean()).astype(float)
    return (w * on)


# (1) plateau of the FILTERED rule
print("(1) MA-FILTERED BTC Donchian — OOS(>22-06) Sharpe across params (plateau check):")
entries, exits = [30, 40, 50, 55, 60, 80], [10, 15, 20, 25, 30]
tbl = pd.DataFrame(index=[f"e{e}" for e in entries], columns=[f"x{x}" for x in exits], dtype=float)
for e in entries:
    for x in exits:
        w = pd.DataFrame(0.0, index=close.index, columns=close.columns); w["BTC"] = filtered_donchian("BTC", e, x)
        r = E.portfolio_backtest(w, close, 10)
        tbl.loc[f"e{e}", f"x{x}"] = E.equity_metrics(r[r.index >= SPLIT]).get("Sharpe", np.nan)
print(tbl.to_string())
v = tbl.values.astype(float).ravel()
print(f"   plateau: median {np.nanmedian(v):.2f}, %>0.7 = {100*np.mean(v>0.7):.0f}%, "
      f"min {np.nanmin(v):.2f}, max {np.nanmax(v):.2f}")

# (2) PRODUCTION BOOK: equal-weight BTC + ETH, Donchian 55/20 long-only + 200dMA filter
ENTRY, EXIT = 55, 20
book = ["BTC", "ETH"]
w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
for c in book:
    w[c] = filtered_donchian(c, ENTRY, EXIT) / len(book)
r = E.portfolio_backtest(w, close, 10)

print(f"\n(2) PRODUCTION BOOK — Donchian {ENTRY}/{EXIT} long-only + 200dMA, EW {book}, 10bps")
for tag, seg in (("FULL", r), ("IS ≤22-06", r[r.index < SPLIT]), ("OOS >22-06", r[r.index >= SPLIT])):
    print("  " + E.fmt(E.equity_metrics(seg, tag)))

print("\n  TRADE-LEVEL STATS per sleeve (entry→exit round trips):")
for c in book:
    pos = (filtered_donchian(c, ENTRY, EXIT) > 0).astype(float)
    sleeve_ret = (pos.shift(1).fillna(0) * close[c].pct_change()).fillna(0)
    ts = E.trade_stats(sleeve_ret, pos)
    print(f"   {c}: {ts}")

print("\n  REGIME BREAKDOWN:")
print(E.regime_breakdown(r, regimes).to_string(index=False))

print("\n  PER-YEAR Sharpe & return:")
yr = r.groupby(r.index.year).agg(
    sharpe=lambda x: (x.mean()*E.ANN)/(x.std()*np.sqrt(E.ANN)) if x.std() > 0 else np.nan,
    ret=lambda x: (1+x).prod()-1)
print(yr.round(2).to_string())

print("\n  COST STRESS (OOS Sharpe):")
for cost in (5, 10, 20, 30, 50):
    rr = E.portfolio_backtest(w, close, cost)
    print(f"   {cost:>2}bps: {E.equity_metrics(rr[rr.index>=SPLIT]).get('Sharpe')}")

# exposure / activity
expo = w.shift(1).sum(axis=1)
print(f"\n  avg gross exposure {expo.mean():.2f} (0=flat,1=fully long); "
      f"days fully flat {100*(expo==0).mean():.0f}%")
