"""
Causal prior-day failed-break and retest research.

Unlike a one-candle "liquidity sweep", this requires:
  * a completed five-minute block to trade beyond the prior high/low and close inside;
  * a distinct later block to retest the reclaimed level and again close inside;
  * entry at the following one-minute open.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np

import lucid_causal_rebuild as L


@dataclass(frozen=True)
class RetestConfig:
    market: str
    tolerance_ticks: int
    max_bars: int
    cutoff: int
    stop_buffer_ticks: int
    target_mode: str
    rr: float

    @property
    def label(self) -> str:
        return (
            f"{self.market}_prior_retest_tol{self.tolerance_ticks}_wait{self.max_bars}_"
            f"cut{self.cutoff}_buf{self.stop_buffer_ticks}_{self.target_mode}{self.rr:g}"
        )


def generate(days: list[L.Day], cfg: RetestConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    tolerance = cfg.tolerance_ticks * tick
    buffer = cfg.stop_buffer_ticks * tick
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        prior_hi = float(np.max(prior.hi))
        prior_lo = float(np.min(prior.lo))
        prior_mid = (prior_hi + prior_lo) / 2.0
        active_side = 0
        boundary = 0.0
        extreme = 0.0
        started = -1
        for ordinal, i in enumerate(L._sample_indices(day, 5, start=4)):
            if day.minute[i] >= cfg.cutoff:
                break
            block_lo = L._block_extreme(day.lo, i, 5, np.min)
            block_hi = L._block_extreme(day.hi, i, 5, np.max)
            close = float(day.cl[i])
            if active_side == 0:
                if block_lo < prior_lo and close > prior_lo:
                    active_side, boundary, extreme, started = 1, prior_lo, block_lo, ordinal
                    continue
                if block_hi > prior_hi and close < prior_hi:
                    active_side, boundary, extreme, started = -1, prior_hi, block_hi, ordinal
                    continue
                continue
            if ordinal - started > cfg.max_bars:
                active_side = 0
                continue
            if active_side > 0:
                extreme = min(extreme, block_lo)
                if close < prior_lo:
                    active_side = 0
                    continue
                retest = block_lo <= boundary + tolerance and close > boundary
                stop = extreme - buffer
            else:
                extreme = max(extreme, block_hi)
                if close > prior_hi:
                    active_side = 0
                    continue
                retest = block_hi >= boundary - tolerance and close < boundary
                stop = extreme + buffer
            if not retest:
                continue
            if cfg.target_mode == "prior_mid":
                target = prior_mid
            elif cfg.target_mode == "open":
                target = float(day.op[0])
            elif cfg.target_mode == "vwap":
                target = float(day.vwap[i])
            else:
                target = None
            trade = L._make_trade(
                day,
                i,
                active_side,
                stop,
                rr=cfg.rr,
                strategy=cfg.label,
                fixed_target=target,
            )
            if trade is not None:
                out.append(trade)
            break
    return out


def configs(markets: list[str]) -> list[RetestConfig]:
    return [
        RetestConfig(market, tolerance, wait, cutoff, buffer, target, rr)
        for market in markets
        for tolerance in (0, 2)
        for wait in (6, 12)
        for cutoff in (240, 330)
        for buffer in (2,)
        for target, rr in (
            ("prior_mid", 2.0),
            ("vwap", 2.0),
            ("rr", 2.0),
        )
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
    ap.add_argument("--markets", nargs="+", default=["es", "nq", "cl"])
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    rows = []
    for market in args.markets:
        days = L.load_days(market)
        grid = configs([market])
        print(f"{market}: {len(grid)} retest configs", flush=True)
        for n, cfg in enumerate(grid, 1):
            trades = generate(days, cfg)
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
