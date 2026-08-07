"""
Strictly causal daily time-series continuation/reversal research.

Direction is formed from one, three, or five fully completed prior RTH sessions.
An optional filter requires today's known 09:30 gap to agree with that direction.
Entry is delayed to the 09:31 open and all positions are flat at 15:59.  The stop
distance is based on the median range of the preceding 20 completed sessions.
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
class DailyConfig:
    market: str
    lookback: int
    direction: str
    gap_filter: str
    threshold: float
    stop_mult: float
    target_rr: float | None

    @property
    def label(self) -> str:
        target = "time" if self.target_rr is None else f"rr{self.target_rr:g}"
        return (
            f"{self.market}_daily_lb{self.lookback}_{self.direction}_"
            f"gap{self.gap_filter}_th{self.threshold:g}_"
            f"s{self.stop_mult:g}_{target}"
        )


def _idx(day: L.Day, minute: int) -> int | None:
    found = np.flatnonzero(day.minute == minute)
    return int(found[0]) if len(found) else None


def _trade(
    day: L.Day,
    side: int,
    stop_points: float,
    target_rr: float | None,
    strategy: str,
) -> L.Trade | None:
    entry_i, exit_i = _idx(day, 1), _idx(day, 389)
    if entry_i is None or exit_i is None:
        return None
    tick = L.MARKETS[day.market]["tick"]
    pv = L.MARKETS[day.market]["pv"]
    stop_points = max(tick, float(stop_points))
    entry = float(day.op[entry_i]) + side * tick
    stop = entry - side * stop_points
    target = (
        None if target_rr is None
        else entry + side * target_rr * stop_points
    )
    raw_exit = float(day.cl[exit_i])
    actual_exit_i = exit_i
    reason = "time"
    for j in range(entry_i, exit_i + 1):
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
            actual_exit_i, reason = j, "stop"
            break
        if targeted:
            raw_exit = float(target)
            actual_exit_i, reason = j, "target"
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


def generate(days: list[L.Day], cfg: DailyConfig) -> list[L.Trade]:
    out = []
    warmup = max(20, cfg.lookback)
    for i in range(warmup, len(days)):
        day = days[i]
        start = days[i - cfg.lookback]
        end = days[i - 1]
        past_return = float(end.cl[-1] / start.op[0] - 1.0)
        if past_return == 0 or abs(past_return) < cfg.threshold:
            continue
        side = 1 if past_return > 0 else -1
        if cfg.direction == "reverse":
            side *= -1
        gap = float(day.op[0] - end.cl[-1])
        if cfg.gap_filter == "agree" and gap * side <= 0:
            continue
        ranges = np.asarray([
            np.max(prior.hi) - np.min(prior.lo)
            for prior in days[i - 20:i]
        ])
        scale = float(np.median(ranges))
        if not math.isfinite(scale) or scale <= 0:
            continue
        trade = _trade(
            day,
            side,
            cfg.stop_mult * scale,
            cfg.target_rr,
            cfg.label,
        )
        if trade is not None:
            out.append(trade)
    return out


def configs(markets: list[str]) -> list[DailyConfig]:
    return [
        DailyConfig(
            market, lookback, direction, gap_filter,
            threshold, stop_mult, target,
        )
        for market in markets
        for lookback in (1, 3, 5)
        for direction in ("continue", "reverse")
        for gap_filter in ("none", "agree")
        for threshold in (0.0, 0.002)
        for stop_mult in (0.25, 0.50)
        for target in (None, 2.0)
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
        print(f"{market}: {len(grid)} daily configs", flush=True)
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
