"""
Causal intraday jump continuation/reversal research.

A jump exists only after a completed return block exceeds a volatility-scaled
threshold.  The strategy enters the following one-minute open and takes at most one
trade per market/session.  This directly tests the documented post-jump continuation
channel without using the triggering candle's high/low as a resting fill price.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np

import lucid_causal_rebuild as L
import lucid_predictive_research as P


@dataclass(frozen=True)
class JumpConfig:
    market: str
    tf: int
    k: float
    cutoff: int
    direction: str
    stop_mode: str
    rr: float | None
    horizon: int

    @property
    def label(self) -> str:
        target = "time" if self.rr is None else f"r{self.rr:g}"
        return (
            f"{self.market}_jump_tf{self.tf}_k{self.k:g}_cut{self.cutoff}_"
            f"{self.direction}_{self.stop_mode}_{target}_h{self.horizon}"
        )


def generate(days: list[L.Day], cfg: JumpConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    for day in days:
        for i in L._sample_indices(day, cfg.tf, start=30):
            if day.minute[i] >= cfg.cutoff:
                break
            before = i - cfg.tf
            if before < 0:
                continue
            move = float(day.cl[i] - day.cl[before])
            threshold = cfg.k * float(day.atr[i]) * math.sqrt(cfg.tf)
            if abs(move) < threshold or move == 0:
                continue
            side = 1 if move > 0 else -1
            if cfg.direction == "reverse":
                side *= -1
            fi = i + 1
            if fi >= len(day.op):
                break
            entry = float(day.op[fi]) + side * tick
            if cfg.stop_mode == "block":
                extreme = (
                    float(np.min(day.lo[max(0, i - cfg.tf + 1):i + 1])) - tick
                    if side > 0 else
                    float(np.max(day.hi[max(0, i - cfg.tf + 1):i + 1])) + tick
                )
                stop_points = side * (entry - extreme)
                if stop_points <= 0:
                    continue
            else:
                stop_points = max(tick, float(day.atr[i]) * math.sqrt(cfg.tf))
            end_minute = min(389, int(day.minute[i]) + cfg.horizon)
            trade = P._timed_trade(
                day,
                i,
                side,
                stop_points,
                cfg.label,
                end_minute=end_minute,
                target_rr=cfg.rr,
            )
            if trade is not None:
                out.append(trade)
            break
    return out


def configs(markets: list[str]) -> list[JumpConfig]:
    return [
        JumpConfig(market, tf, k, cutoff, direction, stop, rr, horizon)
        for market in markets
        for tf in (3, 5, 15)
        for k in (1.5, 2.0, 3.0)
        for cutoff in (120, 240, 330)
        for direction in ("continue", "reverse")
        for stop in ("block", "atr")
        for rr in (None, 1.5, 2.0)
        for horizon in (60, 120)
    ]


def _stats(trades: list[L.Trade], risk: float, lo: date | None, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 30:
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
        print(f"{market}: {len(grid)} jump configs", flush=True)
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
