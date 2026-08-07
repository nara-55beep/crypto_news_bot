"""
indicators.py — pure, stateless technical-analysis helpers.

A "candle" everywhere in this package is a dict: {"t": epoch_sec, "o","h","l","c","v"}.
Everything here is look-ahead-safe: a function only ever uses the candles you pass it, and the
caller is responsible for passing only CLOSED candles up to the decision time.
"""
from __future__ import annotations
from datetime import datetime, timezone


# ---------------------------------------------------------------- true range / ATR
def true_range(c, prev_close):
    return max(c["h"] - c["l"], abs(c["h"] - prev_close), abs(c["l"] - prev_close))


def atr(candles, period=14):
    """Simple-average ATR over the last `period` bars. None if insufficient data."""
    if len(candles) < period + 1:
        return None
    trs = [true_range(candles[i], candles[i - 1]["c"]) for i in range(1, len(candles))]
    window = trs[-period:]
    return sum(window) / len(window)


# ---------------------------------------------------------------- EMA
def ema(values, length):
    if len(values) < 1:
        return None
    k = 2.0 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


# ---------------------------------------------------------------- fractal swings
def swing_high_idx(candles, width=2):
    """Indexes of confirmed swing highs: high strictly exceeds `width` bars on each side."""
    out = []
    for i in range(width, len(candles) - width):
        h = candles[i]["h"]
        if all(h > candles[i - j]["h"] for j in range(1, width + 1)) and \
           all(h > candles[i + j]["h"] for j in range(1, width + 1)):
            out.append(i)
    return out


def swing_low_idx(candles, width=2):
    out = []
    for i in range(width, len(candles) - width):
        lo = candles[i]["l"]
        if all(lo < candles[i - j]["l"] for j in range(1, width + 1)) and \
           all(lo < candles[i + j]["l"] for j in range(1, width + 1)):
            out.append(i)
    return out


# ---------------------------------------------------------------- 4H trend classification
def classify_trend(c4h, width=2, ema_len=50, lookback=120):
    """Return 'bull' | 'bear' | None for the most recent CLOSED 4H structure.

    bull = last two swing highs rising AND last two swing lows rising AND close >= EMA(ema_len)
    bear = mirror. Otherwise None (no trade)."""
    if len(c4h) < width * 2 + 4:
        return None
    seg = c4h[-lookback:] if len(c4h) > lookback else c4h
    shi = swing_high_idx(seg, width)
    sli = swing_low_idx(seg, width)
    if len(shi) < 2 or len(sli) < 2:
        return None
    hh = seg[shi[-1]]["h"] > seg[shi[-2]]["h"]
    hl = seg[sli[-1]]["l"] > seg[sli[-2]]["l"]
    lh = seg[shi[-1]]["h"] < seg[shi[-2]]["h"]
    ll = seg[sli[-1]]["l"] < seg[sli[-2]]["l"]
    e = ema([c["c"] for c in seg], ema_len)
    close = seg[-1]["c"]
    if hh and hl and (e is None or close >= e):
        return "bull"
    if lh and ll and (e is None or close <= e):
        return "bear"
    return None


# ---------------------------------------------------------------- previous-day high/low
def _utc_day(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.year, d.month, d.day


def prev_day_high_low(daily_candles, now_ts):
    """High/low of the most recently COMPLETED UTC day before now_ts. (pdh, pdl) or (None, None)."""
    today = _utc_day(now_ts)
    prev = None
    for c in daily_candles:
        if _utc_day(c["t"]) < today:
            prev = c
        else:
            break
    if prev is None:
        return None, None
    return prev["h"], prev["l"]


# ---------------------------------------------------------------- daily VWAP
def daily_vwap(intraday_candles, now_ts):
    """Volume-weighted average of typical price for the CURRENT UTC day up to now_ts."""
    today = _utc_day(now_ts)
    num = den = 0.0
    for c in intraday_candles:
        if c["t"] > now_ts:
            break
        if _utc_day(c["t"]) == today:
            tp = (c["h"] + c["l"] + c["c"]) / 3.0
            num += tp * c["v"]
            den += c["v"]
    return (num / den) if den > 0 else None


# ---------------------------------------------------------------- consolidation / absorption
def consolidation(bars, atr_val, body_atr_mult=0.6, range_atr_mult=1.5):
    """Given a window of consecutive bars, return (range_high, range_low) if they form a tight,
    small-bodied, sideways cluster; else None.
       - every body <= body_atr_mult * ATR (small bodies / absorption)
       - whole cluster (max high - min low) <= range_atr_mult * ATR (sideways)"""
    if not bars or atr_val is None or atr_val <= 0:
        return None
    for c in bars:
        if abs(c["c"] - c["o"]) > body_atr_mult * atr_val:
            return None
    rh = max(c["h"] for c in bars)
    rl = min(c["l"] for c in bars)
    if (rh - rl) > range_atr_mult * atr_val:
        return None
    return rh, rl
