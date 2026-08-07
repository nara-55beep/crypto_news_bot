"""
Causal research pass for opening-gap and opening-overreaction effects.

The rules in this file are intentionally fixed-time.  They never search forward for
the best entry bar:

  opening_gap
    Decide after a predeclared number of completed minutes.  Trade either the brief
    continuation or the documented opening-gap reversal.  Entry is next-minute open.

  opening_reversal
    Fade a sufficiently large completed opening move at minute 30 or 60.

  gap_close
    Use the already-known close-to-open gap to predict the final half hour.  Decide on
    the completed 15:29 bar and enter the 15:30 open.

The shared engine applies adverse entry/exit ticks, stop-first one-minute ambiguity,
gap-worse stops, integer micros, commission, and Lucid rolling-window accounting.
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
class GapConfig:
    market: str
    family: str
    entry_minute: int
    threshold: float
    direction: str = "reverse"
    confirmation: str = "any"
    stop_mode: str = "extreme"
    target_mode: str = "rr"
    rr: float = 1.5
    stop_mult: float = 1.0
    gap_relation: str = "any"

    @property
    def label(self) -> str:
        return (
            f"{self.market}_{self.family}_m{self.entry_minute}_th{self.threshold:g}_"
            f"{self.direction}_{self.confirmation}_{self.stop_mode}_"
            f"{self.target_mode}{self.rr:g}_s{self.stop_mult:g}_{self.gap_relation}"
        )


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _fixed_target_trade(
    day: L.Day,
    signal_i: int,
    side: int,
    stop: float,
    cfg: GapConfig,
    reference: float | None,
) -> L.Trade | None:
    fixed_target = None
    if cfg.target_mode == "reference":
        if reference is None:
            return None
        # The target must still be ahead of the executable next-minute entry.
        fi = signal_i + 1
        if fi >= len(day.op):
            return None
        entry = float(day.op[fi]) + side * L.MARKETS[day.market]["tick"]
        if (side > 0 and reference <= entry) or (side < 0 and reference >= entry):
            return None
        fixed_target = float(reference)
    return L._make_trade(
        day,
        signal_i,
        side,
        stop,
        rr=cfg.rr,
        strategy=cfg.label,
        fixed_target=fixed_target,
    )


def opening_gap(days: list[L.Day], cfg: GapConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        idx = np.flatnonzero(day.minute <= signal_minute)
        if not len(idx) or day.minute[idx[-1]] != signal_minute:
            continue
        i = int(idx[-1])
        prior_close = float(prior.cl[-1])
        gap = float(day.op[0]) - prior_close
        gap_sign = _sign(gap)
        if not gap_sign or abs(gap) / prior_close < cfg.threshold:
            continue
        drive = float(day.cl[i] - day.op[0])
        if cfg.confirmation == "overextend" and _sign(drive) != gap_sign:
            continue
        if cfg.confirmation == "turn" and _sign(drive) != -gap_sign:
            continue
        side = gap_sign if cfg.direction == "continue" else -gap_sign
        if cfg.stop_mode == "extreme":
            stop = (
                float(np.min(day.lo[:i + 1])) - tick
                if side > 0 else
                float(np.max(day.hi[:i + 1])) + tick
            )
        else:
            horizon_scale = math.sqrt(max(1, cfg.entry_minute))
            distance = cfg.stop_mult * float(day.atr[i]) * horizon_scale
            fi = i + 1
            if fi >= len(day.op):
                continue
            entry = float(day.op[fi]) + side * tick
            stop = entry - side * max(distance, tick)
        reference = prior_close if cfg.target_mode == "reference" else None
        trade = _fixed_target_trade(day, i, side, stop, cfg, reference)
        if trade is not None:
            out.append(trade)
    return out


def opening_reversal(days: list[L.Day], cfg: GapConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        idx = np.flatnonzero(day.minute <= signal_minute)
        if not len(idx) or day.minute[idx[-1]] != signal_minute:
            continue
        i = int(idx[-1])
        opening_move = float(day.cl[i] - day.op[0])
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        if prior_range <= 0 or abs(opening_move) < cfg.threshold * prior_range:
            continue
        side = -_sign(opening_move)
        gap = float(day.op[0] - prior.cl[-1])
        relation = _sign(gap) * _sign(opening_move)
        if cfg.gap_relation == "same" and relation <= 0:
            continue
        if cfg.gap_relation == "opposite" and relation >= 0:
            continue
        stop = (
            float(np.min(day.lo[:i + 1])) - tick
            if side > 0 else
            float(np.max(day.hi[:i + 1])) + tick
        )
        reference = float(day.op[0]) if cfg.target_mode == "reference" else None
        trade = _fixed_target_trade(day, i, side, stop, cfg, reference)
        if trade is not None:
            out.append(trade)
    return out


def gap_close(days: list[L.Day], cfg: GapConfig) -> list[L.Trade]:
    """Trade the overnight-gap sign (or reverse) during the final half hour."""
    out = []
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        idx = np.flatnonzero(day.minute <= signal_minute)
        if not len(idx) or day.minute[idx[-1]] != signal_minute:
            continue
        i = int(idx[-1])
        prior_close = float(prior.cl[-1])
        gap = float(day.op[0]) - prior_close
        gap_sign = _sign(gap)
        if not gap_sign or abs(gap) / prior_close < cfg.threshold:
            continue
        side = gap_sign if cfg.direction == "continue" else -gap_sign
        stop_points = cfg.stop_mult * float(day.atr[i]) * math.sqrt(
            max(1, 390 - cfg.entry_minute)
        )
        trade = P._timed_trade(
            day,
            i,
            side,
            stop_points,
            cfg.label,
            end_minute=389,
            target_rr=None if cfg.target_mode == "time" else cfg.rr,
        )
        if trade is not None:
            out.append(trade)
    return out


def configs(markets: list[str]) -> list[GapConfig]:
    out = []
    for market in markets:
        # The 2000/2005 studies report continuation during roughly the first ten
        # minutes and reversal thereafter, especially beyond a 0.20% gap.
        for minute in (10, 15, 30):
            for threshold in (0.001, 0.002, 0.003):
                for confirmation in ("any", "overextend", "turn"):
                    for stop_mode in ("extreme", "atr"):
                        for target_mode, rr in (("reference", 1.0), ("rr", 1.5), ("rr", 2.0)):
                            out.append(GapConfig(
                                market, "opening_gap", minute, threshold,
                                "reverse", confirmation, stop_mode, target_mode, rr,
                            ))
        for minute in (1, 5, 10):
            for threshold in (0.001, 0.002, 0.003):
                for stop_mode in ("extreme", "atr"):
                    for rr in (1.5, 2.0):
                        out.append(GapConfig(
                            market, "opening_gap", minute, threshold,
                            "continue", "any", stop_mode, "rr", rr,
                        ))
        for minute in (30, 60):
            for threshold in (0.10, 0.25, 0.50):
                for relation in ("any", "same", "opposite"):
                    for target_mode, rr in (("reference", 1.0), ("rr", 1.5), ("rr", 2.0)):
                        out.append(GapConfig(
                            market, "opening_reversal", minute, threshold,
                            target_mode=target_mode, rr=rr, gap_relation=relation,
                        ))
        for threshold in (0.0, 0.001, 0.002, 0.003):
            for direction in ("continue", "reverse"):
                for stop_mult in (0.75, 1.25, 2.0):
                    for target_mode in ("time", "rr"):
                        out.append(GapConfig(
                            market, "gap_close", 360, threshold,
                            direction, "any", "atr", target_mode, 2.0, stop_mult,
                        ))
    return out


def generate(days: list[L.Day], cfg: GapConfig) -> list[L.Trade]:
    if cfg.family == "opening_gap":
        return opening_gap(days, cfg)
    if cfg.family == "opening_reversal":
        return opening_reversal(days, cfg)
    return gap_close(days, cfg)


def _stats(trades: list[L.Trade], risk: float, lo: date | None, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 25:
        return -1e9
    stability = min(train["pf"], valid["pf"])
    return stability * math.log1p(min(train["n"], valid["n"])) + min(
        train["avg"], valid["avg"]
    ) / 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["es", "nq", "cl"])
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    rows = []
    for market in args.markets:
        days = L.load_days(market)
        market_cfgs = [c for c in configs([market])]
        print(f"{market}: {len(market_cfgs)} configurations", flush=True)
        for n, cfg in enumerate(market_cfgs, 1):
            trades = generate(days, cfg)
            train = _stats(trades, args.risk, None, L.TRAIN_END)
            valid = _stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
            rows.append({
                "cfg": cfg,
                "trades": trades,
                "train": train,
                "valid": valid,
                "score": _score(train, valid),
            })
            if n % 100 == 0:
                print(f"  {n}/{len(market_cfgs)}", flush=True)

    rows.sort(key=lambda row: row["score"], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for row in rows[:args.top]:
        test = _stats(row["trades"], args.risk, date(2024, 1, 1), None)
        row["test"] = test
        print(f"\n{row['cfg'].label}")
        for name, stats in (
            ("train", row["train"]),
            ("valid", row["valid"]),
            ("TEST", test),
        ):
            print(
                f"  {name:<5} n{stats['n']:4} PF{stats['pf']:.2f} "
                f"net{stats['net']:+9.0f} avg{stats['avg']:+6.1f} "
                f"DD{stats['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
