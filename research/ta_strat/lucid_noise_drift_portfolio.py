"""
Development-only NQ portfolio search combining two distinct causal effects:
the published same-clock noise-area breakout and fixed opening-direction drift.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_eval_portfolio_search as Q
import lucid_noise_area_search as N
import lucid_opening_horizon_search as H
import lucid_portfolio_policy as S


def noise_configs() -> list[N.NConfig]:
    return [
        N.NConfig("nq", 60, 1.25, 30, False, "boundary"),
        N.NConfig("nq", 60, 1.25, 30, True, "boundary"),
        N.NConfig("nq", 14, 0.75, 30, True, "boundary"),
    ]


def drift_configs() -> list[H.HConfig]:
    return [
        H.HConfig("nq", 1, 240, "opening", False, 0.50),
        H.HConfig("nq", 1, 389, "opening", False, 0.50),
        H.HConfig("nq", 5, 389, "prior", True, 0.10),
    ]


def main() -> int:
    days = L.load_days("nq")
    calendars = {
        "train": [d.day for d in days if d.day <= L.TRAIN_END],
        "valid": [
            d.day for d in days
            if date(2022, 1, 1) <= d.day <= L.VALID_END
        ],
    }
    test_dates = [d.day for d in days if d.day >= date(2024, 1, 1)]
    noises = noise_configs()
    drifts = drift_configs()
    noise_trades = {
        cfg.label: [
            replace(t, strategy="morning_noise_" + t.strategy)
            for t in N.generate(days, cfg)
        ]
        for cfg in noises
    }
    drift_trades = {
        cfg.label: [
            replace(t, strategy="drift_" + t.strategy)
            for t in H.generate(days, cfg)
        ]
        for cfg in drifts
    }

    rows = []
    for noise_cfg, drift_cfg in itertools.product(noises, drifts):
        trades = sorted(
            noise_trades[noise_cfg.label] + drift_trades[drift_cfg.label],
            key=lambda t: (t.entry_ts, S.signal_priority(t), t.exit_ts),
        )
        for noise_risk in (100.0, 150.0, 200.0, 300.0, 400.0):
            for drift_risk in (150.0, 200.0, 300.0, 400.0, 500.0):
                policy = S.Policy(noise_risk, drift_risk)
                score, result = Q.score30(trades, calendars, policy)
                rows.append({
                    "noise": noise_cfg,
                    "drift": drift_cfg,
                    "trades": trades,
                    "policy": policy,
                    "score": score,
                    "result": result,
                })
    rows.sort(key=lambda row: row["score"], reverse=True)
    print(f"base combinations={len(rows)}")

    adaptive = []
    for row in rows[:12]:
        base_policy = row["policy"]
        policy_grid = [base_policy]
        for drawdown_cut in (-300.0, -500.0):
            for drawdown_scale in (0.25, 0.50):
                for profit_cut in (500.0, 800.0):
                    for profit_scale in (1.50, 2.0):
                        policy_grid.append(replace(
                            base_policy,
                            drawdown_cut=drawdown_cut,
                            drawdown_scale=drawdown_scale,
                            profit_cut=profit_cut,
                            profit_scale=profit_scale,
                        ))
        for policy in policy_grid:
            score, result = Q.full_score(row["trades"], calendars, policy)
            adaptive.append({
                **row,
                "policy": policy,
                "score": score,
                "result": result,
            })
    adaptive.sort(key=lambda row: row["score"], reverse=True)

    print("\nDEVELOPMENT-LOCKED LEADERS")
    for row in adaptive[:12]:
        result = row["result"]
        test20 = Q.eval_one(row["trades"], test_dates, row["policy"], 20)
        test30 = Q.eval_one(row["trades"], test_dates, row["policy"], 30)
        print(
            f"NOISE={row['noise'].label}\nDRIFT={row['drift'].label}\n"
            f"policy={row['policy'].label} score={row['score']:.3f}\n"
            f"  train20 {result['train_20']['pass_rate']:.1%}/"
            f"{result['train_20']['fail_rate']:.1%}; "
            f"valid20 {result['valid_20']['pass_rate']:.1%}/"
            f"{result['valid_20']['fail_rate']:.1%}\n"
            f"  train30 {result['train_30']['pass_rate']:.1%}/"
            f"{result['train_30']['fail_rate']:.1%}; "
            f"valid30 {result['valid_30']['pass_rate']:.1%}/"
            f"{result['valid_30']['fail_rate']:.1%}\n"
            f"  TEST20 {test20['pass_rate']:.1%}/{test20['fail_rate']:.1%}; "
            f"TEST30 {test30['pass_rate']:.1%}/{test30['fail_rate']:.1%}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
