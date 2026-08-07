"""
Bar-close/next-open execution audit for development-selected gap signals.

The original causal engine conservatively resolves one-minute stop/target ambiguity,
but live and historical paths can still differ because a resting intrabar order is
not reproducible from OHLC alone.  This audit makes profit exits deterministic: a
completed one-minute CLOSE must breach the stored target, and liquidation occurs at
the following one-minute open with adverse slippage.  Evaluation-safe variants
retain exactly one resting protective stop.  Because no profit order is resting
intrabar, there is no favorable high/low ordering assumption when that stop trades.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_gap_research as G


def convert(
    day: L.Day,
    trade: L.Trade,
    *,
    protective_stop: bool = False,
) -> L.Trade | None:
    # ``load_days`` retains timezone-aware pandas Timestamps in an object array.
    # Comparing Timestamp-to-Timestamp avoids a costly full-array conversion on
    # every trade and is also unit-safe across pandas versions.
    matches = np.flatnonzero(day.ts == pd.Timestamp(trade.entry_ts))
    if not len(matches):
        return None
    entry_i = int(matches[0])
    side = trade.side
    tick = L.MARKETS[trade.market]["tick"]
    pv = L.MARKETS[trade.market]["pv"]
    target_exists = math.isfinite(trade.target)
    raw_exit = float(day.cl[-1])
    exit_i = len(day.cl) - 1
    reason = "eod"
    for j in range(entry_i, len(day.cl)):
        if protective_stop:
            stopped = day.lo[j] <= trade.stop if side > 0 else day.hi[j] >= trade.stop
        else:
            stopped = day.cl[j] <= trade.stop if side > 0 else day.cl[j] >= trade.stop
        if stopped:
            if protective_stop:
                raw_exit = (
                    min(trade.stop, float(day.op[j]))
                    if side > 0 else max(trade.stop, float(day.op[j]))
                )
                exit_i = j
                reason = "stop_protective"
            elif j < len(day.cl) - 1:
                exit_i = j + 1
                raw_exit = float(day.op[exit_i])
                reason = "stop_close"
            break
        if j >= len(day.cl) - 1:
            continue
        targeted = (
            target_exists
            and (day.cl[j] >= trade.target if side > 0 else day.cl[j] <= trade.target)
        )
        if not targeted:
            continue
        exit_i = j + 1
        raw_exit = float(day.op[exit_i])
        reason = "target_close"
        break
    exit_px = raw_exit - side * tick
    return replace(
        trade,
        strategy="barclose_" + trade.strategy,
        exit_ts=pd.Timestamp(day.ts[exit_i]),
        exit=exit_px,
        reason=reason,
        gross_per_micro=side * (exit_px - trade.entry) * pv,
    )


def convert_all(
    days: list[L.Day],
    trades: list[L.Trade],
    *,
    protective_stop: bool = False,
) -> list[L.Trade]:
    by_day = {day.day: day for day in days}
    out = []
    for trade in trades:
        day = by_day.get(trade.day)
        converted = (
            None if day is None
            else convert(day, trade, protective_stop=protective_stop)
        )
        if converted is not None:
            out.append(converted)
    return out


def _stats(
    trades: list[L.Trade],
    lo: date | None,
    hi: date | None,
    risk: float = 300.0,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def main() -> int:
    configs = [
        G.GapConfig(
            "nq", "opening_gap", 30, 0.002,
            "reverse", "turn", "atr", "rr", 2.0,
        ),
        G.GapConfig(
            "es", "opening_gap", 30, 0.002,
            "reverse", "turn", "extreme", "rr", 2.0,
        ),
    ]
    all_trades = []
    for cfg in configs:
        days = L.load_days(cfg.market)
        original = G.generate(days, cfg)
        converted = convert_all(days, original, protective_stop=True)
        all_trades.extend(converted)
        print(f"\n{cfg.market.upper()} {cfg.label}")
        for name, lo, hi in (
            ("train", None, L.TRAIN_END),
            ("valid", date(2022, 1, 1), L.VALID_END),
            ("TEST", date(2024, 1, 1), None),
        ):
            result = _stats(converted, lo, hi)
            print(
                f"  {name:<5} n{result['n']:4} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    print("\nCOMBINED")
    for name, lo, hi in (
        ("train", None, L.TRAIN_END),
        ("valid", date(2022, 1, 1), L.VALID_END),
        ("TEST", date(2024, 1, 1), None),
    ):
        result = _stats(all_trades, lo, hi)
        print(
            f"  {name:<5} n{result['n']:4} PF{result['pf']:.2f} "
            f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
            f"DD{result['maxdd']:8.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
