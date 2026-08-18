"""Vectorised indicators over an OHLCV frame.

Every function returns a series aligned to the input index and is *causal*: the
value at bar ``i`` uses only bars ``<= i``.  Nothing here peeks forward, which is
what makes the engine's "decide on bar i, fill on bar i+1" contract safe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ------------------------------------------------------------------ averages
def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length, min_periods=length).apply(
        lambda window: float(np.dot(window, weights) / weights.sum()), raw=True
    )


def hma(series: pd.Series, length: int) -> pd.Series:
    """Hull moving average: WMA of (2*WMA(n/2) - WMA(n)) over sqrt(n)."""
    half = max(1, int(length / 2))
    root = max(1, int(np.sqrt(length)))
    return wma(2.0 * wma(series, half) - wma(series, length), root)


def dema(series: pd.Series, length: int) -> pd.Series:
    first = ema(series, length)
    return 2.0 * first - ema(first, length)


def tema(series: pd.Series, length: int) -> pd.Series:
    one = ema(series, length)
    two = ema(one, length)
    return 3.0 * one - 3.0 * two + ema(two, length)


def kama(series: pd.Series, length: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman adaptive moving average."""
    change = (series - series.shift(length)).abs()
    volatility = series.diff().abs().rolling(length, min_periods=length).sum()
    ratio = (change / volatility.replace(0.0, np.nan)).fillna(0.0)
    smooth = (ratio * (2.0 / (fast + 1) - 2.0 / (slow + 1)) + 2.0 / (slow + 1)) ** 2
    out = pd.Series(np.nan, index=series.index, dtype=float)
    previous = np.nan
    for position, (value, alpha) in enumerate(zip(series.to_numpy(), smooth.to_numpy())):
        if not np.isfinite(value):
            continue
        previous = value if not np.isfinite(previous) else previous + alpha * (value - previous)
        out.iloc[position] = previous
    return out


MA_FUNCS = {"sma": sma, "ema": ema, "wma": wma, "hma": hma, "dema": dema, "tema": tema}


def moving_average(series: pd.Series, length: int, kind: str = "sma") -> pd.Series:
    try:
        return MA_FUNCS[kind](series, length)
    except KeyError as exc:
        raise ValueError(f"unsupported moving average kind: {kind}") from exc


# --------------------------------------------------------------- oscillators
def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(100.0).where(avg_loss.notna())


def stochastic(frame: pd.DataFrame, length: int = 14, smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    low = frame["low"].rolling(length, min_periods=length).min()
    high = frame["high"].rolling(length, min_periods=length).max()
    k = 100.0 * (frame["close"] - low) / (high - low).replace(0.0, np.nan)
    return k, k.rolling(smooth, min_periods=smooth).mean()


def williams_r(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    high = frame["high"].rolling(length, min_periods=length).max()
    low = frame["low"].rolling(length, min_periods=length).min()
    return -100.0 * (high - frame["close"]) / (high - low).replace(0.0, np.nan)


def cci(frame: pd.DataFrame, length: int = 20) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    mean = typical.rolling(length, min_periods=length).mean()
    deviation = (typical - mean).abs().rolling(length, min_periods=length).mean()
    return (typical - mean) / (0.015 * deviation.replace(0.0, np.nan))


def roc(series: pd.Series, length: int = 12) -> pd.Series:
    return 100.0 * (series / series.shift(length) - 1.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def money_flow_index(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    raw = typical * frame["volume"]
    up = raw.where(typical > typical.shift(1), 0.0)
    down = raw.where(typical < typical.shift(1), 0.0)
    ratio = (up.rolling(length, min_periods=length).sum()
             / down.rolling(length, min_periods=length).sum().replace(0.0, np.nan))
    return 100.0 - 100.0 / (1.0 + ratio)


# ---------------------------------------------------------------- volatility
def true_range(frame: pd.DataFrame) -> pd.Series:
    previous = frame["close"].shift(1)
    return pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous).abs(),
        (frame["low"] - previous).abs(),
    ], axis=1).max(axis=1)


def atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    return true_range(frame).ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def bollinger(series: pd.Series, length: int = 20, deviations: float = 2.0):
    mid = sma(series, length)
    spread = series.rolling(length, min_periods=length).std(ddof=0) * deviations
    return mid - spread, mid, mid + spread


def keltner(frame: pd.DataFrame, length: int = 20, multiplier: float = 2.0):
    mid = ema(frame["close"], length)
    band = atr(frame, length) * multiplier
    return mid - band, mid, mid + band


def donchian(frame: pd.DataFrame, length: int = 20):
    lower = frame["low"].rolling(length, min_periods=length).min()
    upper = frame["high"].rolling(length, min_periods=length).max()
    return lower, (lower + upper) / 2.0, upper


def historical_volatility(series: pd.Series, length: int = 20) -> pd.Series:
    return series.pct_change().rolling(length, min_periods=length).std(ddof=0) * np.sqrt(252.0)


# --------------------------------------------------------------------- trend
def adx(frame: pd.DataFrame, length: int = 14):
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=frame.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=frame.index)
    rng = true_range(frame).ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    plus_di = 100.0 * plus.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean() / rng
    minus_di = 100.0 * minus.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean() / rng
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean(), plus_di, minus_di


def aroon(frame: pd.DataFrame, length: int = 25):
    def since(window: np.ndarray, finder) -> float:
        return float(len(window) - 1 - finder(window))

    up = frame["high"].rolling(length + 1, min_periods=length + 1).apply(
        lambda w: 100.0 * (length - since(w, np.argmax)) / length, raw=True)
    down = frame["low"].rolling(length + 1, min_periods=length + 1).apply(
        lambda w: 100.0 * (length - since(w, np.argmin)) / length, raw=True)
    return up, down


def supertrend(frame: pd.DataFrame, length: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Returns +1 while the SuperTrend is bullish and -1 while bearish."""
    band = atr(frame, length) * multiplier
    mid = (frame["high"] + frame["low"]) / 2.0
    upper, lower = mid + band, mid - band
    close = frame["close"].to_numpy()
    upper_a, lower_a = upper.to_numpy(), lower.to_numpy()
    trend = np.full(len(frame), np.nan)
    direction = 1.0
    final_upper, final_lower = np.nan, np.nan
    for i in range(len(frame)):
        if not np.isfinite(upper_a[i]):
            continue
        if not np.isfinite(final_upper):
            final_upper, final_lower = upper_a[i], lower_a[i]
        final_upper = min(upper_a[i], final_upper) if close[i - 1] <= final_upper else upper_a[i]
        final_lower = max(lower_a[i], final_lower) if close[i - 1] >= final_lower else lower_a[i]
        if close[i] > final_upper:
            direction = 1.0
        elif close[i] < final_lower:
            direction = -1.0
        trend[i] = direction
    return pd.Series(trend, index=frame.index)


def parabolic_sar(frame: pd.DataFrame, step: float = 0.02, maximum: float = 0.2) -> pd.Series:
    high, low = frame["high"].to_numpy(), frame["low"].to_numpy()
    out = np.full(len(frame), np.nan)
    if len(frame) < 2:
        return pd.Series(out, index=frame.index)
    rising, acceleration = True, step
    sar, extreme = low[0], high[0]
    for i in range(1, len(frame)):
        sar = sar + acceleration * (extreme - sar)
        if rising:
            if low[i] < sar:
                rising, sar, extreme, acceleration = False, extreme, low[i], step
            elif high[i] > extreme:
                extreme, acceleration = high[i], min(acceleration + step, maximum)
        else:
            if high[i] > sar:
                rising, sar, extreme, acceleration = True, extreme, high[i], step
            elif low[i] < extreme:
                extreme, acceleration = low[i], min(acceleration + step, maximum)
        out[i] = sar
    return pd.Series(out, index=frame.index)


def ichimoku(frame: pd.DataFrame, conversion: int = 9, base: int = 26, span: int = 52):
    def mid(length: int) -> pd.Series:
        return (frame["high"].rolling(length, min_periods=length).max()
                + frame["low"].rolling(length, min_periods=length).min()) / 2.0

    tenkan, kijun = mid(conversion), mid(base)
    # Spans are shifted FORWARD, so at bar i they describe cloud drawn from past
    # data; reading them at i is causal.
    return tenkan, kijun, ((tenkan + kijun) / 2.0).shift(base), mid(span).shift(base)


def linear_regression_slope(series: pd.Series, length: int = 20) -> pd.Series:
    x = np.arange(length, dtype=float)
    centered = x - x.mean()
    denominator = float((centered ** 2).sum())
    return series.rolling(length, min_periods=length).apply(
        lambda w: float(np.dot(centered, w - w.mean()) / denominator), raw=True
    )


# -------------------------------------------------------------------- volume
def obv(frame: pd.DataFrame) -> pd.Series:
    sign = np.sign(frame["close"].diff()).fillna(0.0)
    return (sign * frame["volume"]).cumsum()


def chaikin_money_flow(frame: pd.DataFrame, length: int = 20) -> pd.Series:
    span = (frame["high"] - frame["low"]).replace(0.0, np.nan)
    multiplier = ((frame["close"] - frame["low"]) - (frame["high"] - frame["close"])) / span
    volume = multiplier * frame["volume"]
    return (volume.rolling(length, min_periods=length).sum()
            / frame["volume"].rolling(length, min_periods=length).sum().replace(0.0, np.nan))


def accumulation_distribution(frame: pd.DataFrame) -> pd.Series:
    span = (frame["high"] - frame["low"]).replace(0.0, np.nan)
    multiplier = ((frame["close"] - frame["low"]) - (frame["high"] - frame["close"])) / span
    return (multiplier * frame["volume"]).fillna(0.0).cumsum()


def relative_volume(frame: pd.DataFrame, length: int = 20) -> pd.Series:
    average = frame["volume"].rolling(length, min_periods=length).mean()
    return frame["volume"] / average.replace(0.0, np.nan)


def rolling_vwap(frame: pd.DataFrame, length: int = 20) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    weighted = (typical * frame["volume"]).rolling(length, min_periods=length).sum()
    return weighted / frame["volume"].rolling(length, min_periods=length).sum().replace(0.0, np.nan)


def zscore(series: pd.Series, length: int = 20) -> pd.Series:
    mean = series.rolling(length, min_periods=length).mean()
    std = series.rolling(length, min_periods=length).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


# ------------------------------------------------------------ candle shapes
def body(frame: pd.DataFrame) -> pd.Series:
    return (frame["close"] - frame["open"]).abs()


def upper_shadow(frame: pd.DataFrame) -> pd.Series:
    return frame["high"] - frame[["open", "close"]].max(axis=1)


def lower_shadow(frame: pd.DataFrame) -> pd.Series:
    return frame[["open", "close"]].min(axis=1) - frame["low"]


def candle_range(frame: pd.DataFrame) -> pd.Series:
    return (frame["high"] - frame["low"]).replace(0.0, np.nan)
