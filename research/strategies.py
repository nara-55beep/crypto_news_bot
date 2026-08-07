"""
research/strategies.py — signal generators. Each returns a daily target-weight
DataFrame (columns = coins) aligned to the close panel, suitable for
engine.portfolio_backtest. No look-ahead: a weight on day t is computed only from
data up to and including t; the engine lags it 1 day before it earns a return.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


# ------------------------------ single-asset trend ----------------------------
def ma_cross(close: pd.DataFrame, coin: str, fast=50, slow=200, allow_short=False):
    px = close[coin]
    f, s = px.rolling(fast).mean(), px.rolling(slow).mean()
    sig = (f > s).astype(float)
    if allow_short:
        sig = sig * 2 - 1                      # +1 long / -1 short
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    w[coin] = sig.where(s.notna(), 0.0)
    return w


def donchian(close: pd.DataFrame, coin: str, entry=50, exit=25, allow_short=False):
    """Long when close breaks the `entry`-day high; flat (or short) when it breaks the
    `exit`-day low. Classic turtle-style breakout. State machine, no look-ahead."""
    px = close[coin]
    hi = px.rolling(entry).max()
    lo = px.rolling(exit).min()
    sig = pd.Series(np.nan, index=px.index)
    state = 0
    for i in range(len(px)):
        if np.isnan(hi.iloc[i]):
            sig.iloc[i] = 0; continue
        p = px.iloc[i]
        if state <= 0 and p >= hi.iloc[i]:
            state = 1
        elif state >= 0 and p <= lo.iloc[i]:
            state = -1 if allow_short else 0
        sig.iloc[i] = state
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    w[coin] = sig
    return w


def tsmom(close: pd.DataFrame, coin: str, lookback=90, allow_short=True):
    """Time-series momentum: long if trailing `lookback`-day return > 0, else short/flat."""
    px = close[coin]
    trail = px.pct_change(lookback)
    sig = np.sign(trail) if allow_short else (trail > 0).astype(float)
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    w[coin] = pd.Series(sig, index=px.index).where(trail.notna(), 0.0)
    return w


# ------------------------------ cross-sectional -------------------------------
def _min_history_mask(close: pd.DataFrame, min_days=120):
    """True where a coin has at least `min_days` of prior closes (so it's tradable)."""
    return close.notna().rolling(min_days).sum() >= min_days


def xs_momentum(close: pd.DataFrame, lookback=60, skip=0, q=0.30, rebal=7,
                long_short=True, min_history=120, universe=None):
    """Cross-sectional momentum: every `rebal` days rank the universe by trailing
    return (lookback, optionally skipping the most recent `skip` days), go long the
    top `q` fraction (equal weight, gross=1) and — if long_short — short the bottom
    `q`. Weights held constant between rebalances."""
    cols = universe or list(close.columns)
    c = close[cols]
    mom = c.shift(skip).pct_change(lookback)
    tradable = _min_history_mask(c, min_history) & c.notna()
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    rebal_days = close.index[::rebal]
    for d in rebal_days:
        m = mom.loc[d][tradable.loc[d]]
        m = m.dropna()
        if len(m) < 6:
            continue
        k = max(1, int(len(m) * q))
        longs = m.nlargest(k).index
        row = pd.Series(0.0, index=close.columns)
        row[longs] = 1.0 / k
        if long_short:
            shorts = m.nsmallest(k).index
            row[shorts] = -1.0 / k
        w.loc[d, :] = row.values
    # keep only rebalance-day rows, then forward-fill held weights between rebalances
    w = pd.DataFrame(np.where(close.index.isin(rebal_days)[:, None], w.values, np.nan),
                     index=close.index, columns=close.columns).ffill().fillna(0.0)
    return w


def xs_reversal(close: pd.DataFrame, lookback=3, q=0.20, rebal=2,
                long_short=True, min_history=120, universe=None):
    """Short-term cross-sectional reversal: long recent LOSERS, short recent WINNERS
    over a short `lookback` (days). Documented in crypto (Liu-Tsyvinski-Wu reversal)."""
    cols = universe or list(close.columns)
    c = close[cols]
    past = c.pct_change(lookback)
    tradable = _min_history_mask(c, min_history) & c.notna()
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    rebal_days = close.index[::rebal]
    for d in rebal_days:
        m = past.loc[d][tradable.loc[d]].dropna()
        if len(m) < 6:
            continue
        k = max(1, int(len(m) * q))
        losers = m.nsmallest(k).index            # buy losers (reversal)
        row = pd.Series(0.0, index=close.columns)
        row[losers] = 1.0 / k
        if long_short:
            winners = m.nlargest(k).index
            row[winners] = -1.0 / k
        w.loc[d, :] = row.values
    w = pd.DataFrame(np.where(close.index.isin(rebal_days)[:, None], w.values, np.nan),
                     index=close.index, columns=close.columns).ffill().fillna(0.0)
    return w


# ------------------------------ benchmarks ------------------------------------
def buy_hold(close: pd.DataFrame, coin: str):
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    w[coin] = 1.0
    return w
