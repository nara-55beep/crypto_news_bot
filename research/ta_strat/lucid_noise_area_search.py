"""
Causal futures adaptation of the Concretum/Zarattini intraday noise-area rule.

For each clock minute, sigma is the average absolute open-to-close move at that
same clock minute over prior completed sessions.  At predeclared rebalance closes:

    UB = max(today_open, prior_close) * (1 + multiplier * sigma)
    LB = min(today_open, prior_close) * (1 - multiplier * sigma)

Close above UB and VWAP selects long; close below LB and VWAP selects short.
Orders execute at the following one-minute open.  Exposure changes only at the
fixed rebalance schedule.  A fixed, known protective stop is the only intrabar
order; scheduled exits execute at the selected minute's open.
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
class NConfig:
    market: str
    lookback: int
    multiplier: float
    frequency: int
    vwap_filter: bool
    stop_mode: str

    @property
    def label(self) -> str:
        return (
            f"{self.market}_noise_lb{self.lookback}_k{self.multiplier:g}_"
            f"f{self.frequency}_vw{int(self.vwap_filter)}_{self.stop_mode}"
        )


def configs(market: str) -> list[NConfig]:
    return [
        NConfig(market, lookback, multiplier, frequency, vwap_filter, stop_mode)
        for lookback in (14, 30, 60)
        for multiplier in (0.75, 1.0, 1.25)
        for frequency in (15, 30, 60)
        for vwap_filter in (True, False)
        for stop_mode in ("open", "boundary", "opposite")
    ]


def _at_minute(day: L.Day, minute: int) -> int | None:
    idx = np.flatnonzero(day.minute == minute)
    return int(idx[-1]) if len(idx) else None


_SIGMA_CACHE: dict[tuple[int, int, int, int], float | None] = {}


def _sigma(
    days: list[L.Day],
    day_index: int,
    minute: int,
    lookback: int,
) -> float | None:
    key = (id(days), day_index, minute, lookback)
    if key in _SIGMA_CACHE:
        return _SIGMA_CACHE[key]
    moves = []
    for day in days[max(0, day_index - lookback):day_index]:
        i = _at_minute(day, minute)
        if i is None or day.op[0] == 0:
            continue
        moves.append(abs(float(day.cl[i] / day.op[0] - 1.0)))
    if len(moves) < max(10, lookback // 2):
        result = None
    else:
        result = float(np.mean(moves))
    _SIGMA_CACHE[key] = result
    return result


def _trade_segment(
    day: L.Day,
    cfg: NConfig,
    signal_i: int,
    exit_i: int,
    side: int,
    upper: float,
    lower: float,
) -> L.Trade | None:
    entry_i = signal_i + 1
    if entry_i >= exit_i or exit_i >= len(day.op):
        return None
    tick = L.MARKETS[cfg.market]["tick"]
    pv = L.MARKETS[cfg.market]["pv"]
    entry = float(day.op[entry_i]) + side * tick
    if cfg.stop_mode == "open":
        stop = float(day.op[0])
    elif cfg.stop_mode == "boundary":
        stop = (
            max(float(day.vwap[signal_i]), upper)
            if side > 0 else
            min(float(day.vwap[signal_i]), lower)
        )
    else:
        stop = lower if side > 0 else upper
    stop_points = side * (entry - stop)
    if not math.isfinite(stop_points) or stop_points < tick:
        return None

    raw_exit = float(day.op[exit_i])
    actual_exit_i = exit_i
    reason = "rebalance_open"
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
    return L.Trade(
        market=cfg.market,
        strategy=cfg.label,
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[entry_i]),
        exit_ts=pd.Timestamp(day.ts[actual_exit_i]),
        side=side,
        entry=entry,
        stop=float(stop),
        target=math.nan,
        exit=exit_px,
        reason=reason,
        risk_per_micro=(stop_points + tick) * pv,
        gross_per_micro=side * (exit_px - entry) * pv,
    )


def generate(days: list[L.Day], cfg: NConfig) -> list[L.Trade]:
    out = []
    for n in range(cfg.lookback, len(days)):
        day = days[n]
        prior_close = float(days[n - 1].cl[-1])
        checkpoints = [
            int(i)
            for i in np.flatnonzero(
                ((day.minute + 1) % cfg.frequency == 0)
                & (day.minute < 360)
            )
        ]
        observations = []
        for i in checkpoints:
            minute = int(day.minute[i])
            sigma = _sigma(days, n, minute, cfg.lookback)
            if sigma is None:
                continue
            upper = max(float(day.op[0]), prior_close) * (
                1.0 + cfg.multiplier * sigma
            )
            lower = min(float(day.op[0]), prior_close) * (
                1.0 - cfg.multiplier * sigma
            )
            side = 0
            if day.cl[i] > upper and (
                not cfg.vwap_filter or day.cl[i] > day.vwap[i]
            ):
                side = 1
            elif day.cl[i] < lower and (
                not cfg.vwap_filter or day.cl[i] < day.vwap[i]
            ):
                side = -1
            observations.append((i, side, upper, lower))
        if not observations:
            continue
        current_side = 0
        current_obs = None
        for observation in observations:
            i, side, upper, lower = observation
            if side == current_side:
                continue
            if current_side and current_obs is not None:
                trade = _trade_segment(
                    day,
                    cfg,
                    current_obs[0],
                    i + 1,
                    current_side,
                    current_obs[2],
                    current_obs[3],
                )
                if trade is not None:
                    out.append(trade)
            current_side = side
            current_obs = observation if side else None
        if current_side and current_obs is not None:
            final_i = int(np.flatnonzero(day.minute >= 389)[0])
            trade = _trade_segment(
                day,
                cfg,
                current_obs[0],
                final_i,
                current_side,
                current_obs[2],
                current_obs[3],
            )
            if trade is not None:
                out.append(trade)
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
        for risk in (100.0, 150.0, 200.0, 300.0, 400.0, 500.0, 600.0):
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
