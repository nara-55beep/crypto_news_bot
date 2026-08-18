"""Executable rule engines.

Each function maps ``(frame, params) -> Signal``.  ``Signal.position`` is the
target exposure at each bar in ``{-1, 0, +1}``; the engine fills it at the *next*
bar's open, so every value here may only use information from that bar and
earlier.

Strategies are data, these are the (few) behaviours.  One parameterised engine
backs many catalog entries, which is what makes a catalog of this size testable:
a bug is fixed once, not several hundred times.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    position: pd.Series
    stop_atr: pd.Series | None = None
    atr_stop_multiple: float = 0.0
    take_profit_multiple: float = 0.0
    max_bars_held: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


RuleFunc = Callable[[pd.DataFrame, dict[str, Any]], Signal]
RULES: dict[str, RuleFunc] = {}


def rule(name: str) -> Callable[[RuleFunc], RuleFunc]:
    def register(func: RuleFunc) -> RuleFunc:
        if name in RULES:
            raise ValueError(f"duplicate rule id: {name}")
        RULES[name] = func
        return func
    return register


def _flat(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(0.0, index=frame.index)


def _long_short(condition_long: pd.Series, condition_short: pd.Series,
                allow_short: bool) -> pd.Series:
    out = pd.Series(0.0, index=condition_long.index)
    out[condition_long.fillna(False)] = 1.0
    if allow_short:
        out[condition_short.fillna(False)] = -1.0
    return out


def _state_from_events(index: pd.Index, enter_long: pd.Series, exit_long: pd.Series,
                       enter_short: pd.Series | None = None,
                       exit_short: pd.Series | None = None) -> pd.Series:
    """Walk discrete entry/exit events into a held position series."""
    el = enter_long.fillna(False).to_numpy()
    xl = exit_long.fillna(False).to_numpy()
    es = enter_short.fillna(False).to_numpy() if enter_short is not None else np.zeros(len(index), bool)
    xs = exit_short.fillna(False).to_numpy() if exit_short is not None else np.zeros(len(index), bool)
    out = np.zeros(len(index))
    state = 0.0
    for i in range(len(index)):
        if state == 1.0 and xl[i]:
            state = 0.0
        elif state == -1.0 and xs[i]:
            state = 0.0
        if state == 0.0:
            if el[i]:
                state = 1.0
            elif es[i]:
                state = -1.0
        out[i] = state
    return pd.Series(out, index=index)


def _risk(params: dict[str, Any], frame: pd.DataFrame) -> dict[str, Any]:
    """Shared optional risk overlay, applied by the engine."""
    stop_mult = float(params.get("atr_stop_multiple", 0) or 0)
    return {
        "stop_atr": ind.atr(frame, int(params.get("atr_length", 14))) if stop_mult > 0 else None,
        "atr_stop_multiple": stop_mult,
        "take_profit_multiple": float(params.get("take_profit_multiple", 0) or 0),
        "max_bars_held": int(params.get("max_bars_held", 0) or 0),
    }


# =========================================================== trend following
@rule("trend.ma_crossover")
def ma_crossover(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    kind = str(p.get("ma_kind", "sma"))
    fast = ind.moving_average(frame["close"], int(p["fast"]), kind)
    slow = ind.moving_average(frame["close"], int(p["slow"]), kind)
    return Signal(_long_short(fast > slow, fast < slow, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


@rule("trend.price_vs_ma")
def price_vs_ma(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    ma = ind.moving_average(frame["close"], int(p["length"]), str(p.get("ma_kind", "sma")))
    return Signal(_long_short(frame["close"] > ma, frame["close"] < ma,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.triple_ma")
def triple_ma(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    kind = str(p.get("ma_kind", "ema"))
    fast = ind.moving_average(frame["close"], int(p["fast"]), kind)
    mid = ind.moving_average(frame["close"], int(p["mid"]), kind)
    slow = ind.moving_average(frame["close"], int(p["slow"]), kind)
    return Signal(_long_short((fast > mid) & (mid > slow), (fast < mid) & (mid < slow),
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.ma_ribbon")
def ma_ribbon(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    kind, base, step = str(p.get("ma_kind", "ema")), int(p["base"]), int(p["step"])
    count = int(p.get("count", 6))
    mas = [ind.moving_average(frame["close"], base + step * i, kind) for i in range(count)]
    stacked_up = pd.Series(True, index=frame.index)
    stacked_dn = pd.Series(True, index=frame.index)
    for a, b in zip(mas, mas[1:]):
        stacked_up &= a > b
        stacked_dn &= a < b
    return Signal(_long_short(stacked_up, stacked_dn, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


@rule("trend.macd")
def macd_rule(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    line, sig, _ = ind.macd(frame["close"], int(p["fast"]), int(p["slow"]), int(p["signal"]))
    if str(p.get("trigger", "signal")) == "zero":
        long_c, short_c = line > 0, line < 0
    else:
        long_c, short_c = line > sig, line < sig
    return Signal(_long_short(long_c, short_c, bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.adx_filtered_ma")
def adx_filtered_ma(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    adx_line, plus, minus = ind.adx(frame, int(p.get("adx_length", 14)))
    strong = adx_line > float(p.get("adx_threshold", 25))
    return Signal(_long_short(strong & (plus > minus), strong & (minus > plus),
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.supertrend")
def supertrend_rule(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    direction = ind.supertrend(frame, int(p.get("length", 10)), float(p.get("multiplier", 3.0)))
    return Signal(_long_short(direction > 0, direction < 0, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


@rule("trend.parabolic_sar")
def psar_rule(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    sar = ind.parabolic_sar(frame, float(p.get("step", 0.02)), float(p.get("maximum", 0.2)))
    return Signal(_long_short(frame["close"] > sar, frame["close"] < sar,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.ichimoku")
def ichimoku_rule(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    tenkan, kijun, span_a, span_b = ind.ichimoku(
        frame, int(p.get("conversion", 9)), int(p.get("base", 26)), int(p.get("span", 52)))
    above = (frame["close"] > span_a) & (frame["close"] > span_b)
    below = (frame["close"] < span_a) & (frame["close"] < span_b)
    if bool(p.get("require_tk_cross", True)):
        above &= tenkan > kijun
        below &= tenkan < kijun
    return Signal(_long_short(above, below, bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.linreg_slope")
def linreg_slope_rule(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    slope = ind.linear_regression_slope(frame["close"], int(p.get("length", 20)))
    normalised = slope / frame["close"] * 100.0
    threshold = float(p.get("threshold", 0.0))
    return Signal(_long_short(normalised > threshold, normalised < -threshold,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("trend.donchian_breakout")
def donchian_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    entry, exit_len = int(p["entry_length"]), int(p["exit_length"])
    upper = frame["high"].rolling(entry, min_periods=entry).max().shift(1)
    lower = frame["low"].rolling(entry, min_periods=entry).min().shift(1)
    exit_low = frame["low"].rolling(exit_len, min_periods=exit_len).min().shift(1)
    exit_high = frame["high"].rolling(exit_len, min_periods=exit_len).max().shift(1)
    allow_short = bool(p.get("allow_short", False))
    position = _state_from_events(
        frame.index,
        frame["close"] > upper, frame["close"] < exit_low,
        (frame["close"] < lower) if allow_short else None,
        (frame["close"] > exit_high) if allow_short else None,
    )
    return Signal(position, **_risk(p, frame))


# ============================================================ mean reversion
@rule("reversion.rsi")
def rsi_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.rsi(frame["close"], int(p.get("length", 14)))
    low, high = float(p.get("oversold", 30)), float(p.get("overbought", 70))
    exit_level = float(p.get("exit_level", 50))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, value < low, value > exit_level,
        (value > high) if allow_short else None,
        (value < exit_level) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.bollinger")
def bollinger_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    lower, mid, upper = ind.bollinger(frame["close"], int(p.get("length", 20)),
                                      float(p.get("deviations", 2.0)))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, frame["close"] < lower, frame["close"] > mid,
        (frame["close"] > upper) if allow_short else None,
        (frame["close"] < mid) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.zscore")
def zscore_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    z = ind.zscore(frame["close"], int(p.get("length", 20)))
    entry, exit_z = float(p.get("entry_z", 2.0)), float(p.get("exit_z", 0.5))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, z < -entry, z > -exit_z,
        (z > entry) if allow_short else None, (z < exit_z) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.stochastic")
def stochastic_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    k, d = ind.stochastic(frame, int(p.get("length", 14)), int(p.get("smooth", 3)))
    low, high = float(p.get("oversold", 20)), float(p.get("overbought", 80))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, (k < low) & (k > d), k > 50.0,
        ((k > high) & (k < d)) if allow_short else None,
        (k < 50.0) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.williams_r")
def williams_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.williams_r(frame, int(p.get("length", 14)))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, value < float(p.get("oversold", -80)), value > -50.0,
        (value > float(p.get("overbought", -20))) if allow_short else None,
        (value < -50.0) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.cci")
def cci_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.cci(frame, int(p.get("length", 20)))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, value < float(p.get("oversold", -100)), value > 0.0,
        (value > float(p.get("overbought", 100))) if allow_short else None,
        (value < 0.0) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.mfi")
def mfi_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.money_flow_index(frame, int(p.get("length", 14)))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, value < float(p.get("oversold", 20)), value > 50.0,
        (value > float(p.get("overbought", 80))) if allow_short else None,
        (value < 50.0) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.vwap_band")
def vwap_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    vwap = ind.rolling_vwap(frame, int(p.get("length", 20)))
    spread = (frame["close"] - vwap) / vwap * 100.0
    band = float(p.get("band_pct", 2.0))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, spread < -band, spread > 0.0,
        (spread > band) if allow_short else None, (spread < 0.0) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.ma_distance")
def ma_distance_reversion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    ma = ind.moving_average(frame["close"], int(p.get("length", 50)), str(p.get("ma_kind", "sma")))
    stretch = (frame["close"] - ma) / ma * 100.0
    band = float(p.get("stretch_pct", 5.0))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_state_from_events(
        frame.index, stretch < -band, stretch > 0.0,
        (stretch > band) if allow_short else None, (stretch < 0.0) if allow_short else None,
    ), **_risk(p, frame))


@rule("reversion.consecutive_bars")
def consecutive_bars(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    count = int(p.get("count", 3))
    down = frame["close"] < frame["close"].shift(1)
    streak_down = down.rolling(count, min_periods=count).sum() == count
    up = frame["close"] > frame["close"].shift(1)
    streak_up = up.rolling(count, min_periods=count).sum() == count
    allow_short = bool(p.get("allow_short", False))
    exit_after = int(p.get("max_bars_held", 5))
    return Signal(_state_from_events(
        frame.index, streak_down, streak_up,
        streak_up if allow_short else None, streak_down if allow_short else None,
    ), **{**_risk(p, frame), "max_bars_held": exit_after})


@rule("reversion.short_term_reversal")
def short_term_reversal(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    lookback = int(p.get("lookback", 5))
    change = frame["close"].pct_change(lookback) * 100.0
    threshold = float(p.get("threshold_pct", 5.0))
    allow_short = bool(p.get("allow_short", False))
    return Signal(_long_short(change < -threshold, change > threshold, allow_short),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", lookback))})


# =================================================================== breakout
@rule("breakout.price_channel")
def price_channel_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    length = int(p.get("length", 20))
    upper = frame["high"].rolling(length, min_periods=length).max().shift(1)
    lower = frame["low"].rolling(length, min_periods=length).min().shift(1)
    return Signal(_long_short(frame["close"] > upper, frame["close"] < lower,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("breakout.narrow_range")
def narrow_range_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    count = int(p.get("count", 7))
    span = frame["high"] - frame["low"]
    narrowest = span == span.rolling(count, min_periods=count).min()
    trigger_high = frame["high"].where(narrowest).ffill()
    trigger_low = frame["low"].where(narrowest).ffill()
    armed = narrowest.shift(1).fillna(False)
    return Signal(_long_short(armed & (frame["close"] > trigger_high.shift(1)),
                              armed & (frame["close"] < trigger_low.shift(1)),
                              bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 5))})


@rule("breakout.inside_bar")
def inside_bar_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    inside = (frame["high"] < frame["high"].shift(1)) & (frame["low"] > frame["low"].shift(1))
    mother_high = frame["high"].shift(1).where(inside).ffill()
    mother_low = frame["low"].shift(1).where(inside).ffill()
    armed = inside.shift(1).fillna(False)
    return Signal(_long_short(armed & (frame["close"] > mother_high),
                              armed & (frame["close"] < mother_low),
                              bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 5))})


@rule("breakout.volatility")
def volatility_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    band = ind.atr(frame, int(p.get("atr_length", 14))) * float(p.get("multiplier", 1.5))
    reference = frame["close"].shift(1)
    return Signal(_long_short(frame["close"] > reference + band,
                              frame["close"] < reference - band,
                              bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 10))})


@rule("breakout.squeeze")
def squeeze_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    length = int(p.get("length", 20))
    bb_low, _, bb_high = ind.bollinger(frame["close"], length, float(p.get("deviations", 2.0)))
    kc_low, _, kc_high = ind.keltner(frame, length, float(p.get("multiplier", 1.5)))
    squeezed = (bb_high < kc_high) & (bb_low > kc_low)
    released = squeezed.shift(1).fillna(False) & ~squeezed.fillna(False)
    momentum = ind.linear_regression_slope(frame["close"], length)
    return Signal(_long_short(released & (momentum > 0), released & (momentum < 0),
                              bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 10))})


@rule("breakout.gap")
def gap_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    gap = (frame["open"] - frame["close"].shift(1)) / frame["close"].shift(1) * 100.0
    threshold = float(p.get("gap_pct", 2.0))
    fade = bool(p.get("fade", False))
    up, down = gap > threshold, gap < -threshold
    long_c, short_c = (down, up) if fade else (up, down)
    return Signal(_long_short(long_c, short_c, bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 3))})


@rule("breakout.prior_period_high")
def prior_period_high(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    length = int(p.get("length", 252))
    high = frame["high"].rolling(length, min_periods=min(length, 60)).max().shift(1)
    low = frame["low"].rolling(length, min_periods=min(length, 60)).min().shift(1)
    return Signal(_long_short(frame["close"] >= high, frame["close"] <= low,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


# =================================================================== momentum
@rule("momentum.rate_of_change")
def roc_momentum(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.roc(frame["close"], int(p.get("length", 12)))
    threshold = float(p.get("threshold", 0.0))
    return Signal(_long_short(value > threshold, value < -threshold,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("momentum.time_series")
def time_series_momentum(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    lookback = int(p.get("lookback", 252))
    skip = int(p.get("skip", 0))
    past = frame["close"].shift(skip)
    change = past / past.shift(lookback) - 1.0
    return Signal(_long_short(change > 0, change < 0, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


@rule("momentum.rsi_trend")
def rsi_trend(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.rsi(frame["close"], int(p.get("length", 14)))
    level = float(p.get("level", 50))
    return Signal(_long_short(value > level, value < level, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


@rule("momentum.volatility_scaled")
def volatility_scaled_momentum(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    lookback = int(p.get("lookback", 126))
    change = frame["close"].pct_change(lookback)
    vol = ind.historical_volatility(frame["close"], int(p.get("vol_length", 20)))
    score = change / vol.replace(0.0, np.nan)
    threshold = float(p.get("threshold", 0.0))
    return Signal(_long_short(score > threshold, score < -threshold,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


# ===================================================== candlesticks / action
def _trend_context(frame: pd.DataFrame, length: int) -> tuple[pd.Series, pd.Series]:
    ma = ind.sma(frame["close"], length)
    return frame["close"] < ma, frame["close"] > ma


@rule("price-action.candlestick")
def candlestick(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    pattern = str(p["pattern"])
    o, h, l, c = frame["open"], frame["high"], frame["low"], frame["close"]
    body, rng = ind.body(frame), ind.candle_range(frame)
    upper, lower = ind.upper_shadow(frame), ind.lower_shadow(frame)
    bull, bear = c > o, c < o
    prev_bull, prev_bear = bull.shift(1), bear.shift(1)
    down_ctx, up_ctx = _trend_context(frame, int(p.get("context_length", 20)))
    small = body <= rng * float(p.get("small_body_ratio", 0.35))

    if pattern == "bullish-engulfing":
        cond = bull & prev_bear & (c >= o.shift(1)) & (o <= c.shift(1)) & down_ctx
        bear_cond = bear & prev_bull & (c <= o.shift(1)) & (o >= c.shift(1)) & up_ctx
    elif pattern == "hammer":
        cond = (lower >= body * 2.0) & (upper <= body) & (body > 0) & down_ctx
        bear_cond = (upper >= body * 2.0) & (lower <= body) & (body > 0) & up_ctx
    elif pattern == "shooting-star":
        cond = (lower >= body * 2.0) & (upper <= body) & (body > 0) & down_ctx
        bear_cond = (upper >= body * 2.0) & (lower <= body) & (body > 0) & up_ctx
    elif pattern == "doji":
        doji = body <= rng * float(p.get("doji_ratio", 0.1))
        cond, bear_cond = doji & down_ctx, doji & up_ctx
    elif pattern == "morning-star":
        cond = (prev_bear.shift(1).fillna(False) & small.shift(1).fillna(False) & bull
                & (c > (o.shift(2) + c.shift(2)) / 2.0) & down_ctx)
        bear_cond = (prev_bull.shift(1).fillna(False) & small.shift(1).fillna(False) & bear
                     & (c < (o.shift(2) + c.shift(2)) / 2.0) & up_ctx)
    elif pattern == "harami":
        cond = (prev_bear & bull & (h <= h.shift(1)) & (l >= l.shift(1)) & down_ctx)
        bear_cond = (prev_bull & bear & (h <= h.shift(1)) & (l >= l.shift(1)) & up_ctx)
    elif pattern == "piercing":
        cond = (prev_bear & bull & (o < c.shift(1))
                & (c > (o.shift(1) + c.shift(1)) / 2.0) & (c < o.shift(1)) & down_ctx)
        bear_cond = (prev_bull & bear & (o > c.shift(1))
                     & (c < (o.shift(1) + c.shift(1)) / 2.0) & (c > o.shift(1)) & up_ctx)
    elif pattern == "three-soldiers":
        three_up = bull & prev_bull & bull.shift(2).fillna(False)
        rising = (c > c.shift(1)) & (c.shift(1) > c.shift(2))
        cond = three_up & rising & down_ctx
        three_dn = bear & prev_bear & bear.shift(2).fillna(False)
        falling = (c < c.shift(1)) & (c.shift(1) < c.shift(2))
        bear_cond = three_dn & falling & up_ctx
    elif pattern == "pin-bar":
        cond = (lower >= rng * 0.6) & down_ctx
        bear_cond = (upper >= rng * 0.6) & up_ctx
    elif pattern == "outside-bar":
        outside = (h > h.shift(1)) & (l < l.shift(1))
        cond, bear_cond = outside & bull & down_ctx, outside & bear & up_ctx
    elif pattern == "marubozu":
        solid = (body >= rng * 0.9)
        cond, bear_cond = solid & bull & down_ctx, solid & bear & up_ctx
    else:
        raise ValueError(f"unknown candlestick pattern: {pattern}")

    allow_short = bool(p.get("allow_short", False))
    return Signal(_long_short(cond, bear_cond, allow_short),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 5))})


@rule("price-action.structure")
def market_structure(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    length = int(p.get("length", 10))
    swing_high = frame["high"].rolling(length, min_periods=length).max()
    swing_low = frame["low"].rolling(length, min_periods=length).min()
    higher_high = swing_high > swing_high.shift(length)
    higher_low = swing_low > swing_low.shift(length)
    lower_high = swing_high < swing_high.shift(length)
    lower_low = swing_low < swing_low.shift(length)
    return Signal(_long_short(higher_high & higher_low, lower_high & lower_low,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


# ===================================================================== volume
@rule("volume.obv_trend")
def obv_trend(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    line = ind.obv(frame)
    ma = ind.sma(line, int(p.get("length", 20)))
    return Signal(_long_short(line > ma, line < ma, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


@rule("volume.chaikin_money_flow")
def cmf_rule(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    value = ind.chaikin_money_flow(frame, int(p.get("length", 20)))
    threshold = float(p.get("threshold", 0.05))
    return Signal(_long_short(value > threshold, value < -threshold,
                              bool(p.get("allow_short", False))), **_risk(p, frame))


@rule("volume.relative_volume_breakout")
def rvol_breakout(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    rvol = ind.relative_volume(frame, int(p.get("length", 20)))
    hot = rvol > float(p.get("multiple", 2.0))
    length = int(p.get("breakout_length", 20))
    upper = frame["high"].rolling(length, min_periods=length).max().shift(1)
    lower = frame["low"].rolling(length, min_periods=length).min().shift(1)
    return Signal(_long_short(hot & (frame["close"] > upper), hot & (frame["close"] < lower),
                              bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 10))})


@rule("volume.accumulation_trend")
def accumulation_trend(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    line = ind.accumulation_distribution(frame)
    ma = ind.sma(line, int(p.get("length", 20)))
    return Signal(_long_short(line > ma, line < ma, bool(p.get("allow_short", False))),
                  **_risk(p, frame))


# ================================================================= volatility
@rule("volatility.regime_filter")
def volatility_regime(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    vol = ind.historical_volatility(frame["close"], int(p.get("length", 20)))
    rank = vol.rolling(int(p.get("rank_length", 252)), min_periods=60).rank(pct=True)
    trend = ind.sma(frame["close"], int(p.get("trend_length", 100)))
    calm = rank < float(p.get("max_percentile", 0.5))
    return Signal(_long_short(calm & (frame["close"] > trend), pd.Series(False, index=frame.index),
                              False), **_risk(p, frame))


@rule("volatility.atr_expansion")
def atr_expansion(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    fast = ind.atr(frame, int(p.get("fast", 5)))
    slow = ind.atr(frame, int(p.get("slow", 20)))
    expanding = fast > slow * float(p.get("ratio", 1.2))
    ma = ind.sma(frame["close"], int(p.get("trend_length", 20)))
    return Signal(_long_short(expanding & (frame["close"] > ma),
                              expanding & (frame["close"] < ma),
                              bool(p.get("allow_short", False))),
                  **{**_risk(p, frame), "max_bars_held": int(p.get("max_bars_held", 10))})


# =================================================================== seasonal
def _calendar(frame: pd.DataFrame) -> pd.DatetimeIndex:
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("seasonal strategies need a datetime index")
    return frame.index


@rule("seasonal.turn_of_month")
def turn_of_month(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    index = _calendar(frame)
    before, after = int(p.get("days_before", 3)), int(p.get("days_after", 3))
    month_key = index.year * 12 + index.month
    position = pd.Series(0.0, index=index)
    order = pd.Series(range(len(index)), index=index)
    from_end = order.groupby(month_key).transform("max") - order
    from_start = order - order.groupby(month_key).transform("min")
    position[(from_end < before) | (from_start < after)] = 1.0
    return Signal(position)


@rule("seasonal.day_of_week")
def day_of_week(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    index = _calendar(frame)
    target = int(p.get("weekday", 0))
    position = pd.Series(0.0, index=index)
    position[index.weekday == target] = -1.0 if bool(p.get("short", False)) else 1.0
    return Signal(position)


@rule("seasonal.month_window")
def month_window(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    index = _calendar(frame)
    months = {int(m) for m in str(p.get("months", "11,12,1,2,3,4")).split(",")}
    position = pd.Series(0.0, index=index)
    position[[m in months for m in index.month]] = 1.0
    return Signal(position)


# ============================================================== benchmark/mix
@rule("benchmark.buy_and_hold")
def buy_and_hold(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    return Signal(pd.Series(1.0, index=frame.index))


@rule("allocation.trend_filtered")
def trend_filtered_allocation(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    ma = ind.moving_average(frame["close"], int(p.get("length", 200)), str(p.get("ma_kind", "sma")))
    monthly = pd.Series(0.0, index=frame.index)
    monthly[frame["close"] > ma] = 1.0
    if bool(p.get("month_end_only", True)):
        index = _calendar(frame)
        order = pd.Series(range(len(index)), index=index)
        month_key = index.year * 12 + index.month
        is_month_end = order == order.groupby(month_key).transform("max")
        monthly = monthly.where(is_month_end).ffill().fillna(0.0)
    return Signal(monthly)


@rule("combo.confirmed_trend")
def confirmed_trend(frame: pd.DataFrame, p: dict[str, Any]) -> Signal:
    """Trend direction plus an independent oscillator and volume confirmation."""
    ma_fast = ind.moving_average(frame["close"], int(p.get("fast", 20)), str(p.get("ma_kind", "ema")))
    ma_slow = ind.moving_average(frame["close"], int(p.get("slow", 50)), str(p.get("ma_kind", "ema")))
    osc = ind.rsi(frame["close"], int(p.get("rsi_length", 14)))
    rvol = ind.relative_volume(frame, int(p.get("volume_length", 20)))
    confirm = rvol > float(p.get("min_relative_volume", 1.0))
    long_c = (ma_fast > ma_slow) & (osc > float(p.get("rsi_level", 50))) & confirm
    short_c = (ma_fast < ma_slow) & (osc < float(p.get("rsi_level", 50))) & confirm
    return Signal(_long_short(long_c, short_c, bool(p.get("allow_short", False))), **_risk(p, frame))


def run_rule(rule_id: str, frame: pd.DataFrame, params: dict[str, Any]) -> Signal:
    try:
        func = RULES[rule_id]
    except KeyError as exc:
        raise ValueError(f"unknown rule id: {rule_id}") from exc
    return func(frame, params)
