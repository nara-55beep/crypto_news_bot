"""
Dynamic-exit implementation of the published noise-area momentum strategy.

The earlier local adaptation froze the boundary/VWAP stop when a segment opened.
The paper instead recomputes the current noise boundary and RTH VWAP at each
half-hour checkpoint.  This implementation makes every decision on the completed
checkpoint minute and executes at the following minute's open.

A fixed hard stop based only on the prior 20 completed RTH ranges is added between
checkpoints.  This is necessary for bounded Lucid risk and is deliberately kept
separate from the paper's dynamic close-confirmed exit.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_noise_area_search as N


_RANGE_SCALE_CACHE: dict[int, np.ndarray] = {}


def _range_scales(days: list[L.Day]) -> np.ndarray:
    cached = _RANGE_SCALE_CACHE.get(id(days))
    if cached is not None and len(cached) == len(days):
        return cached
    ranges = np.asarray(
        [float(np.max(day.hi) - np.min(day.lo)) for day in days],
        dtype=float,
    )
    scales = np.full(len(days), np.nan)
    for i in range(20, len(days)):
        scales[i] = float(np.median(ranges[i - 20:i]))
    _RANGE_SCALE_CACHE[id(days)] = scales
    return scales


@dataclass(frozen=True)
class TrailConfig:
    market: str
    lookback: int
    multiplier: float
    entry_vwap: bool
    exit_mode: str
    hard_stop_mult: float
    pattern: str = "none"

    @property
    def label(self) -> str:
        return (
            f"{self.market}_noise_dynamic_lb{self.lookback}_"
            f"k{self.multiplier:g}_ev{int(self.entry_vwap)}_"
            f"x{self.exit_mode}_hs{self.hard_stop_mult:g}_{self.pattern}"
        )


def configs(market: str, patterns: tuple[str, ...] = ("none",)) -> list[TrailConfig]:
    return [
        TrailConfig(
            market,
            lookback,
            multiplier,
            entry_vwap,
            exit_mode,
            hard_stop,
            pattern,
        )
        for lookback in (14, 60, 90)
        for multiplier in (0.75, 1.0, 1.25)
        for entry_vwap in (False, True)
        for exit_mode in ("opposite", "current", "vwap", "vwap_current")
        for hard_stop in (0.25, 0.50, 1.0)
        for pattern in patterns
    ]


def _daily_pattern(days: list[L.Day], i: int, pattern: str) -> bool:
    if pattern == "none":
        return True
    prior = days[i - 1]
    ph = float(np.max(prior.hi))
    pl = float(np.min(prior.lo))
    po = float(prior.op[0])
    pc = float(prior.cl[-1])
    pr = ph - pl
    if pr <= 0:
        return False
    if pattern == "nr4":
        if i < 4:
            return False
        ranges = [
            float(np.max(day.hi) - np.min(day.lo))
            for day in days[i - 4:i]
        ]
        return ranges[-1] <= min(ranges)
    if pattern == "triangle":
        if i < 3:
            return False
        return (
            ph < max(float(np.max(x.hi)) for x in days[i - 3:i - 1])
            and pl > min(float(np.min(x.lo)) for x in days[i - 3:i - 1])
        )
    if pattern == "strong_close":
        return (pc - pl) / pr >= 0.90 or (pc - pl) / pr <= 0.10
    if pattern == "big_tail":
        location_open = (po - pl) / pr
        location_close = (pc - pl) / pr
        return (
            (location_open >= 0.75 and location_close >= 0.75)
            or (location_open <= 0.25 and location_close <= 0.25)
        )
    return True


def _append_trade(
    out: list[L.Trade],
    day: L.Day,
    cfg: TrailConfig,
    side: int,
    entry_i: int,
    entry: float,
    stop: float,
    exit_i: int,
    raw_exit: float,
    reason: str,
) -> None:
    tick = L.MARKETS[cfg.market]["tick"]
    pv = L.MARKETS[cfg.market]["pv"]
    exit_px = float(raw_exit) - side * tick
    out.append(
        L.Trade(
            market=cfg.market,
            strategy=cfg.label,
            day=day.day,
            entry_ts=pd.Timestamp(day.ts[entry_i]),
            exit_ts=pd.Timestamp(day.ts[exit_i]),
            side=side,
            entry=entry,
            stop=stop,
            target=math.nan,
            exit=exit_px,
            reason=reason,
            risk_per_micro=(abs(entry - stop) + tick) * pv,
            gross_per_micro=side * (exit_px - entry) * pv,
        )
    )


def _desired(
    day: L.Day,
    i: int,
    upper: float,
    lower: float,
    entry_vwap: bool,
) -> int:
    close = float(day.cl[i])
    if close > upper and (not entry_vwap or close > float(day.vwap[i])):
        return 1
    if close < lower and (not entry_vwap or close < float(day.vwap[i])):
        return -1
    return 0


def _keep(
    day: L.Day,
    i: int,
    side: int,
    desired: int,
    upper: float,
    lower: float,
    mode: str,
) -> bool:
    close = float(day.cl[i])
    if mode == "opposite":
        return desired != -side
    if mode == "current":
        return close > upper if side > 0 else close < lower
    if mode == "vwap":
        return close > float(day.vwap[i]) if side > 0 else close < float(day.vwap[i])
    threshold = (
        max(upper, float(day.vwap[i]))
        if side > 0
        else min(lower, float(day.vwap[i]))
    )
    return close > threshold if side > 0 else close < threshold


def generate(days: list[L.Day], cfg: TrailConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    range_scales = _range_scales(days)
    for n in range(max(cfg.lookback, 20), len(days)):
        if not _daily_pattern(days, n, cfg.pattern):
            continue
        day = days[n]
        prior_close = float(days[n - 1].cl[-1])
        hard_distance = cfg.hard_stop_mult * float(range_scales[n])
        if not math.isfinite(hard_distance) or hard_distance < tick:
            continue
        checkpoints = [
            int(i)
            for i in np.flatnonzero(
                ((day.minute + 1) % 30 == 0) & (day.minute < 360)
            )
        ]
        final_matches = np.flatnonzero(day.minute >= 389)
        if not checkpoints or not len(final_matches):
            continue
        final_i = int(final_matches[0])

        side = 0
        entry_i = -1
        entry = stop = 0.0
        scan_start = 0
        for i in checkpoints:
            if side:
                interval = (
                    day.lo[scan_start:i + 1] <= stop
                    if side > 0
                    else day.hi[scan_start:i + 1] >= stop
                )
                hits = np.flatnonzero(interval)
                if len(hits):
                    stop_i = scan_start + int(hits[0])
                    raw = (
                        min(stop, float(day.op[stop_i]))
                        if side > 0
                        else max(stop, float(day.op[stop_i]))
                    )
                    _append_trade(
                        out, day, cfg, side, entry_i, entry, stop,
                        stop_i, raw, "hard_stop",
                    )
                    side = 0
            minute = int(day.minute[i])
            if i + 1 >= final_i:
                continue
            sigma = N._sigma(days, n, minute, cfg.lookback)
            if sigma is None:
                continue
            upper = max(float(day.op[0]), prior_close) * (
                1.0 + cfg.multiplier * sigma
            )
            lower = min(float(day.op[0]), prior_close) * (
                1.0 - cfg.multiplier * sigma
            )
            wanted = _desired(
                day, i, upper, lower, cfg.entry_vwap
            )
            if side and not _keep(
                day, i, side, wanted, upper, lower, cfg.exit_mode
            ):
                _append_trade(
                    out, day, cfg, side, entry_i, entry, stop,
                    i + 1, float(day.op[i + 1]), "dynamic_exit",
                )
                side = 0
            if side == 0 and wanted:
                side = wanted
                entry_i = i + 1
                entry = float(day.op[entry_i]) + side * tick
                stop = entry - side * hard_distance
            scan_start = i + 1

        if side:
            # The final minute is an opening-price flatten. Its later high/low
            # cannot trigger a stop after the position has already been closed.
            interval = (
                day.lo[scan_start:final_i] <= stop
                if side > 0
                else day.hi[scan_start:final_i] >= stop
            )
            hits = np.flatnonzero(interval)
            if len(hits):
                stop_i = scan_start + int(hits[0])
                raw = (
                    min(stop, float(day.op[stop_i]))
                    if side > 0
                    else max(stop, float(day.op[stop_i]))
                )
                _append_trade(
                    out, day, cfg, side, entry_i, entry, stop,
                    stop_i, raw, "hard_stop",
                )
            else:
                _append_trade(
                    out, day, cfg, side, entry_i, entry, stop,
                    final_i, float(day.op[final_i]), "eod",
                )
    return sorted(out, key=lambda trade: (trade.entry_ts, trade.exit_ts))


def _stats(
    trades: list[L.Trade],
    risk: float,
    lo: date | None,
    hi: date | None,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=("es", "nq"), default="es")
    ap.add_argument("--risk", type=float, default=100.0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument(
        "--patterns",
        nargs="+",
        choices=("none", "nr4", "triangle", "strong_close", "big_tail"),
        default=("none",),
    )
    args = ap.parse_args()
    days = L.load_days(args.market)
    rows = []
    grid = configs(args.market, tuple(args.patterns))
    print(f"{args.market}: {len(grid)} dynamic-noise configurations", flush=True)
    for number, cfg in enumerate(grid, 1):
        trades = generate(days, cfg)
        train = _stats(trades, args.risk, None, L.TRAIN_END)
        valid = _stats(
            trades, args.risk, date(2022, 1, 1), L.VALID_END
        )
        score = E.development_score(train, valid)
        rows.append((score, cfg, trades, train, valid))
        if number % 50 == 0:
            print(f"  completed {number}/{len(grid)}", flush=True)
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = _stats(trades, args.risk, date(2024, 1, 1), None)
        print(f"\n{cfg.label} score {score:.3f}")
        for label, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {label:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
