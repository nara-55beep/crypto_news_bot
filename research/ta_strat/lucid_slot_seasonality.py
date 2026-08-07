"""
Walk-forward half-hour clock-seasonality research.

For each of the 13 regular-session half-hour slots, the direction and volatility
estimate use only that same slot from prior sessions.  The order is known before the
slot starts, enters its one-minute open with adverse slippage, and exits at the next
slot's open (or 15:59 for the final slot).  This tests daily-period return continuation
without reading any part of the slot being traded.
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
class SlotConfig:
    market: str
    lookback: int
    t_threshold: float
    direction: str
    stop_mult: float

    @property
    def label(self) -> str:
        return (
            f"{self.market}_slot_lb{self.lookback}_t{self.t_threshold:g}_"
            f"{self.direction}_s{self.stop_mult:g}"
        )


def _idx(day: L.Day, minute: int) -> int | None:
    found = np.flatnonzero(day.minute == minute)
    return int(found[0]) if len(found) else None


def _historical_slot_move(day: L.Day, start: int) -> float | None:
    si = _idx(day, start)
    if si is None:
        return None
    if start < 360:
        ei = _idx(day, start + 30)
        return None if ei is None else float(day.op[ei] - day.op[si])
    ei = _idx(day, 389)
    return None if ei is None else float(day.cl[ei] - day.op[si])


def _slot_trade(
    day: L.Day,
    start: int,
    side: int,
    stop_points: float,
    strategy: str,
) -> L.Trade | None:
    si = _idx(day, start)
    if si is None:
        return None
    tick = L.MARKETS[day.market]["tick"]
    pv = L.MARKETS[day.market]["pv"]
    entry = float(day.op[si]) + side * tick
    stop_points = max(tick, float(stop_points))
    stop = entry - side * stop_points
    if start < 360:
        exit_i = _idx(day, start + 30)
        if exit_i is None:
            return None
        end_scan_minute = start + 29
        raw_exit = float(day.op[exit_i])
    else:
        exit_i = _idx(day, 389)
        if exit_i is None:
            return None
        end_scan_minute = 389
        raw_exit = float(day.cl[exit_i])
    reason = "time"
    actual_exit_i = exit_i
    for j in np.flatnonzero(
        (day.minute >= start) & (day.minute <= end_scan_minute)
    ):
        stopped = day.lo[j] <= stop if side > 0 else day.hi[j] >= stop
        if not stopped:
            continue
        raw_exit = min(stop, float(day.op[j])) if side > 0 else max(stop, float(day.op[j]))
        actual_exit_i = int(j)
        reason = "stop"
        break
    exit_px = raw_exit - side * tick
    return L.Trade(
        market=day.market,
        strategy=strategy,
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[si]),
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


def slot_history(days: list[L.Day]) -> dict[int, list[float | None]]:
    slots = tuple(range(0, 361, 30))
    return {
        start: [_historical_slot_move(day, start) for day in days]
        for start in slots
    }


def generate(
    days: list[L.Day],
    cfg: SlotConfig,
    historical: dict[int, list[float | None]] | None = None,
) -> list[L.Trade]:
    out = []
    slots = tuple(range(0, 361, 30))
    historical = slot_history(days) if historical is None else historical
    for i in range(cfg.lookback, len(days)):
        for start in slots:
            values = np.asarray(
                historical[start][i - cfg.lookback:i],
                dtype=float,
            )
            values = values[np.isfinite(values)]
            if len(values) < max(15, cfg.lookback // 2):
                continue
            mean = float(np.mean(values))
            sigma = float(np.std(values, ddof=1))
            if sigma <= 1e-12:
                continue
            t_stat = mean / (sigma / math.sqrt(len(values)))
            if abs(t_stat) < cfg.t_threshold or mean == 0:
                continue
            side = 1 if mean > 0 else -1
            if cfg.direction == "reverse":
                side *= -1
            trade = _slot_trade(
                days[i],
                start,
                side,
                cfg.stop_mult * sigma,
                cfg.label,
            )
            if trade is not None:
                out.append(trade)
    return out


def configs(markets: list[str]) -> list[SlotConfig]:
    return [
        SlotConfig(market, lookback, threshold, direction, stop_mult)
        for market in markets
        for lookback in (20, 40, 60)
        for threshold in (0.0, 0.5, 1.0, 1.5)
        for direction in ("continue", "reverse")
        for stop_mult in (1.0, 2.0, 3.0)
    ]


def _stats(trades: list[L.Trade], risk: float, lo: date | None, hi: date | None) -> dict:
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
        historical = slot_history(days)
        grid = configs([market])
        print(f"{market}: {len(grid)} slot configs", flush=True)
        for cfg in grid:
            trades = generate(days, cfg, historical)
            train = _stats(trades, args.risk, None, L.TRAIN_END)
            valid = _stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
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
