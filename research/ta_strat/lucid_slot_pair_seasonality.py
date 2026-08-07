"""
Walk-forward ES/NQ cross-market half-hour seasonality research.

The forecast for each regular-session half-hour slot is estimated exclusively from
that same slot on earlier sessions.  A beta-adjusted MNQ/MES pair is opened at the
slot's one-minute open and closed at the next slot's open.  The final slot closes at
15:59.  Both legs pay one tick of adverse entry and exit slippage plus published
round-turn commissions.

This is a small-universe, implementable test inspired by the documented same-clock-
interval persistence effect.  It does not assume that a bar's high and low occurred
in a favorable order and does not use any observation from the interval traded.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_pair_research as P


@dataclass(frozen=True)
class SlotPairConfig:
    lookback: int
    t_threshold: float
    direction: str
    risk_mult: float

    @property
    def label(self) -> str:
        return (
            f"esnq_slotpair_lb{self.lookback}_t{self.t_threshold:g}_"
            f"{self.direction}_r{self.risk_mult:g}"
        )


@dataclass(frozen=True)
class SlotObservation:
    es_return: float
    nq_return: float
    es_entry: float
    nq_entry: float
    es_exit: float
    nq_exit: float
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp


def _idx(day: L.Day, minute: int) -> int | None:
    found = np.flatnonzero(day.minute == minute)
    return int(found[0]) if len(found) else None


def _observation(es: L.Day, nq: L.Day, start: int) -> SlotObservation | None:
    esi, nqi = _idx(es, start), _idx(nq, start)
    if esi is None or nqi is None:
        return None
    if start < 360:
        ese, nqe = _idx(es, start + 30), _idx(nq, start + 30)
        if ese is None or nqe is None:
            return None
        es_exit, nq_exit = float(es.op[ese]), float(nq.op[nqe])
    else:
        ese, nqe = _idx(es, 389), _idx(nq, 389)
        if ese is None or nqe is None:
            return None
        es_exit, nq_exit = float(es.cl[ese]), float(nq.cl[nqe])
    es_entry, nq_entry = float(es.op[esi]), float(nq.op[nqi])
    if min(es_entry, nq_entry) <= 0:
        return None
    return SlotObservation(
        es_return=es_exit / es_entry - 1.0,
        nq_return=nq_exit / nq_entry - 1.0,
        es_entry=es_entry,
        nq_entry=nq_entry,
        es_exit=es_exit,
        nq_exit=nq_exit,
        entry_ts=pd.Timestamp(nq.ts[nqi]),
        exit_ts=pd.Timestamp(nq.ts[nqe]),
    )


def histories(
    records: list[tuple[L.Day, L.Day]],
) -> dict[int, list[SlotObservation | None]]:
    return {
        start: [_observation(es, nq, start) for es, nq in records]
        for start in range(0, 361, 30)
    }


def _rolling_model(
    observations: list[SlotObservation | None],
    i: int,
    lookback: int,
) -> tuple[float, float, float] | None:
    sample = [
        obs for obs in observations[i - lookback:i]
        if obs is not None
    ]
    if len(sample) < max(15, lookback // 2):
        return None
    x = np.asarray([obs.es_return for obs in sample], dtype=float)
    y = np.asarray([obs.nq_return for obs in sample], dtype=float)
    x_var = float(np.dot(x, x))
    if x_var <= 1e-16:
        return None
    beta = float(np.dot(x, y) / x_var)
    residual = y - beta * x
    mean = float(np.mean(residual))
    sigma = float(np.std(residual, ddof=1))
    if not math.isfinite(sigma) or sigma <= 1e-10:
        return None
    return beta, mean, sigma


def _historical_pair_pnl(
    sample: list[SlotObservation],
    beta: float,
    nq_qty: int,
    es_qty: int,
    side_nq: int,
) -> np.ndarray:
    side_es = -side_nq
    nq_pv, es_pv = L.MARKETS["nq"]["pv"], L.MARKETS["es"]["pv"]
    return np.asarray([
        side_nq * (obs.nq_exit - obs.nq_entry) * nq_pv * nq_qty
        + side_es * (obs.es_exit - obs.es_entry) * es_pv * es_qty
        for obs in sample
    ])


def generate(
    records: list[tuple[L.Day, L.Day]],
    cfg: SlotPairConfig,
    historical: dict[int, list[SlotObservation | None]] | None = None,
) -> list[P.PairTrade]:
    historical = histories(records) if historical is None else historical
    out: list[P.PairTrade] = []
    nq_tick, es_tick = L.MARKETS["nq"]["tick"], L.MARKETS["es"]["tick"]
    nq_pv, es_pv = L.MARKETS["nq"]["pv"], L.MARKETS["es"]["pv"]
    for i in range(cfg.lookback, len(records)):
        es_day, nq_day = records[i]
        for start in range(0, 361, 30):
            current = historical[start][i]
            model = _rolling_model(historical[start], i, cfg.lookback)
            if current is None or model is None:
                continue
            beta, mean, sigma = model
            t_stat = mean / (sigma / math.sqrt(cfg.lookback))
            if abs(t_stat) < cfg.t_threshold or mean == 0:
                continue
            side_nq = 1 if mean > 0 else -1
            if cfg.direction == "reverse":
                side_nq *= -1
            side_es = -side_nq

            # Two MNQ against the nearest beta/notional-matched MES quantity.
            nq_qty = 2
            nq_notional = current.nq_entry * nq_pv * nq_qty
            es_one_notional = current.es_entry * es_pv
            es_qty = max(1, int(round(abs(beta) * nq_notional / es_one_notional)))
            if nq_qty + es_qty > L.MAX_MICROS:
                continue

            nq_entry = current.nq_entry + side_nq * nq_tick
            es_entry = current.es_entry + side_es * es_tick
            nq_exit = current.nq_exit - side_nq * nq_tick
            es_exit = current.es_exit - side_es * es_tick
            gross = (
                side_nq * (nq_exit - nq_entry) * nq_pv * nq_qty
                + side_es * (es_exit - es_entry) * es_pv * es_qty
            )
            commission = float(nq_qty + es_qty) * L.COMMISSION_RT

            sample = [
                obs for obs in historical[start][i - cfg.lookback:i]
                if obs is not None
            ]
            historical_pnl = _historical_pair_pnl(
                sample, beta, nq_qty, es_qty, side_nq
            )
            pnl_sigma = float(np.std(historical_pnl, ddof=1))
            if not math.isfinite(pnl_sigma) or pnl_sigma <= 1.0:
                continue
            out.append(P.PairTrade(
                strategy=cfg.label,
                day=nq_day.day,
                entry_ts=current.entry_ts,
                exit_ts=current.exit_ts,
                gross_per_unit=float(gross),
                stop_risk_per_unit=max(10.0, cfg.risk_mult * pnl_sigma),
                micros_per_unit=nq_qty + es_qty,
                commission_per_unit=commission,
                reason="time",
            ))
    return out


def configs() -> list[SlotPairConfig]:
    return [
        SlotPairConfig(lookback, threshold, direction, risk_mult)
        for lookback in (20, 40, 60)
        for threshold in (0.0, 0.5, 1.0, 1.5)
        for direction in ("continue", "reverse")
        for risk_mult in (1.0, 2.0, 3.0)
    ]


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 100:
        return -1e9
    return min(train["pf"], valid["pf"]) * math.log1p(
        min(train["n"], valid["n"])
    ) + min(train["avg"], valid["avg"]) / 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=100.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    records = P.paired_days(L.load_days("es"), L.load_days("nq"))
    historical = histories(records)
    rows = []
    grid = configs()
    print(f"{len(records)} paired sessions, {len(grid)} configs", flush=True)
    for n, cfg in enumerate(grid, 1):
        trades = generate(records, cfg, historical)
        train = P.stats(trades, args.risk, None, L.TRAIN_END)
        valid = P.stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
        rows.append((_score(train, valid), cfg, trades, train, valid))
        if n % 12 == 0:
            print(f"  {n}/{len(grid)}", flush=True)
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = P.stats(trades, args.risk, date(2024, 1, 1), None)
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
