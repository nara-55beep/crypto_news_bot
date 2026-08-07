"""
High-frequency causal session-drift research.

The strategy decides after a fixed 1-, 5-, or 15-minute observation window and
enters the following minute.  Direction rules are deliberately simple: permanent
long/short bias, opening move, overnight gap, or prior-session move.  Protective
stop distance uses only the prior completed session or ATR known at signal time.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, replace
from datetime import date

import numpy as np
import pandas as pd

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_portfolio_policy as S
import lucid_predictive_research as P


@dataclass(frozen=True)
class DConfig:
    market: str
    entry_minute: int
    mode: str
    reverse: bool
    stop_mode: str
    stop_mult: float
    target_rr: float | None

    @property
    def label(self) -> str:
        target = "time" if self.target_rr is None else f"r{self.target_rr:g}"
        return (
            f"{self.market}_drift_m{self.entry_minute}_{self.mode}_"
            f"rev{int(self.reverse)}_{self.stop_mode}{self.stop_mult:g}_{target}"
        )


def configs(market: str) -> list[DConfig]:
    out = []
    for minute in (1, 5, 15):
        for mode in ("long", "opening", "gap", "prior"):
            reverse_choices = (False,) if mode in ("long", "short") else (False, True)
            for reverse in reverse_choices:
                for stop_mode in ("prior_range", "atr"):
                    for stop_mult in (0.10, 0.25, 0.50):
                        for target in (0.50, 1.0, None):
                            out.append(DConfig(
                                market, minute, mode, reverse,
                                stop_mode, stop_mult, target,
                            ))
    return out


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _timed_exact(day: L.Day, trade: L.Trade) -> L.Trade:
    side = trade.side
    tick = L.MARKETS[trade.market]["tick"]
    pv = L.MARKETS[trade.market]["pv"]
    # The exit order is submitted at the start of the final cached minute.
    exit_i = len(day.op) - 1
    raw_exit = float(day.op[exit_i])
    reason = "time_open"
    matches = np.flatnonzero(day.ts == pd.Timestamp(trade.entry_ts))
    entry_i = int(matches[0])
    for j in range(entry_i, exit_i + 1):
        stopped = day.lo[j] <= trade.stop if side > 0 else day.hi[j] >= trade.stop
        if not stopped:
            continue
        raw_exit = (
            min(trade.stop, float(day.op[j]))
            if side > 0 else max(trade.stop, float(day.op[j]))
        )
        exit_i = j
        reason = "stop_protective"
        break
    exit_px = raw_exit - side * tick
    return replace(
        trade,
        strategy="exact_" + trade.strategy,
        exit_ts=pd.Timestamp(day.ts[exit_i]),
        exit=exit_px,
        reason=reason,
        gross_per_micro=side * (exit_px - trade.entry) * pv,
    )


def generate(days: list[L.Day], cfg: DConfig) -> list[L.Trade]:
    out = []
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        idx = np.flatnonzero(day.minute <= signal_minute)
        if not len(idx) or day.minute[idx[-1]] != signal_minute:
            continue
        i = int(idx[-1])
        if cfg.mode == "long":
            side = 1
        elif cfg.mode == "short":
            side = -1
        elif cfg.mode == "opening":
            side = _sign(float(day.cl[i] - day.op[0]))
        elif cfg.mode == "gap":
            side = _sign(float(day.op[0] - prior.cl[-1]))
        else:
            side = _sign(float(prior.cl[-1] - prior.op[0]))
        if cfg.reverse:
            side *= -1
        if not side:
            continue
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        if cfg.stop_mode == "prior_range":
            stop_points = cfg.stop_mult * prior_range
        else:
            stop_points = cfg.stop_mult * float(day.atr[i]) * math.sqrt(
                max(1, 390 - cfg.entry_minute)
            )
        if not math.isfinite(stop_points) or stop_points <= 0:
            continue
        raw = P._timed_trade(
            day,
            i,
            side,
            stop_points,
            cfg.label,
            target_rr=cfg.target_rr,
        )
        if raw is None:
            continue
        exact = (
            _timed_exact(day, raw)
            if cfg.target_rr is None
            else B.convert(day, raw, protective_stop=True)
        )
        if exact is not None:
            out.append(exact)
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
    finalists = [row for row in rows if row["score"] > -1e8][:args.top]
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
