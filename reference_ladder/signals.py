"""Pluggable reference-signal implementations.

The reference signal records direction and the completed signal bar's close. It
does not open a real position. The ladder engine is deliberately independent of
the signal generator so another causal trigger can be substituted later.
"""
from __future__ import annotations

from typing import Protocol

import numpy as np
import pandas as pd

from .config import LadderConfig


class ReferenceSignal(Protocol):
    name: str

    def generate(self, frame: pd.DataFrame, config: LadderConfig) -> pd.Series:
        """Return -1/0/+1 on each completed bar."""


def rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    average_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    average_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    ratio = average_gain / average_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + ratio)).fillna(50.0)


class BollingerRsiSmaSignal:
    """The repository's existing BTC mean-reversion entry, made pluggable.

    Default long signal: the low touches the lower 20/2 Bollinger Band, RSI is
    below 35, and the close remains above the 200-period SMA. Optional shorts
    mirror the rule but are disabled because the existing paper bot is long-only.
    """

    name = "bollinger-rsi-sma"

    def generate(self, frame: pd.DataFrame, config: LadderConfig) -> pd.Series:
        close = frame["close"].astype(float)
        middle = close.rolling(config.bb_length, min_periods=config.bb_length).mean()
        deviation = close.rolling(config.bb_length, min_periods=config.bb_length).std()
        lower = middle - config.bb_deviations * deviation
        upper = middle + config.bb_deviations * deviation
        strength = rsi(close, config.rsi_length)
        trend = close.rolling(
            config.trend_sma_length, min_periods=config.trend_sma_length,
        ).mean()
        long_signal = (
            (frame["low"].astype(float) <= lower)
            & (strength < config.rsi_oversold)
            & (close > trend)
        )
        short_signal = pd.Series(False, index=frame.index)
        if config.allow_short_signals:
            short_signal = (
                (frame["high"].astype(float) >= upper)
                & (strength > config.rsi_overbought)
                & (close < trend)
            )
        if config.regime_filter:
            change = close.pct_change(config.regime_slope_lookback).abs() * 100.0
            regime_ok = change <= config.max_regime_slope_pct
            long_signal &= regime_ok
            short_signal &= regime_ok
        result = pd.Series(0, index=frame.index, dtype=np.int8)
        result.loc[long_signal.fillna(False)] = 1
        result.loc[short_signal.fillna(False)] = -1
        return result
