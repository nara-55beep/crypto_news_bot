"""
Pre-open regime filters for the causal NQ daily-ATR breakout.

All filters are fixed before the 09:30 New York open.  This module intentionally
does not use the current session's eventual range, close, or realized volatility.
Base strategies are the nearby parameterizations that survived development in
``lucid_fast_alpha_research.py``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_fast_alpha_research as F


@dataclass(frozen=True)
class Regime:
    name: str
    rules: tuple[str, ...]


SINGLE_REGIMES = (
    Regime("none", ()),
    Regime("long", ("long",)),
    Regime("short", ("short",)),
    Regime("trend_align", ("trend_align",)),
    Regime("trend_counter", ("trend_counter",)),
    Regime("gap_align", ("gap_align",)),
    Regime("gap_counter", ("gap_counter",)),
    Regime("gap_small", ("gap_small",)),
    Regime("prev_align", ("prev_align",)),
    Regime("prev_counter", ("prev_counter",)),
    Regime("vol_low", ("vol_low",)),
    Regime("vol_high", ("vol_high",)),
    Regime("nr4", ("nr4",)),
    Regime("compression", ("compression",)),
    Regime("ibs_align", ("ibs_align",)),
    Regime("ibs_counter", ("ibs_counter",)),
    # Small two-factor follow-up set.  These combine the development-period
    # gap-alignment leader with independent pre-open state variables; there is
    # deliberately no arbitrary threshold grid.
    Regime("gap_align_vol_high", ("gap_align", "vol_high")),
    Regime("gap_align_trend_align", ("gap_align", "trend_align")),
    Regime("gap_align_nr4", ("gap_align", "nr4")),
    Regime("gap_align_short", ("gap_align", "short")),
    Regime("vol_high_trend_align", ("vol_high", "trend_align")),
    Regime("nr4_trend_align", ("nr4", "trend_align")),
)


BASES = (
    F.FastConfig("nq", 10, 0.35, "immediate", "immediate", 1, 0.0),
    F.FastConfig("nq", 14, 0.35, "immediate", "immediate", 1, 0.0),
    F.FastConfig("nq", 20, 0.35, "immediate", "immediate", 1, 0.0),
    F.FastConfig("nq", 10, 0.50, "immediate", "immediate", 1, 0.0),
    F.FastConfig("nq", 14, 0.50, "immediate", "immediate", 1, 0.0),
    F.FastConfig("nq", 20, 0.50, "immediate", "immediate", 1, 0.0),
)


def _features(days: list[L.Day]) -> dict[date, dict[str, float | bool]]:
    closes = np.asarray([float(day.cl[-1]) for day in days])
    opens = np.asarray([float(day.op[0]) for day in days])
    highs = np.asarray([float(np.max(day.hi)) for day in days])
    lows = np.asarray([float(np.min(day.lo)) for day in days])
    ranges = highs - lows
    true_ranges = np.full(len(days), np.nan)
    true_ranges[0] = ranges[0]
    for i in range(1, len(days)):
        true_ranges[i] = max(
            ranges[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    out: dict[date, dict[str, float | bool]] = {}
    for i in range(60, len(days)):
        prior_atr = float(np.mean(true_ranges[i - 14:i]))
        long_vol = float(np.median(true_ranges[i - 60:i]))
        prior_range = float(ranges[i - 1])
        prior_low = float(lows[i - 1])
        ibs = (
            (float(closes[i - 1]) - prior_low) / prior_range
            if prior_range > 0 else 0.5
        )
        out[days[i].day] = {
            "trend": float(closes[i - 1] - np.mean(closes[i - 20:i])),
            "gap": float(opens[i] - closes[i - 1]),
            "prev": float(closes[i - 1] - opens[i - 1]),
            "atr": prior_atr,
            "vol_high": prior_atr >= long_vol,
            "nr4": prior_range <= float(np.min(ranges[i - 4:i])),
            "compression": prior_range <= float(np.median(ranges[i - 20:i])),
            "ibs": ibs,
        }
    return out


def _matches(trade: L.Trade, regime: Regime, feature: dict[str, float | bool]) -> bool:
    side = trade.side
    for rule in regime.rules:
        if rule == "long" and side < 0:
            return False
        if rule == "short" and side > 0:
            return False
        if rule == "trend_align" and side * float(feature["trend"]) <= 0:
            return False
        if rule == "trend_counter" and side * float(feature["trend"]) >= 0:
            return False
        if rule == "gap_align" and side * float(feature["gap"]) <= 0:
            return False
        if rule == "gap_counter" and side * float(feature["gap"]) >= 0:
            return False
        if rule == "gap_small" and abs(float(feature["gap"])) > 0.10 * float(feature["atr"]):
            return False
        if rule == "prev_align" and side * float(feature["prev"]) <= 0:
            return False
        if rule == "prev_counter" and side * float(feature["prev"]) >= 0:
            return False
        if rule == "vol_low" and bool(feature["vol_high"]):
            return False
        if rule == "vol_high" and not bool(feature["vol_high"]):
            return False
        if rule == "nr4" and not bool(feature["nr4"]):
            return False
        if rule == "compression" and not bool(feature["compression"]):
            return False
        if rule == "ibs_align":
            if (side > 0 and float(feature["ibs"]) < 0.75) or (
                side < 0 and float(feature["ibs"]) > 0.25
            ):
                return False
        if rule == "ibs_counter":
            if (side > 0 and float(feature["ibs"]) > 0.25) or (
                side < 0 and float(feature["ibs"]) < 0.75
            ):
                return False
    return True


def filter_trades(
    trades: list[L.Trade],
    regime: Regime,
    features: dict[date, dict[str, float | bool]],
) -> list[L.Trade]:
    return [
        trade for trade in trades
        if trade.day in features and _matches(trade, regime, features[trade.day])
    ]


def _stats(trades: list[L.Trade], lo: date | None, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), 300.0))


def main() -> int:
    days = L.load_days("nq")
    features = _features(days)
    rows = []
    for base in BASES:
        raw = F.generate(days, base)
        for regime in SINGLE_REGIMES:
            trades = filter_trades(raw, regime, features)
            train = _stats(trades, None, L.TRAIN_END)
            valid = _stats(trades, date(2022, 1, 1), L.VALID_END)
            score = E.development_score(train, valid)
            rows.append((score, base, regime, trades, train, valid))

    rows.sort(key=lambda row: row[0], reverse=True)
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, base, regime, trades, train, valid in rows[:30]:
        test = _stats(trades, date(2024, 1, 1), None)
        print(f"\n{base.label} + {regime.name} score {score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
