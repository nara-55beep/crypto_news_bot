"""
research/engine.py — backtest primitives + metric suite.

Two backtest shapes, both with realistic costs:
  * portfolio_backtest(weights, prices, cost_bps): a daily target-weight matrix
    (columns=coins). Returns the net daily-return series. Costs charged on turnover.
  * Trade-level stats are derived from a discrete position series per coin.

All signals are shifted +1 day before they earn returns (decide on close, trade next
day) so there is NO look-ahead. Annualization uses 365 (crypto trades every day).
"""
from __future__ import annotations
import numpy as np
import pandas as pd

ANN = 365.0


# ----------------------------- return-based metrics ---------------------------
def equity_metrics(daily_ret: pd.Series, name="") -> dict:
    r = daily_ret.dropna()
    if len(r) < 30 or r.std() == 0:
        return {"name": name, "n_days": len(r), "note": "too short/flat"}
    eq = (1 + r).cumprod()
    yrs = len(r) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1.0
    vol = r.std() * np.sqrt(ANN)
    sharpe = (r.mean() * ANN) / vol if vol > 0 else 0.0
    downside = r[r < 0].std() * np.sqrt(ANN)
    sortino = (r.mean() * ANN) / downside if downside > 0 else 0.0
    peak = eq.cummax()
    dd = (eq / peak - 1.0)
    maxdd = dd.min()
    calmar = cagr / abs(maxdd) if maxdd < 0 else np.nan
    return {
        "name": name, "n_days": len(r), "years": round(yrs, 2),
        "CAGR": round(cagr, 4), "vol": round(vol, 4),
        "Sharpe": round(sharpe, 2), "Sortino": round(sortino, 2),
        "maxDD": round(maxdd, 4), "Calmar": round(calmar, 2) if calmar == calmar else None,
        "total_ret": round(eq.iloc[-1] - 1, 3),
    }


def trade_stats(daily_ret: pd.Series, position: pd.Series) -> dict:
    """Round-trip trade stats from a position series (…,0,1,1,0,… or with -1).
    A 'trade' is a contiguous run of non-zero position; its pnl is the compounded
    strategy return over that run. Gives win rate / profit factor / expectancy."""
    pos = position.reindex(daily_ret.index).fillna(0)
    r = daily_ret.fillna(0)
    trades = []
    in_trade = False
    cur = 1.0
    for d in daily_ret.index:
        p = pos.loc[d]
        if p != 0 and not in_trade:
            in_trade = True; cur = 1.0
        if in_trade:
            cur *= (1 + r.loc[d])
        if p == 0 and in_trade:
            trades.append(cur - 1.0); in_trade = False
    if in_trade:
        trades.append(cur - 1.0)
    trades = np.array(trades)
    if len(trades) == 0:
        return {"n_trades": 0}
    wins = trades[trades > 0]; losses = trades[trades <= 0]
    pf = (wins.sum() / abs(losses.sum())) if losses.sum() != 0 else np.inf
    return {
        "n_trades": int(len(trades)),
        "win_rate": round(len(wins) / len(trades), 3),
        "avg_trade": round(trades.mean(), 4),
        "profit_factor": round(pf, 2) if pf != np.inf else None,
        "expectancy": round(trades.mean(), 4),
        "best": round(trades.max(), 3), "worst": round(trades.min(), 3),
    }


# ----------------------------- portfolio backtest -----------------------------
def close_panel(prices: dict) -> pd.DataFrame:
    """Align all coins' close into one DataFrame indexed by date."""
    return pd.DataFrame({c: df["close"] for c, df in prices.items()}).sort_index()


def daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change()


def portfolio_backtest(weights: pd.DataFrame, close: pd.DataFrame,
                       cost_bps: float = 10.0) -> pd.Series:
    """weights: target weight per coin per day (decided on that day's close).
    Returns net daily strategy returns. Signal is lagged 1 day (trade next day),
    turnover cost = cost_bps * sum|Δweight|."""
    rets = close.pct_change()
    w = weights.reindex(close.index).reindex(columns=close.columns).fillna(0.0)
    w_eff = w.shift(1).fillna(0.0)                       # hold yesterday's target today
    gross = (w_eff * rets).sum(axis=1)
    turnover = (w_eff - w_eff.shift(1)).abs().sum(axis=1)
    cost = turnover * (cost_bps / 1e4)
    return (gross - cost).rename("ret")


# ----------------------------- regime tagging ---------------------------------
def btc_regimes(btc_close: pd.Series) -> pd.Series:
    """Label each day bull / bear / sideways from BTC's 200d trend and slope.
    bull = price>200dMA and MA rising; bear = price<200dMA and MA falling; else sideways."""
    ma = btc_close.rolling(200).mean()
    slope = ma.diff(20)
    reg = pd.Series("sideways", index=btc_close.index)
    reg[(btc_close > ma) & (slope > 0)] = "bull"
    reg[(btc_close < ma) & (slope < 0)] = "bear"
    reg[ma.isna()] = "n/a"
    return reg


def regime_breakdown(daily_ret: pd.Series, regimes: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"ret": daily_ret, "reg": regimes.reindex(daily_ret.index)}).dropna()
    rows = []
    for reg, g in df.groupby("reg"):
        if reg == "n/a" or len(g) < 20:
            continue
        m = equity_metrics(g["ret"], reg)
        rows.append({"regime": reg, "n_days": m["n_days"], "CAGR": m.get("CAGR"),
                     "Sharpe": m.get("Sharpe"), "maxDD": m.get("maxDD"),
                     "total_ret": m.get("total_ret")})
    return pd.DataFrame(rows)


def fmt(m: dict) -> str:
    if m.get("note"):
        return f"{m['name']:<28} {m['note']}"
    return (f"{m['name']:<28} Sharpe {m['Sharpe']:>5} Sortino {m['Sortino']:>5} "
            f"CAGR {m['CAGR']*100:>6.1f}% maxDD {m['maxDD']*100:>6.1f}% "
            f"Calmar {str(m['Calmar']):>5} tot {m['total_ret']*100:>7.0f}% ({m['years']}y)")
