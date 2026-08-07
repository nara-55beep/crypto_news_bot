"""
Causal intraday VWAP-deviation research.

Unlike the invalid original VWAP fade, a signal here is evaluated only after a
predeclared 5- or 15-minute block has closed.  Entry is the following one-minute
open.  The completed block's cumulative VWAP is therefore observable before the
order exists.  Only the earliest qualifying signal is traded each session.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_portfolio_policy as S


@dataclass(frozen=True)
class VConfig:
    market: str
    tf: int
    z: float
    direction: str
    confirmation: str
    stop_mode: str
    stop_mult: float
    target_mode: str
    rr: float

    @property
    def label(self) -> str:
        return (
            f"{self.market}_vwapdev_tf{self.tf}_z{self.z:g}_"
            f"{self.direction}_{self.confirmation}_{self.stop_mode}"
            f"{self.stop_mult:g}_{self.target_mode}{self.rr:g}"
        )


def configs(market: str) -> list[VConfig]:
    out = []
    for tf in (5, 15):
        for z in (0.75, 1.25, 1.75):
            for direction in ("reverse", "continue"):
                for confirmation in ("any", "turn"):
                    for stop_mode in ("atr", "bar"):
                        for stop_mult in (0.75, 1.25):
                            for target_mode, rr in (
                                ("rr", 0.50),
                                ("rr", 0.75),
                                ("rr", 1.0),
                                ("vwap", 1.0),
                            ):
                                out.append(VConfig(
                                    market, tf, z, direction, confirmation,
                                    stop_mode, stop_mult, target_mode, rr,
                                ))
    return out


def generate(days: list[L.Day], cfg: VConfig) -> list[L.Trade]:
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    for day in days:
        for i in L._sample_indices(day, cfg.tf, start=30 + cfg.tf - 1):
            if day.minute[i] >= 300:
                break
            atr = float(day.atr[i])
            if not math.isfinite(atr) or atr <= 0:
                continue
            deviation = float(day.cl[i] - day.vwap[i])
            deviation_sign = 1 if deviation > 0 else -1 if deviation < 0 else 0
            if not deviation_sign or abs(deviation) < cfg.z * atr:
                continue
            side = (
                -deviation_sign if cfg.direction == "reverse"
                else deviation_sign
            )
            block_start = max(0, i - cfg.tf + 1)
            block_move = float(day.cl[i] - day.op[block_start])
            if cfg.confirmation == "turn" and block_move * side <= 0:
                continue
            fi = i + 1
            if fi >= len(day.op):
                continue
            entry = float(day.op[fi]) + side * tick
            if cfg.stop_mode == "atr":
                stop = entry - side * max(tick, cfg.stop_mult * atr)
            else:
                extreme = (
                    float(np.min(day.lo[block_start:i + 1]))
                    if side > 0 else
                    float(np.max(day.hi[block_start:i + 1]))
                )
                stop = extreme - side * tick
            fixed_target = None
            if cfg.target_mode == "vwap":
                fixed_target = float(day.vwap[i])
                if (
                    (side > 0 and fixed_target <= entry)
                    or (side < 0 and fixed_target >= entry)
                ):
                    continue
            raw = L._make_trade(
                day,
                int(i),
                side,
                float(stop),
                rr=cfg.rr,
                strategy=cfg.label,
                fixed_target=fixed_target,
            )
            exact = None if raw is None else B.convert(
                day, raw, protective_stop=True
            )
            if exact is not None:
                out.append(exact)
                break
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
    finalists = [r for r in rows if r["score"] > -1e8][:args.top]

    print(f"{args.market.upper()} configs={len(rows)} finalists={len(finalists)}")
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
        for risk in (200.0, 300.0, 400.0, 500.0, 600.0):
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
