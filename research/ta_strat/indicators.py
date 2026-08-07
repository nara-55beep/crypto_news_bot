"""
indicators.py — a broad, dependency-free (pandas/numpy only) technical-analysis
library: trend, momentum, volatility, volume indicators + candlestick patterns.
Everything is vectorized and causal (uses only past/current bar data).
"""
from __future__ import annotations
import numpy as np
import pandas as pd


# ----------------------------- moving averages --------------------------------
def sma(s, n):  return s.rolling(n).mean()
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()


def wma(s, n):
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


# ----------------------------- momentum ---------------------------------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def stoch(high, low, close, n=14, d=3):
    ll = low.rolling(n).min(); hh = high.rolling(n).max()
    k = 100 * (close - ll) / (hh - ll).replace(0, np.nan)
    return k, k.rolling(d).mean()


def macd(close, fast=12, slow=26, sig=9):
    line = ema(close, fast) - ema(close, slow)
    signal = ema(line, sig)
    return line, signal, line - signal


def roc(close, n=10):
    return close.pct_change(n) * 100


def cci(high, low, close, n=20):
    tp = (high + low + close) / 3
    ma = tp.rolling(n).mean()
    md = (tp - ma).abs().rolling(n).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def williams_r(high, low, close, n=14):
    hh = high.rolling(n).max(); ll = low.rolling(n).min()
    return -100 * (hh - close) / (hh - ll).replace(0, np.nan)


# ----------------------------- volatility -------------------------------------
def true_range(high, low, close):
    pc = close.shift(1)
    return pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)


def atr(high, low, close, n=14):
    return true_range(high, low, close).ewm(alpha=1 / n, adjust=False).mean()


def bollinger(close, n=20, k=2.0):
    ma = close.rolling(n).mean(); sd = close.rolling(n).std()
    return ma - k * sd, ma, ma + k * sd


def keltner(high, low, close, n=20, k=2.0):
    mid = ema(close, n); rng = atr(high, low, close, n)
    return mid - k * rng, mid, mid + k * rng


def donchian(high, low, n=20):
    return low.rolling(n).min(), high.rolling(n).max()


def adx(high, low, close, n=14):
    up = high.diff(); dn = -low.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = true_range(high, low, close)
    atr_ = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), plus_di, minus_di


def supertrend(high, low, close, n=10, mult=3.0):
    """Returns (trend_dir series in {1,-1}, supertrend line). dir=1 uptrend."""
    hl2 = (high + low) / 2
    a = atr(high, low, close, n)
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    n_ = len(close)
    st = np.full(n_, np.nan); dir_ = np.ones(n_)
    c = close.values; up = upper.values; lo = lower.values
    fu = up.copy(); fl = lo.copy()
    for i in range(1, n_):
        fu[i] = up[i] if (up[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lo[i] if (lo[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        if c[i] > fu[i-1]:
            dir_[i] = 1
        elif c[i] < fl[i-1]:
            dir_[i] = -1
        else:
            dir_[i] = dir_[i-1]
        st[i] = fl[i] if dir_[i] == 1 else fu[i]
    return pd.Series(dir_, index=close.index), pd.Series(st, index=close.index)


# ----------------------------- volume -----------------------------------------
def vwap_session(df):
    """Rolling intraday VWAP, reset each UTC day."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    day = df.index.normalize()
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return cum_pv / cum_v


def obv(close, volume):
    return (np.sign(close.diff()).fillna(0) * volume).cumsum()


def vol_sma_ratio(volume, n=20):
    return volume / volume.rolling(n).mean()


# ----------------------------- candlestick patterns ---------------------------
def candle_features(df):
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o)
    rng = (h - l).replace(0, np.nan)
    upsh = h - c.combine(o, max)
    dnsh = c.combine(o, min) - l
    out = pd.DataFrame(index=df.index)
    out["body"] = body
    out["body_abs"] = body.abs()
    out["range"] = (h - l)
    out["body_frac"] = body.abs() / rng
    out["up_shadow"] = upsh
    out["dn_shadow"] = dnsh
    out["bull"] = (c > o).astype(int)
    return out


def bullish_engulfing(df):
    o, c = df["open"], df["close"]
    prev_bear = c.shift(1) < o.shift(1)
    cur_bull = c > o
    engulf = (c >= o.shift(1)) & (o <= c.shift(1))
    return (prev_bear & cur_bull & engulf).astype(int)


def bearish_engulfing(df):
    o, c = df["open"], df["close"]
    prev_bull = c.shift(1) > o.shift(1)
    cur_bear = c < o
    engulf = (o >= c.shift(1)) & (c <= o.shift(1))
    return (prev_bull & cur_bear & engulf).astype(int)


def hammer(df):
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs(); rng = (h - l).replace(0, np.nan)
    lower = c.combine(o, min) - l; upper = h - c.combine(o, max)
    return ((lower >= 2 * body) & (upper <= 0.3 * body) & (body / rng < 0.4)).astype(int)


def shooting_star(df):
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs(); rng = (h - l).replace(0, np.nan)
    lower = c.combine(o, min) - l; upper = h - c.combine(o, max)
    return ((upper >= 2 * body) & (lower <= 0.3 * body) & (body / rng < 0.4)).astype(int)


def doji(df, thresh=0.1):
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).replace(0, np.nan)
    return (((c - o).abs() / rng) < thresh).astype(int)


def add_all(df):
    """Attach a broad indicator set to a copy of df. Returns the enriched frame."""
    x = df.copy()
    c, h, l, v = x["close"], x["high"], x["low"], x["volume"]
    x["ema9"] = ema(c, 9); x["ema21"] = ema(c, 21); x["ema50"] = ema(c, 50)
    x["ema200"] = ema(c, 200); x["sma200"] = sma(c, 200)
    x["rsi14"] = rsi(c, 14); x["rsi2"] = rsi(c, 2)
    k, d = stoch(h, l, c); x["stoch_k"], x["stoch_d"] = k, d
    ml, ms, mh = macd(c); x["macd"], x["macd_sig"], x["macd_hist"] = ml, ms, mh
    x["atr14"] = atr(h, l, c, 14)
    bl, bm, bu = bollinger(c); x["bb_low"], x["bb_mid"], x["bb_up"] = bl, bm, bu
    x["bb_width"] = (bu - bl) / bm
    dl, du = donchian(h, l, 20); x["dc_low20"], x["dc_high20"] = dl, du
    dl5, du5 = donchian(h, l, 55); x["dc_low55"], x["dc_high55"] = dl5, du5
    adx_, pdi, mdi = adx(h, l, c); x["adx"], x["plus_di"], x["minus_di"] = adx_, pdi, mdi
    sdir, sline = supertrend(h, l, c); x["st_dir"], x["st_line"] = sdir, sline
    x["vwap"] = vwap_session(x)
    x["vol_ratio"] = vol_sma_ratio(v, 20)
    x["cci20"] = cci(h, l, c, 20)
    x["willr"] = williams_r(h, l, c, 14)
    return x
