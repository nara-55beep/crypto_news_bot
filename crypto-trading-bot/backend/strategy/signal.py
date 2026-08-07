"""
signal.py — STRATEGY V2 (rule-based, backtestable). Replaces the old V1.

Long when ALL hold (short = mirror):
  1. 4H trend up   : 4H close > EMA200 and EMA50 > EMA200
  2. trend strength: 4H ADX >= adx_thresh           (only trade real trends)
  3. not overextended: price within overext_atr * ATR of the 15m EMA50 (no chasing)
  4. regime ok     : ATR above its rolling percentile floor (skip dead markets)
                     AND the previous candle was not a giant spike (> giant_mult*ATR)
  5. trigger       : structure break (close beyond prior N-bar high/low)
                     OR pullback-continuation (near EMA50 + candle closes our way)

Trade management lives in the engines (backtest/paper/live): take partial at +1R,
move stop to breakeven, ATR-trail the rest, and a time stop. Same code runs in
backtest, paper and live — no look-ahead, no hidden state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from backend.strategy import indicators as ind


@dataclass
class Signal:
    side: str                       # "long" | "short"
    price: float
    time: str
    atr: float
    symbol: str = ""
    reason: str = ""
    meta: dict = field(default_factory=dict)


class Strategy:
    """Strategy V2. `prepare()` builds the feature frame; `evaluate_row()` applies
    the rules; `latest_signal()` is the live/paper entry point."""

    def __init__(self, settings):
        self.s = settings

    # ---- build the entry frame with 4H trend + ADX merged in -----------------
    def prepare(self, htf_df: pd.DataFrame, entry_df: pd.DataFrame) -> pd.DataFrame:
        s = self.s
        e = entry_df.copy()
        e["ema50"] = ind.ema(e["close"], s.ema_fast)
        e["ema20"] = ind.ema(e["close"], 20)
        e["atr"] = ind.atr(e, s.atr_period)
        e["atr_pct"] = e["atr"] / e["close"]
        e["dist_ema"] = (e["close"] - e["ema50"]).abs()
        e["range"] = e["high"] - e["low"]
        e["prev_range"] = e["range"].shift(1)
        e["hh"] = e["high"].rolling(s.breakout_lookback).max().shift(1)
        e["ll"] = e["low"].rolling(s.breakout_lookback).min().shift(1)
        e["atr_floor"] = e["atr"].shift(1).rolling(s.atr_floor_win).quantile(s.atr_floor_q)

        # 4H trend (+1/-1/0) and 4H ADX, mapped onto each entry bar (most recent CLOSED 4H bar)
        trend = ind.htf_trend(htf_df, s).rename("trend_htf")
        adx_htf = ind.adx(htf_df, s.adx_period).rename("adx_htf")
        h = pd.concat([trend, adx_htf], axis=1).reset_index()
        h.columns = ["dt", "trend_htf", "adx_htf"]
        left = e.reset_index()
        left.columns = ["dt"] + list(left.columns[1:])
        # Normalise datetime resolution on BOTH merge keys. A freshly-fetched frame can be
        # ms-resolution while a disk-cached one is us/ns; merge_asof requires identical dtypes.
        left["dt"] = pd.to_datetime(left["dt"], utc=True).dt.as_unit("ns")
        h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.as_unit("ns")
        merged = pd.merge_asof(left.sort_values("dt"), h.sort_values("dt"), on="dt", direction="backward")
        merged = merged.set_index("dt")
        merged["trend_htf"] = merged["trend_htf"].fillna(0).astype(int)
        merged["adx_htf"] = merged["adx_htf"].fillna(0.0)
        return merged

    # ---- per-bar filter checks ----------------------------------------------
    def _filters(self, row, side: str) -> dict:
        s, atr = self.s, row["atr"]
        if side == "long":
            trig = (row["close"] > row["hh"]) or (
                row["dist_ema"] <= s.pullback_atr * atr and row["close"] > row["open"]
            )
        else:
            trig = (row["close"] < row["ll"]) or (
                row["dist_ema"] <= s.pullback_atr * atr and row["close"] < row["open"]
            )
        return {
            "trigger": bool(trig),
            "adx": row["adx_htf"] >= s.adx_thresh,
            "not_overextended": row["dist_ema"] <= s.overext_atr * atr,
            "atr_regime": (atr == atr) and (atr >= row["atr_floor"]),
            "not_giant": row["prev_range"] <= s.giant_mult * atr,
            "volatility": (row["atr_pct"] == row["atr_pct"]) and (s.min_atr_pct <= row["atr_pct"] <= s.max_atr_pct),
        }

    def evaluate_row(self, row, symbol: str = "", funding: Optional[float] = None) -> Optional[Signal]:
        if pd.isna(row.get("atr")) or pd.isna(row.get("atr_floor")):
            return None
        for side, want in (("long", 1), ("short", -1)):
            if row["trend_htf"] != want:
                continue
            f = self._filters(row, side)
            if not all(f.values()):
                continue
            # optional funding filter (only when enabled and data present)
            if self.s.use_funding_filter and funding is not None:
                if side == "long" and funding > self.s.max_funding:
                    continue
                if side == "short" and funding < -self.s.max_funding:
                    continue
            return Signal(
                side=side, price=float(row["close"]), time=str(row.name), atr=float(row["atr"]),
                symbol=symbol,
                reason=f"V2 {side} · 4H trend · ADX {row['adx_htf']:.0f} · "
                       f"{'breakout' if (row['close']>row['hh'] if side=='long' else row['close']<row['ll']) else 'pullback'}",
                meta={"adx_htf": round(float(row["adx_htf"]), 1),
                      "atr_pct": round(float(row["atr_pct"]) * 100, 3),
                      "dist_ema_atr": round(float(row["dist_ema"] / row["atr"]), 2) if row["atr"] else None},
            )
        return None

    def latest_signal(self, htf_df, entry_df, symbol: str = "", funding: Optional[float] = None) -> Optional[Signal]:
        prepared = self.prepare(htf_df, entry_df)
        if len(prepared) < 3:
            return None
        return self.evaluate_row(prepared.iloc[-2], symbol=symbol, funding=funding)  # last CLOSED bar


class StrategyV3:
    """STRATEGY V3 — Glen Goodman's daily trend-following, mechanised (see
    STRATEGY_goodman_v3.md). Long-only. Buy a Donchian breakout from a base while the
    asset is above its RISING long MA, on expanding volume; ride it with a chandelier
    trailing stop until the trend bends (handled in the engines). Same interface as
    Strategy so the backtest/paper engines can use either."""

    def __init__(self, settings):
        self.s = settings

    def prepare(self, htf_df: pd.DataFrame, entry_df: pd.DataFrame) -> pd.DataFrame:
        s = self.s
        d = entry_df.copy()                                   # daily frame
        d["ema_regime"] = ind.ema(d["close"], s.v3_regime_ma)
        d["regime_prev"] = d["ema_regime"].shift(5)           # for the rising-MA slope check
        d["atr"] = ind.atr(d, s.atr_period)
        d["donch_hi"] = d["high"].rolling(s.v3_donchian).max().shift(1)   # prior N-day high
        d["vol_ma"] = d["volume"].rolling(20).mean()
        return d

    def evaluate_row(self, row, symbol: str = "", funding: Optional[float] = None) -> Optional[Signal]:
        s = self.s
        if pd.isna(row.get("atr")) or pd.isna(row.get("donch_hi")) or pd.isna(row.get("ema_regime")):
            return None
        rising = row["ema_regime"] > (row["regime_prev"] if row["regime_prev"] == row["regime_prev"] else row["ema_regime"])
        regime_ok = (row["close"] > row["ema_regime"]) and rising      # above a RISING long MA
        breakout = row["close"] > row["donch_hi"]                      # Donchian breakout
        vol_ok = (row["vol_ma"] == row["vol_ma"]) and (row["volume"] > s.v3_vol_mult * row["vol_ma"])
        if regime_ok and breakout and vol_ok:
            return Signal(
                side="long", price=float(row["close"]), time=str(row.name), atr=float(row["atr"]),
                symbol=symbol,
                reason=f"V3 long · {s.v3_donchian}d Donchian breakout · above rising {s.v3_regime_ma}d MA · vol>avg",
                meta={"donch_hi": round(float(row["donch_hi"]), 2),
                      "regime_ma": round(float(row["ema_regime"]), 2)},
            )
        return None  # shorts are off by default (ch.12)

    def latest_signal(self, htf_df, entry_df, symbol: str = "", funding: Optional[float] = None) -> Optional[Signal]:
        prepared = self.prepare(htf_df, entry_df)
        if len(prepared) < 3:
            return None
        return self.evaluate_row(prepared.iloc[-2], symbol=symbol, funding=funding)


class StrategyV4:
    """STRATEGY V4 — Momentum Sweep VWAP Breakout (intraday, 20x).

    Two LONG setups (shorts mirror):
      * Breakout: close > VWAP, EMA-fast > EMA-slow and RISING, close breaks the prior
        v4_break_lookback high, on volume > v4_vol_mult_breakout * volume-MA.
      * Liquidity sweep: the bar dips below the prior v4_sweep_lookback low but CLOSES
        back above it as a bullish candle on strong volume, with close > EMA-fast.

    Exits + sizing live in the engines (backtest/paper): ATR stop, fixed R-multiple
    target, an N-bar cooldown, and 20x all-in notional. Same prepare()/evaluate_row()
    interface as Strategy/StrategyV3, so the engines can drive any version uniformly.
    """

    def __init__(self, settings):
        self.s = settings

    def prepare(self, htf_df: pd.DataFrame, entry_df: pd.DataFrame) -> pd.DataFrame:
        s = self.s
        d = entry_df.copy()                                   # single timeframe (htf ignored)
        d["ema_fast"] = ind.ema(d["close"], s.v4_ema_fast)
        d["ema_slow"] = ind.ema(d["close"], s.v4_ema_slow)
        d["vwap"] = ind.session_vwap(d)                       # daily-reset, typical-price VWAP
        d["vol_ma"] = d["volume"].rolling(s.v4_vol_ma).mean()
        d["ph_break"] = d["high"].rolling(s.v4_break_lookback).max().shift(1)
        d["pl_break"] = d["low"].rolling(s.v4_break_lookback).min().shift(1)
        d["ph_sweep"] = d["high"].rolling(s.v4_sweep_lookback).max().shift(1)
        d["pl_sweep"] = d["low"].rolling(s.v4_sweep_lookback).min().shift(1)
        # ATR = simple moving average of True Range (matches the reference backtest; not Wilder)
        prev_close = d["close"].shift(1)
        tr = pd.concat([(d["high"] - d["low"]),
                        (d["high"] - prev_close).abs(),
                        (d["low"] - prev_close).abs()], axis=1).max(axis=1)
        d["atr"] = tr.rolling(s.v4_atr_period).mean()

        rising = d["ema_fast"] > d["ema_fast"].shift(5)
        falling = d["ema_fast"] < d["ema_fast"].shift(5)
        vol, vm = d["volume"], d["vol_ma"]
        d["long_breakout"] = ((d["close"] > d["vwap"]) & (d["ema_fast"] > d["ema_slow"]) & rising &
                              (d["close"] > d["ph_break"]) & (vol > vm * s.v4_vol_mult_breakout))
        d["short_breakout"] = ((d["close"] < d["vwap"]) & (d["ema_fast"] < d["ema_slow"]) & falling &
                               (d["close"] < d["pl_break"]) & (vol > vm * s.v4_vol_mult_breakout))
        d["long_sweep"] = ((d["low"] < d["pl_sweep"]) & (d["close"] > d["pl_sweep"]) & (d["close"] > d["open"]) &
                           (vol > vm * s.v4_vol_mult_sweep) & (d["close"] > d["ema_fast"]))
        d["short_sweep"] = ((d["high"] > d["ph_sweep"]) & (d["close"] < d["ph_sweep"]) & (d["close"] < d["open"]) &
                            (vol > vm * s.v4_vol_mult_sweep) & (d["close"] < d["ema_fast"]))
        return d

    def evaluate_row(self, row, symbol: str = "", funding: Optional[float] = None) -> Optional[Signal]:
        if pd.isna(row.get("atr")) or pd.isna(row.get("vol_ma")):
            return None
        long_sig = bool(row.get("long_breakout")) or bool(row.get("long_sweep"))
        short_sig = bool(row.get("short_breakout")) or bool(row.get("short_sweep"))
        side = "long" if long_sig else ("short" if short_sig else None)
        if side is None:
            return None
        is_breakout = bool(row.get("long_breakout")) if side == "long" else bool(row.get("short_breakout"))
        return Signal(
            side=side, price=float(row["close"]), time=str(row.name), atr=float(row["atr"]),
            symbol=symbol,
            reason=f"V4 {side} · {'breakout' if is_breakout else 'liquidity sweep'} · VWAP+EMA trend · volume spike",
            meta={"setup": "breakout" if is_breakout else "sweep",
                  "vwap": round(float(row["vwap"]), 2) if row.get("vwap") == row.get("vwap") else None},
        )

    def latest_signal(self, htf_df, entry_df, symbol: str = "", funding: Optional[float] = None) -> Optional[Signal]:
        prepared = self.prepare(htf_df, entry_df)
        if len(prepared) < 3:
            return None
        return self.evaluate_row(prepared.iloc[-2], symbol=symbol, funding=funding)  # last CLOSED bar


def make_strategy(settings):
    """Return the strategy object for the configured version (v2 default, v3 = Goodman
    daily, v4 = Momentum Sweep VWAP Breakout)."""
    v = getattr(settings, "strategy_version", "v2")
    if v == "v3":
        return StrategyV3(settings)
    if v == "v4":
        return StrategyV4(settings)
    return Strategy(settings)
