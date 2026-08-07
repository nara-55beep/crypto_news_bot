"""
Sequential failed-weekly-auction research at five-minute resolution.

The prior completed week's RTH volume profile defines VAL, POC, and VAH.  A setup
requires three time-ordered facts on completed bars:

  1. price trades outside value and closes back inside;
  2. a later bar retests that boundary and again closes inside;
  3. entry occurs at the following one-minute open toward POC.

This ordering is explicit to avoid the same-bar retrospective error that invalidated
the old Turtle Soup model.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np

import lucid_causal_rebuild as L


@dataclass(frozen=True)
class AuctionConfig:
    market: str
    bin_ticks: int
    value_fraction: float
    tolerance_ticks: int
    max_bars: int
    stop_buffer_ticks: int
    target_mode: str
    rr: float = 2.0

    @property
    def label(self) -> str:
        return (
            f"{self.market}_weekly_auction_bin{self.bin_ticks}_va{self.value_fraction:g}_"
            f"tol{self.tolerance_ticks}_wait{self.max_bars}_buf{self.stop_buffer_ticks}_"
            f"{self.target_mode}{self.rr:g}"
        )


def _week_key(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return int(iso.year), int(iso.week)


def volume_profile(
    days: list[L.Day],
    tick: float,
    bin_ticks: int,
    value_fraction: float,
) -> tuple[float, float, float] | None:
    if not days:
        return None
    bin_size = tick * bin_ticks
    histogram: dict[int, float] = {}
    for day in days:
        typical = (day.hi + day.lo + day.cl) / 3.0
        bins = np.floor(typical / bin_size).astype(np.int64)
        weights = np.where(day.vol > 0, day.vol, 1.0)
        for price_bin, weight in zip(bins, weights):
            histogram[int(price_bin)] = histogram.get(int(price_bin), 0.0) + float(weight)
    if not histogram:
        return None
    keys = sorted(histogram)
    poc = max(keys, key=lambda key: histogram[key])
    selected = {poc}
    total = sum(histogram.values())
    accumulated = histogram[poc]
    lo = hi = poc
    while accumulated < value_fraction * total and (lo > keys[0] or hi < keys[-1]):
        below = histogram.get(lo - 1, -1.0)
        above = histogram.get(hi + 1, -1.0)
        if above >= below and hi < keys[-1]:
            hi += 1
            selected.add(hi)
            accumulated += max(0.0, histogram.get(hi, 0.0))
        elif lo > keys[0]:
            lo -= 1
            selected.add(lo)
            accumulated += max(0.0, histogram.get(lo, 0.0))
        else:
            break
    # Boundaries are bin edges; POC is represented by its bin center.
    val = lo * bin_size
    vah = (hi + 1) * bin_size
    poc_price = (poc + 0.5) * bin_size
    return val, poc_price, vah


def weekly_profiles(
    days: list[L.Day],
    cfg: AuctionConfig,
) -> dict[date, tuple[float, float, float]]:
    tick = L.MARKETS[cfg.market]["tick"]
    grouped: dict[tuple[int, int], list[L.Day]] = {}
    for day in days:
        grouped.setdefault(_week_key(day.day), []).append(day)
    keys = sorted(grouped)
    profile_by_week = {
        key: volume_profile(grouped[key], tick, cfg.bin_ticks, cfg.value_fraction)
        for key in keys
    }
    out = {}
    for previous, current in zip(keys, keys[1:]):
        profile = profile_by_week.get(previous)
        if profile is not None:
            for day in grouped[current]:
                out[day.day] = profile
    return out


def generate(
    days: list[L.Day],
    cfg: AuctionConfig,
    profiles: dict[date, tuple[float, float, float]] | None = None,
) -> list[L.Trade]:
    profiles = weekly_profiles(days, cfg) if profiles is None else profiles
    tick = L.MARKETS[cfg.market]["tick"]
    tolerance = cfg.tolerance_ticks * tick
    buffer = cfg.stop_buffer_ticks * tick
    out = []
    for day in days:
        profile = profiles.get(day.day)
        if profile is None:
            continue
        val, poc, vah = profile
        active_side = 0
        boundary = 0.0
        extreme = 0.0
        started_at = -1
        for ordinal, i in enumerate(L._sample_indices(day, 5, start=4)):
            block_lo = L._block_extreme(day.lo, i, 5, np.min)
            block_hi = L._block_extreme(day.hi, i, 5, np.max)
            close = float(day.cl[i])
            if active_side == 0:
                if block_lo < val and close > val:
                    active_side, boundary, extreme, started_at = 1, val, block_lo, ordinal
                    continue
                if block_hi > vah and close < vah:
                    active_side, boundary, extreme, started_at = -1, vah, block_hi, ordinal
                    continue
                continue

            if ordinal - started_at > cfg.max_bars:
                active_side = 0
                continue
            if active_side > 0:
                extreme = min(extreme, block_lo)
                if close < val:
                    active_side = 0
                    continue
                retest = block_lo <= boundary + tolerance and close > boundary
                stop = extreme - buffer
            else:
                extreme = max(extreme, block_hi)
                if close > vah:
                    active_side = 0
                    continue
                retest = block_hi >= boundary - tolerance and close < boundary
                stop = extreme + buffer
            if not retest:
                continue
            fixed_target = poc if cfg.target_mode == "poc" else None
            trade = L._make_trade(
                day,
                i,
                active_side,
                stop,
                rr=cfg.rr,
                strategy=cfg.label,
                fixed_target=fixed_target,
            )
            if trade is not None:
                out.append(trade)
            break
    return out


def configs(markets: list[str]) -> list[AuctionConfig]:
    # Keep this close to the pre-registered mechanism instead of optimizing dozens
    # of volume-profile definitions after seeing outcomes.
    return [
        AuctionConfig(market, bins, value, tolerance, wait, buffer, target, rr)
        for market in markets
        for bins in (8,)
        for value in (0.70,)
        for tolerance in (0, 2)
        for wait in (6, 12)
        for buffer in (2, 4)
        for target, rr in (("poc", 2.0), ("rr", 2.0))
    ]


def _stats(trades: list[L.Trade], risk: float, lo: date | None, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 25:
        return -1e9
    return min(train["pf"], valid["pf"]) * math.log1p(
        min(train["n"], valid["n"])
    ) + min(train["avg"], valid["avg"]) / 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["es", "nq"])
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    rows = []
    for market in args.markets:
        days = L.load_days(market)
        grid = configs([market])
        profile_cache = {}
        print(f"{market}: {len(grid)} auction configs", flush=True)
        for n, cfg in enumerate(grid, 1):
            profile_key = (cfg.bin_ticks, cfg.value_fraction)
            if profile_key not in profile_cache:
                profile_cache[profile_key] = weekly_profiles(days, cfg)
            trades = generate(days, cfg, profile_cache[profile_key])
            train = _stats(trades, args.risk, None, L.TRAIN_END)
            valid = _stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
            rows.append((_score(train, valid), cfg, trades, train, valid))
            if n % 100 == 0:
                print(f"  {n}/{len(grid)}", flush=True)
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = _stats(trades, args.risk, date(2024, 1, 1), None)
        print(f"\n{cfg.label} score {score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:4} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
