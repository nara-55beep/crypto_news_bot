"""
Strict causal test of consecutive-close reversal and continuation.

After N completed closes move in the same direction, enter at the next one-minute
open.  Multiple trades per session are allowed only after the previous exact exit.
Profit exits require a completed close and execute at the following open; the sole
intrabar order is a gap-aware protective stop.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E


@dataclass(frozen=True)
class CConfig:
    market: str
    n: int
    direction: str
    stop_mode: str
    stop_mult: float
    rr: float
    max_trades: int

    @property
    def label(self) -> str:
        return (
            f"{self.market}_consec_n{self.n}_{self.direction}_"
            f"{self.stop_mode}{self.stop_mult:g}_r{self.rr:g}_mx{self.max_trades}"
        )


def configs(market: str) -> list[CConfig]:
    return [
        CConfig(market, n, direction, stop_mode, stop_mult, rr, max_trades)
        for n in (2, 3, 4)
        for direction in ("reverse", "continue")
        for stop_mode in ("run", "atr")
        for stop_mult in (0.75, 1.25)
        for rr in (0.50, 0.75, 1.0)
        for max_trades in (1, 3)
    ]


def generate(days: list[L.Day], cfg: CConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    for day in days:
        i = 16
        completed = 0
        while i < len(day.cl) - 1 and completed < cfg.max_trades:
            if day.minute[i] >= 300:
                break
            changes = np.sign(np.diff(day.cl[max(0, i - cfg.n):i + 1]))
            if len(changes) < cfg.n or np.any(changes == 0) or not np.all(
                changes == changes[-1]
            ):
                i += 1
                continue
            run_side = int(changes[-1])
            side = -run_side if cfg.direction == "reverse" else run_side
            fi = i + 1
            entry = float(day.op[fi]) + side * tick
            if cfg.stop_mode == "run":
                start = max(0, i - cfg.n + 1)
                extreme = (
                    float(np.min(day.lo[start:i + 1]))
                    if side > 0 else
                    float(np.max(day.hi[start:i + 1]))
                )
                stop = extreme - side * tick
            else:
                distance = cfg.stop_mult * float(day.atr[i])
                stop = entry - side * max(distance, tick)
            raw = L._make_trade(
                day,
                i,
                side,
                float(stop),
                rr=cfg.rr,
                strategy=cfg.label,
            )
            exact = None if raw is None else B.convert(
                day, raw, protective_stop=True
            )
            if exact is None:
                i += 1
                continue
            out.append(exact)
            completed += 1
            exit_matches = np.flatnonzero(
                day.ts == pd.Timestamp(exact.exit_ts)
            )
            if not len(exit_matches):
                break
            i = int(exit_matches[0]) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=("es", "nq", "cl"), required=True)
    ap.add_argument("--top", type=int, default=12)
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
        for risk in (50.0, 75.0, 100.0, 150.0, 200.0, 300.0):
            score, result = E.evaluation_score(row["trades"], days, risk)
            policies.append((score, risk, row, result))
    policies.sort(key=lambda item: item[0], reverse=True)
    test_days = E.period_days(days, date(2024, 1, 1), None)
    print("\nDEVELOPMENT-LOCKED LEADERS")
    for score, risk, row, result in policies[:10]:
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
