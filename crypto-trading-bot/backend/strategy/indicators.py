"""
indicators.py — plain, dependency-light technical indicators (pandas/numpy only).

No TA-Lib needed (it's a pain to install). Every function takes/returns pandas
Series so they compose cleanly and are easy to read.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI (0-100)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == EMA with alpha = 1/length
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)  # neutral until enough data


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram)."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def roc(series: pd.Series, length: int = 10) -> pd.Series:
    """Rate of change in percent over `length` bars."""
    return series.pct_change(length) * 100.0


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average Directional Index (Wilder) — measures TREND STRENGTH (0-100).
    V2 only trades when this is high enough (a real trend exists), which is the
    single biggest fix over V1."""
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean() / atr_
    mdi = 100 * minus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False, min_periods=length).mean().fillna(0.0)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price that resets each UTC day (typical-price based)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = typical * df["volume"]
    day = df.index.tz_convert("UTC").date if df.index.tz is not None else df.index.date
    grouper = pd.Series(day, index=df.index)
    cum_tpv = tpv.groupby(grouper).cumsum()
    cum_vol = df["volume"].groupby(grouper).cumsum().replace(0.0, np.nan)
    return (cum_tpv / cum_vol).ffill()


def add_entry_indicators(df: pd.DataFrame, s) -> pd.DataFrame:
    """Add every column the entry-timeframe strategy needs. `s` is the settings object."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], s.ema_fast)
    out["ema_slow"] = ema(out["close"], s.ema_slow)
    out["rsi"] = rsi(out["close"], s.rsi_period)
    out["atr"] = atr(out, s.atr_period)
    out["atr_pct"] = out["atr"] / out["close"]
    out["vwap"] = session_vwap(out)
    out["vol_ma"] = out["volume"].rolling(s.volume_ma).mean()
    _, _, out["macd_hist"] = macd(out["close"])
    out["roc"] = roc(out["close"], s.roc_period)
    out["hh"] = out["high"].rolling(s.breakout_lookback).max().shift(1)  # prior N-bar high
    out["ll"] = out["low"].rolling(s.breakout_lookback).min().shift(1)   # prior N-bar low
    return out


def htf_trend(df: pd.DataFrame, s) -> pd.Series:
    """Higher-timeframe trend per bar: +1 bullish, -1 bearish, 0 neither.

    Bullish  = close > EMA200 AND EMA50 > EMA200
    Bearish  = close < EMA200 AND EMA50 < EMA200
    """
    ef = ema(df["close"], s.ema_fast)
    es = ema(df["close"], s.ema_slow)
    bull = (df["close"] > es) & (ef > es)
    bear = (df["close"] < es) & (ef < es)
    trend = pd.Series(0, index=df.index, dtype=int)
    trend[bull] = 1
    trend[bear] = -1
    return trend
