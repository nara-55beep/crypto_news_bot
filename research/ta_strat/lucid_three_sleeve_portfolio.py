"""
Final diversification test: locked NQ noise + NQ opening drift, with one
development-selected ES opening sleeve.  NQ noise has its own risk budget; the two
opening-drift trades share a smaller common budget and the same account constraints.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_eval_portfolio_search as Q
import lucid_noise_area_search as N
import lucid_opening_horizon_search as H
import lucid_portfolio_policy as S


def main() -> int:
    days = {market: L.load_days(market) for market in ("es", "nq")}
    calendars = {
        "train": Q.calendar(days, None, L.TRAIN_END),
        "valid": Q.calendar(days, date(2022, 1, 1), L.VALID_END),
    }
    test_dates = Q.calendar(days, date(2024, 1, 1), None)

    noise_cfg = N.NConfig("nq", 14, 0.75, 30, True, "boundary")
    nq_cfg = H.HConfig("nq", 1, 389, "opening", False, 0.50)
    es_cfgs = [
        H.HConfig("es", 5, 389, "opening", False, 0.50),
        H.HConfig("es", 1, 389, "opening", False, 0.50),
        H.HConfig("es", 5, 240, "opening", False, 0.50),
        H.HConfig("es", 5, 389, "opening", False, 0.25),
    ]
    noise = [
        replace(t, strategy="morning_noise_" + t.strategy)
        for t in N.generate(days["nq"], noise_cfg)
    ]
    nq = [
        replace(t, strategy="nq_drift_" + t.strategy)
        for t in H.generate(days["nq"], nq_cfg)
    ]
    es_map = {
        cfg.label: [
            replace(t, strategy="es_drift_" + t.strategy)
            for t in H.generate(days["es"], cfg)
        ]
        for cfg in es_cfgs
    }

    rows = []
    for es_cfg in es_cfgs:
        trades = sorted(
            noise + nq + es_map[es_cfg.label],
            key=lambda t: (t.entry_ts, S.signal_priority(t), t.exit_ts),
        )
        for noise_risk in (75.0, 100.0, 150.0, 200.0):
            for drift_risk in (75.0, 100.0, 150.0, 200.0, 300.0):
                base = S.Policy(noise_risk, drift_risk)
                policy_grid = [base]
                for drawdown_scale in (0.25, 0.50):
                    for profit_scale in (1.50, 2.0):
                        policy_grid.append(replace(
                            base,
                            drawdown_cut=-500.0,
                            drawdown_scale=drawdown_scale,
                            profit_cut=800.0,
                            profit_scale=profit_scale,
                        ))
                for policy in policy_grid:
                    score, result = Q.full_score(trades, calendars, policy)
                    rows.append({
                        "es": es_cfg,
                        "trades": trades,
                        "policy": policy,
                        "score": score,
                        "result": result,
                    })
    rows.sort(key=lambda row: row["score"], reverse=True)
    print(f"combinations={len(rows)}")
    print("\nDEVELOPMENT-LOCKED LEADERS")
    for row in rows[:12]:
        result = row["result"]
        test20 = Q.eval_one(row["trades"], test_dates, row["policy"], 20)
        test30 = Q.eval_one(row["trades"], test_dates, row["policy"], 30)
        print(
            f"ES={row['es'].label}\npolicy={row['policy'].label} "
            f"score={row['score']:.3f}\n"
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
