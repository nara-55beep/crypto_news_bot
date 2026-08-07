"""
Causal 30-minute moving-average trend/reversal research.

Only completed 30-minute RTH closes enter each moving average.  A changed regime is
executed at the following one-minute open, positions are never carried overnight,
and the initial stop is scaled by the standard deviation of the preceding 20
completed 30-minute moves.  Stop fills are gap-worse and receive one additional
adverse tick.
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
class MAConfig:
    market: str
    fast: int
    slow: int
    direction: str
    stop_mult: float

    @property
    def label(self) -> str:
        return (
            f"{self.market}_ma30_f{self.fast}_s{self.slow}_"
            f"{self.direction}_stop{self.stop_mult:g}"
        )


def _idx(day: L.Day, minute: int) -> int | None:
    found = np.flatnonzero(day.minute == minute)
    return int(found[0]) if len(found) else None


def _bar_stream(
    days: list[L.Day],
) -> tuple[list[float], dict[date, list[tuple[int, int]]]]:
    closes: list[float] = []
    lookup: dict[date, list[tuple[int, int]]] = {}
    for day in days:
        rows = []
        for signal_minute in range(29, 360, 30):
            i = _idx(day, signal_minute)
            if i is None:
                continue
            closes.append(float(day.cl[i]))
            rows.append((signal_minute, len(closes) - 1))
        lookup[day.day] = rows
    return closes, lookup


def _make_regime_trade(
    day: L.Day,
    entry_minute: int,
    planned_exit_minute: int | None,
    side: int,
    stop_points: float,
    strategy: str,
) -> L.Trade | None:
    entry_i = _idx(day, entry_minute)
    if entry_i is None:
        return None
    if planned_exit_minute is None:
        exit_i = _idx(day, 389)
        use_open = False
    else:
        exit_i = _idx(day, planned_exit_minute)
        use_open = True
    if exit_i is None or exit_i <= entry_i:
        return None
    tick = L.MARKETS[day.market]["tick"]
    pv = L.MARKETS[day.market]["pv"]
    stop_points = max(tick, float(stop_points))
    entry = float(day.op[entry_i]) + side * tick
    stop = entry - side * stop_points
    raw_exit = float(day.op[exit_i] if use_open else day.cl[exit_i])
    actual_exit_i = exit_i
    reason = "reverse" if use_open else "eod"
    scan_end = exit_i - 1 if use_open else exit_i
    for j in range(entry_i, scan_end + 1):
        stopped = day.lo[j] <= stop if side > 0 else day.hi[j] >= stop
        if not stopped:
            continue
        raw_exit = (
            min(stop, float(day.op[j]))
            if side > 0 else max(stop, float(day.op[j]))
        )
        actual_exit_i = j
        reason = "stop"
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
        target=math.nan,
        exit=exit_px,
        reason=reason,
        risk_per_micro=(stop_points + tick) * pv,
        gross_per_micro=side * (exit_px - entry) * pv,
    )


def generate(
    days: list[L.Day],
    cfg: MAConfig,
    stream: tuple[list[float], dict[date, list[tuple[int, int]]]] | None = None,
) -> list[L.Trade]:
    closes_list, lookup = _bar_stream(days) if stream is None else stream
    closes = np.asarray(closes_list, dtype=float)
    out = []
    for day in days:
        regimes: list[tuple[int, int, float]] = []
        previous_side = 0
        for signal_minute, pos in lookup[day.day]:
            if pos < max(cfg.slow, 21):
                continue
            fast = float(np.mean(closes[pos - cfg.fast + 1:pos + 1]))
            slow = float(np.mean(closes[pos - cfg.slow + 1:pos + 1]))
            side = 1 if fast > slow else -1 if fast < slow else 0
            if cfg.direction == "reverse":
                side *= -1
            if side == 0 or side == previous_side:
                continue
            moves = np.diff(closes[pos - 20:pos + 1])
            sigma = float(np.std(moves, ddof=1))
            if not math.isfinite(sigma) or sigma <= 0:
                continue
            regimes.append((signal_minute + 1, side, cfg.stop_mult * sigma))
            previous_side = side
        for r, (entry_minute, side, stop_points) in enumerate(regimes):
            planned_exit = regimes[r + 1][0] if r + 1 < len(regimes) else None
            trade = _make_regime_trade(
                day,
                entry_minute,
                planned_exit,
                side,
                stop_points,
                cfg.label,
            )
            if trade is not None:
                out.append(trade)
    return out


def configs(markets: list[str]) -> list[MAConfig]:
    return [
        MAConfig(market, fast, slow, direction, stop_mult)
        for market in markets
        for fast, slow in ((1, 4), (2, 8), (3, 12))
        for direction in ("continue", "reverse")
        for stop_mult in (1.5, 2.5, 4.0)
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
        stream = _bar_stream(days)
        grid = configs([market])
        print(f"{market}: {len(grid)} MA configs", flush=True)
        for cfg in grid:
            trades = generate(days, cfg, stream)
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
