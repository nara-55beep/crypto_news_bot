"""
Causal overnight-return forecast research using RTH-only history.

The opening gap from the prior regular-session close to today's 09:30 open is known
before either trade:

  * early: enter at 09:31 and exit at the 10:00 open;
  * late:  enter at 15:30 and exit at 15:59.

The published hypothesis is that the gap forecasts the first half hour negatively
and the last half hour positively.  Stops are scaled only by the completed prior
session's range.  Every market order pays one adverse tick and every stop is filled
at the worse of its level or the current minute's open.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L


@dataclass(frozen=True)
class OvernightConfig:
    market: str
    segment: str
    gap_threshold: float
    direction: str
    stop_mult: float
    target_rr: float | None

    @property
    def label(self) -> str:
        target = "time" if self.target_rr is None else f"rr{self.target_rr:g}"
        return (
            f"{self.market}_overnight_{self.segment}_th{self.gap_threshold:g}_"
            f"{self.direction}_s{self.stop_mult:g}_{target}"
        )


def _idx(day: L.Day, minute: int) -> int | None:
    found = np.flatnonzero(day.minute == minute)
    return int(found[0]) if len(found) else None


def _trade(
    day: L.Day,
    entry_minute: int,
    exit_minute: int,
    side: int,
    stop_points: float,
    target_rr: float | None,
    strategy: str,
) -> L.Trade | None:
    entry_i, exit_i = _idx(day, entry_minute), _idx(day, exit_minute)
    if entry_i is None or exit_i is None or exit_i <= entry_i:
        return None
    tick = L.MARKETS[day.market]["tick"]
    pv = L.MARKETS[day.market]["pv"]
    stop_points = max(float(stop_points), tick)
    entry = float(day.op[entry_i]) + side * tick
    stop = entry - side * stop_points
    target = (
        None if target_rr is None
        else entry + side * target_rr * stop_points
    )
    if exit_minute < 389:
        raw_exit = float(day.op[exit_i])
        scan_end = exit_i - 1
    else:
        raw_exit = float(day.cl[exit_i])
        scan_end = exit_i
    actual_exit_i = exit_i
    reason = "time"
    for j in range(entry_i, scan_end + 1):
        stopped = day.lo[j] <= stop if side > 0 else day.hi[j] >= stop
        targeted = (
            False if target is None
            else day.hi[j] >= target if side > 0
            else day.lo[j] <= target
        )
        if stopped:
            raw_exit = (
                min(stop, float(day.op[j]))
                if side > 0 else max(stop, float(day.op[j]))
            )
            actual_exit_i = j
            reason = "stop"
            break
        if targeted:
            raw_exit = float(target)
            actual_exit_i = j
            reason = "target"
            break
    exit_px = raw_exit - side * tick
    return L.Trade(
        market=day.market,
        strategy=strategy,
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[entry_i]),
        exit_ts=pd.Timestamp(day.ts[actual_exit_i]),
        side=side,
        entry=entry,
        stop=stop,
        target=math.nan if target is None else float(target),
        exit=exit_px,
        reason=reason,
        risk_per_micro=(stop_points + tick) * pv,
        gross_per_micro=side * (exit_px - entry) * pv,
    )


def generate(days: list[L.Day], cfg: OvernightConfig) -> list[L.Trade]:
    out = []
    for i in range(1, len(days)):
        prior, day = days[i - 1], days[i]
        prior_close = float(prior.cl[-1])
        if prior_close <= 0:
            continue
        gap = float(day.op[0]) / prior_close - 1.0
        if abs(gap) < cfg.gap_threshold or gap == 0:
            continue
        side = 1 if gap > 0 else -1
        if cfg.direction == "reverse":
            side *= -1
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        if prior_range <= 0:
            continue
        if cfg.segment == "early":
            entry_minute, exit_minute = 1, 30
        else:
            entry_minute, exit_minute = 360, 389
        trade = _trade(
            day,
            entry_minute,
            exit_minute,
            side,
            cfg.stop_mult * prior_range,
            cfg.target_rr,
            cfg.label,
        )
        if trade is not None:
            out.append(trade)
    return out


def configs(markets: list[str]) -> list[OvernightConfig]:
    return [
        OvernightConfig(
            market,
            segment,
            threshold,
            "reverse" if segment == "early" else "continue",
            stop_mult,
            target,
        )
        for market in markets
        for segment in ("early", "late")
        for threshold in (0.0, 0.0005, 0.001, 0.002)
        for stop_mult in (0.10, 0.20, 0.30)
        for target in (None, 1.5, 2.0)
    ]


def _stats(
    trades: list[L.Trade],
    risk: float,
    lo: date | None,
    hi: date | None,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 100:
        return -1e9
    return min(train["pf"], valid["pf"]) * math.log1p(
        min(train["n"], valid["n"])
    ) + min(train["avg"], valid["avg"]) / 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["es", "nq", "cl"])
    ap.add_argument("--risk", type=float, default=100.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    rows = []
    for market in args.markets:
        days = L.load_days(market)
        grid = configs([market])
        print(f"{market}: {len(grid)} overnight configs", flush=True)
        for cfg in grid:
            trades = generate(days, cfg)
            train = _stats(trades, args.risk, None, L.TRAIN_END)
            valid = _stats(
                trades, args.risk, date(2022, 1, 1), L.VALID_END
            )
            rows.append((_score(train, valid), cfg, trades, train, valid))
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = _stats(trades, args.risk, date(2024, 1, 1), None)
        print(f"\n{cfg.label} score {score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
