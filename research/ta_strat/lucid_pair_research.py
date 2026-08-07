"""
Strictly causal ES/NQ relative-value research.

At a fixed clock time, estimate NQ's beta to ES from prior sessions only.  If the
completed opening returns diverge by a predeclared rolling-z threshold, trade a
dollar-hedged MES/MNQ pair at the next one-minute opens.  Stops, targets, and
mean-reversion exits are detected from later completed closes and filled at the next
minute opens.  No same-bar high/low ordering is used.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L


@dataclass(frozen=True)
class PairConfig:
    entry_minute: int
    lookback: int
    entry_z: float
    stop_usd: float
    exit_mode: str
    rr: float
    exit_minute: int

    @property
    def label(self) -> str:
        return (
            f"esnq_pair_m{self.entry_minute}_lb{self.lookback}_z{self.entry_z:g}_"
            f"stop{self.stop_usd:g}_{self.exit_mode}_r{self.rr:g}_x{self.exit_minute}"
        )


@dataclass(frozen=True)
class PairTrade:
    strategy: str
    day: date
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    gross_per_unit: float
    stop_risk_per_unit: float
    micros_per_unit: int
    commission_per_unit: float
    reason: str


@dataclass(frozen=True)
class SizedPairTrade:
    trade: PairTrade
    units: int
    pnl: float


def _minute_index(day: L.Day, minute: int) -> int | None:
    idx = np.flatnonzero(day.minute == minute)
    return int(idx[0]) if len(idx) else None


def paired_days(es: list[L.Day], nq: list[L.Day]) -> list[tuple[L.Day, L.Day]]:
    es_map = {d.day: d for d in es}
    nq_map = {d.day: d for d in nq}
    return [(es_map[d], nq_map[d]) for d in sorted(set(es_map) & set(nq_map))]


def _opening_return(day: L.Day, signal_minute: int) -> float | None:
    i = _minute_index(day, signal_minute)
    if i is None or day.op[0] <= 0:
        return None
    return float(day.cl[i] / day.op[0] - 1.0)


def rolling_models(
    records: list[tuple[L.Day, L.Day]],
    entry_minute: int,
    lookback: int,
) -> list[tuple[float, float, float] | None]:
    """(beta, historical residual std, current residual), using prior days only."""
    signal_minute = entry_minute - 1
    x = np.array([
        _opening_return(es, signal_minute) for es, _ in records
    ], dtype=float)
    y = np.array([
        _opening_return(nq, signal_minute) for _, nq in records
    ], dtype=float)
    out: list[tuple[float, float, float] | None] = [None] * len(records)
    for i in range(lookback, len(records)):
        hx, hy = x[i - lookback:i], y[i - lookback:i]
        good = np.isfinite(hx) & np.isfinite(hy)
        if int(good.sum()) < max(30, lookback // 2):
            continue
        xx, yy = hx[good], hy[good]
        variance = float(np.dot(xx, xx))
        if variance <= 1e-16:
            continue
        beta = float(np.dot(xx, yy) / variance)
        residuals = yy - beta * xx
        sigma = float(np.std(residuals, ddof=1))
        if not math.isfinite(sigma) or sigma <= 1e-8:
            continue
        if not math.isfinite(x[i]) or not math.isfinite(y[i]):
            continue
        out[i] = (beta, sigma, float(y[i] - beta * x[i]))
    return out


def _exit_pnl(
    nq: L.Day,
    es: L.Day,
    minute: int,
    side_nq: int,
    side_es: int,
    nq_qty: int,
    es_qty: int,
    nq_entry: float,
    es_entry: float,
    *,
    use_open: bool,
) -> float | None:
    ni, ei = _minute_index(nq, minute), _minute_index(es, minute)
    if ni is None or ei is None:
        return None
    nq_raw = float(nq.op[ni] if use_open else nq.cl[ni])
    es_raw = float(es.op[ei] if use_open else es.cl[ei])
    nq_exit = nq_raw - side_nq * L.MARKETS["nq"]["tick"]
    es_exit = es_raw - side_es * L.MARKETS["es"]["tick"]
    return (
        side_nq * (nq_exit - nq_entry) * L.MARKETS["nq"]["pv"] * nq_qty
        + side_es * (es_exit - es_entry) * L.MARKETS["es"]["pv"] * es_qty
    )


def generate(
    records: list[tuple[L.Day, L.Day]],
    cfg: PairConfig,
    models: list[tuple[float, float, float] | None],
) -> list[PairTrade]:
    out = []
    for i, ((es, nq), model) in enumerate(zip(records, models)):
        if model is None:
            continue
        beta, sigma, residual = model
        if abs(residual) < cfg.entry_z * sigma:
            continue
        ni, ei = _minute_index(nq, cfg.entry_minute), _minute_index(es, cfg.entry_minute)
        if ni is None or ei is None:
            continue
        # Rich NQ residual: short NQ and buy beta-adjusted ES; vice versa.
        side_nq = -1 if residual > 0 else 1
        side_es = -side_nq
        nq_qty = 2
        nq_notional = float(nq.op[ni]) * L.MARKETS["nq"]["pv"] * nq_qty
        es_one_notional = float(es.op[ei]) * L.MARKETS["es"]["pv"]
        es_qty = max(1, int(round(abs(beta) * nq_notional / es_one_notional)))
        if es_qty + nq_qty > L.MAX_MICROS:
            continue
        nq_entry = float(nq.op[ni]) + side_nq * L.MARKETS["nq"]["tick"]
        es_entry = float(es.op[ei]) + side_es * L.MARKETS["es"]["tick"]
        commission = float(nq_qty + es_qty) * L.COMMISSION_RT
        reason = "time"
        exit_minute = cfg.exit_minute
        gross = _exit_pnl(
            nq, es, exit_minute, side_nq, side_es, nq_qty, es_qty,
            nq_entry, es_entry, use_open=True,
        )
        if gross is None:
            continue

        # A close-based condition is always executed at the following minute open.
        for minute in range(cfg.entry_minute, cfg.exit_minute):
            marked = _exit_pnl(
                nq, es, minute, side_nq, side_es, nq_qty, es_qty,
                nq_entry, es_entry, use_open=False,
            )
            if marked is None:
                continue
            current_es = _opening_return(es, minute)
            current_nq = _opening_return(nq, minute)
            current_residual = (
                math.nan
                if current_es is None or current_nq is None
                else current_nq - beta * current_es
            )
            hit_stop = marked <= -cfg.stop_usd
            hit_target = marked >= cfg.rr * cfg.stop_usd
            hit_mean = (
                cfg.exit_mode == "mean"
                and math.isfinite(current_residual)
                and residual * current_residual <= 0
            )
            if not (hit_stop or hit_target or hit_mean):
                continue
            next_minute = minute + 1
            executed = _exit_pnl(
                nq, es, next_minute, side_nq, side_es, nq_qty, es_qty,
                nq_entry, es_entry, use_open=True,
            )
            if executed is None:
                continue
            gross = executed
            exit_minute = next_minute
            reason = "stop" if hit_stop else "target" if hit_target else "mean"
            break

        exit_i = _minute_index(nq, exit_minute)
        if exit_i is None:
            continue
        out.append(PairTrade(
            strategy=cfg.label,
            day=nq.day,
            entry_ts=pd.Timestamp(nq.ts[ni]),
            exit_ts=pd.Timestamp(nq.ts[exit_i]),
            gross_per_unit=float(gross),
            stop_risk_per_unit=cfg.stop_usd,
            micros_per_unit=nq_qty + es_qty,
            commission_per_unit=commission,
            reason=reason,
        ))
    return out


def size_trades(trades: list[PairTrade], risk_usd: float) -> list[SizedPairTrade]:
    out = []
    for trade in trades:
        all_in = trade.stop_risk_per_unit + trade.commission_per_unit
        units = min(
            L.MAX_MICROS // trade.micros_per_unit,
            int(math.floor(risk_usd / all_in)),
        )
        if units < 1:
            continue
        pnl = (trade.gross_per_unit - trade.commission_per_unit) * units
        out.append(SizedPairTrade(trade, units, pnl))
    return out


def stats(trades: list[PairTrade], risk: float, lo: date | None, hi: date | None) -> dict:
    selected = [
        t for t in trades
        if (lo is None or t.day >= lo) and (hi is None or t.day <= hi)
    ]
    sized = size_trades(selected, risk)
    if not sized:
        return {"n": 0, "pf": 0.0, "net": 0.0, "avg": 0.0, "maxdd": 0.0}
    pnl = np.array([t.pnl for t in sized])
    gp = float(pnl[pnl > 0].sum())
    gl = float(-pnl[pnl <= 0].sum())
    curve = np.cumsum(pnl)
    peaks = np.maximum.accumulate(np.r_[0.0, curve])[:-1]
    return {
        "n": len(pnl),
        "pf": gp / gl if gl > 0 else 99.0,
        "net": float(pnl.sum()),
        "avg": float(pnl.mean()),
        "maxdd": float(np.min(curve - peaks)),
    }


def configs() -> list[PairConfig]:
    return [
        PairConfig(entry, lookback, z, stop, mode, rr, exit_minute)
        for entry in (15, 30)
        for lookback in (60, 120)
        for z in (1.0, 1.5, 2.0)
        for stop in (50.0, 100.0)
        for mode in ("mean", "target")
        for rr in (1.5,)
        for exit_minute in (120, 240, 389)
        if exit_minute > entry
    ]


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 25:
        return -1e9
    return min(train["pf"], valid["pf"]) * math.log1p(
        min(train["n"], valid["n"])
    ) + min(train["avg"], valid["avg"]) / 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    records = paired_days(L.load_days("es"), L.load_days("nq"))
    model_cache = {}
    rows = []
    grid = configs()
    print(f"{len(records)} paired sessions, {len(grid)} configs", flush=True)
    for n, cfg in enumerate(grid, 1):
        key = (cfg.entry_minute, cfg.lookback)
        if key not in model_cache:
            model_cache[key] = rolling_models(records, *key)
        trades = generate(records, cfg, model_cache[key])
        train = stats(trades, args.risk, None, L.TRAIN_END)
        valid = stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
        rows.append(( _score(train, valid), cfg, trades, train, valid))
        if n % 50 == 0:
            print(f"  {n}/{len(grid)}", flush=True)
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = stats(trades, args.risk, date(2024, 1, 1), None)
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
