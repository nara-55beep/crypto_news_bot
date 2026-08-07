"""
Account-level ES/NQ portfolio search for the fixed-horizon opening effects.

All signal configurations were selected by the preceding development screen.
Portfolio membership, per-market risk, and adaptive sizing are ranked on
2016-2023 only before the 2024+ result is evaluated.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_eval_portfolio_search as Q
import lucid_opening_horizon_search as H
import lucid_portfolio_policy as S


def configs() -> dict[str, list[H.HConfig]]:
    return {
        "es": [
            H.HConfig("es", 5, 389, "opening", False, 0.50),
            H.HConfig("es", 1, 389, "opening", False, 0.50),
            H.HConfig("es", 5, 240, "opening", False, 0.50),
            H.HConfig("es", 5, 389, "opening", False, 0.25),
        ],
        "nq": [
            H.HConfig("nq", 1, 240, "opening", False, 0.50),
            H.HConfig("nq", 1, 389, "opening", False, 0.50),
            H.HConfig("nq", 5, 389, "prior", True, 0.10),
            H.HConfig("nq", 15, 389, "opening", False, 0.25),
            H.HConfig("nq", 1, 120, "opening", False, 0.50),
        ],
    }


def tagged(market: str, trades: list[L.Trade]) -> list[L.Trade]:
    if market == "nq":
        return [replace(t, strategy="morning_nq_" + t.strategy) for t in trades]
    return [replace(t, strategy="es_" + t.strategy) for t in trades]


def main() -> int:
    days = {market: L.load_days(market) for market in ("es", "nq")}
    calendars = {
        "train": Q.calendar(days, None, L.TRAIN_END),
        "valid": Q.calendar(days, date(2022, 1, 1), L.VALID_END),
    }
    test_dates = Q.calendar(days, date(2024, 1, 1), None)
    cfgs = configs()
    generated = {
        (market, cfg.label): tagged(market, H.generate(days[market], cfg))
        for market, choices in cfgs.items()
        for cfg in choices
    }

    base = []
    for es_cfg, nq_cfg in itertools.product(cfgs["es"], cfgs["nq"]):
        trades = sorted(
            generated[("es", es_cfg.label)] + generated[("nq", nq_cfg.label)],
            key=lambda t: (t.entry_ts, S.signal_priority(t), t.exit_ts),
        )
        for es_risk in (150.0, 200.0, 300.0, 400.0, 500.0, 600.0):
            for nq_risk in (150.0, 200.0, 300.0, 400.0, 500.0, 600.0):
                policy = S.Policy(nq_risk, es_risk)
                score, result = Q.score30(trades, calendars, policy)
                base.append({
                    "es": es_cfg,
                    "nq": nq_cfg,
                    "trades": trades,
                    "policy": policy,
                    "score": score,
                    "result": result,
                })
    base.sort(key=lambda row: row["score"], reverse=True)
    print(f"base combinations={len(base)}")

    adaptive = []
    for row in base[:15]:
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
            score, result = Q.full_score(
                row["trades"], calendars, policy
            )
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
            f"ES={row['es'].label}\nNQ={row['nq'].label}\n"
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
