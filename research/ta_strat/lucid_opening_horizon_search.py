"""
Fixed-horizon follow-up to the development-positive NQ opening-direction effect.

Observe a completed 1-, 5-, or 15-minute opening window, enter the following
minute, then exit at a predeclared clock-minute open.  No end-of-bar price is used
for that scheduled exit.  A protective stop based on the prior completed RTH range
is active only between entry and the scheduled exit.
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


@dataclass(frozen=True)
class HConfig:
    market: str
    entry_minute: int
    exit_minute: int
    mode: str
    reverse: bool
    stop_mult: float

    @property
    def label(self) -> str:
        return (
            f"{self.market}_openh_m{self.entry_minute}_x{self.exit_minute}_"
            f"{self.mode}_rev{int(self.reverse)}_pr{self.stop_mult:g}"
        )


def configs(market: str) -> list[HConfig]:
    out = []
    for entry in (1, 5, 15):
        for exit_minute in (30, 60, 120, 240, 389):
            if exit_minute <= entry:
                continue
            for mode in ("long", "opening", "gap", "prior"):
                reverse_choices = (False,) if mode == "long" else (False, True)
                for reverse in reverse_choices:
                    for stop_mult in (0.10, 0.25, 0.50):
                        out.append(HConfig(
                            market, entry, exit_minute, mode, reverse, stop_mult
                        ))
    return out


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def generate(days: list[L.Day], cfg: HConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    pv = L.MARKETS[cfg.market]["pv"]
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        sig = np.flatnonzero(day.minute <= signal_minute)
        exits = np.flatnonzero(day.minute >= cfg.exit_minute)
        if (
            not len(sig)
            or day.minute[sig[-1]] != signal_minute
            or not len(exits)
        ):
            continue
        signal_i = int(sig[-1])
        entry_i = signal_i + 1
        exit_i = int(exits[0])
        if entry_i >= exit_i:
            continue
        if cfg.mode == "long":
            side = 1
        elif cfg.mode == "opening":
            side = _sign(float(day.cl[signal_i] - day.op[0]))
        elif cfg.mode == "gap":
            side = _sign(float(day.op[0] - prior.cl[-1]))
        else:
            side = _sign(float(prior.cl[-1] - prior.op[0]))
        if cfg.reverse:
            side *= -1
        if not side:
            continue
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        stop_points = cfg.stop_mult * prior_range
        if not math.isfinite(stop_points) or stop_points <= 0:
            continue
        entry = float(day.op[entry_i]) + side * tick
        stop = entry - side * max(stop_points, tick)
        raw_exit = float(day.op[exit_i])
        actual_exit_i = exit_i
        reason = "time_open"
        # The scheduled market exit executes at exit_i's open, before that
        # minute's high/low can activate the protective stop.
        for j in range(entry_i, exit_i):
            stopped = day.lo[j] <= stop if side > 0 else day.hi[j] >= stop
            if not stopped:
                continue
            raw_exit = (
                min(stop, float(day.op[j]))
                if side > 0 else max(stop, float(day.op[j]))
            )
            actual_exit_i = j
            reason = "stop_protective"
            break
        exit_px = raw_exit - side * tick
        out.append(L.Trade(
            market=cfg.market,
            strategy=cfg.label,
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
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=("es", "nq"), required=True)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    days = L.load_days(args.market)
    rows = []
    for cfg in configs(args.market):
        trades = generate(days, cfg)
        train = E.stats(trades, None, L.TRAIN_END)
        valid = E.stats(trades, date(2022, 1, 1), L.VALID_END)
        rows.append({
            "cfg": cfg,
            "trades": trades,
            "train": train,
            "valid": valid,
            "score": E.development_score(train, valid),
        })
    rows.sort(key=lambda row: row["score"], reverse=True)
    finalists = rows[:args.top]
    print(f"{args.market.upper()} configs={len(rows)}")
    for rank, row in enumerate(finalists, 1):
        print(
            f"{rank:2}. {row['cfg'].label} score={row['score']:.3f} "
            f"train n{row['train']['n']} PF{row['train']['pf']:.2f} "
            f"W{row['train']['win']:.1%} avg{row['train']['avg']:+.1f}; "
            f"valid n{row['valid']['n']} PF{row['valid']['pf']:.2f} "
            f"W{row['valid']['win']:.1%} avg{row['valid']['avg']:+.1f}"
        )

    policies = []
    for row in finalists:
        for risk in (150.0, 200.0, 300.0, 400.0, 500.0, 600.0):
            score, result = E.evaluation_score(row["trades"], days, risk)
            policies.append((score, risk, row, result))
    policies.sort(key=lambda item: item[0], reverse=True)
    test_days = E.period_days(days, date(2024, 1, 1), None)
    print("\nDEVELOPMENT-LOCKED LEADERS")
    for score, risk, row, result in policies[:12]:
        test20 = E.evaluation_result(row["trades"], test_days, risk, 20)
        test30 = E.evaluation_result(row["trades"], test_days, risk, 30)
        test_stats = E.stats(row["trades"], date(2024, 1, 1), None)
        print(
            f"{row['cfg'].label} risk{risk:.0f} score{score:.3f} | "
            f"tr20 {result['train_20']['pass_rate']:.1%}/"
            f"{result['train_20']['fail_rate']:.1%} "
            f"va20 {result['valid_20']['pass_rate']:.1%}/"
            f"{result['valid_20']['fail_rate']:.1%} "
            f"tr30 {result['train_30']['pass_rate']:.1%}/"
            f"{result['train_30']['fail_rate']:.1%} "
            f"va30 {result['valid_30']['pass_rate']:.1%}/"
            f"{result['valid_30']['fail_rate']:.1%} | "
            f"TEST20 {test20['pass_rate']:.1%}/{test20['fail_rate']:.1%} "
            f"TEST30 {test30['pass_rate']:.1%}/{test30['fail_rate']:.1%} "
            f"PF{test_stats['pf']:.2f} avg{test_stats['avg']:+.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
