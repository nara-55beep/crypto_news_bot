"""
Development-only search for a jointly risk-managed ES/NQ evaluation portfolio.

Signal configurations in this file are the leading candidates from the causal
low-target screen.  Their inclusion and every risk-policy choice are ranked on
2016-2023 only.  The 2024+ result is printed once for the locked leaders.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import replace
from datetime import date

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_gap_research as G
import lucid_portfolio_policy as S
import lucid_predictive_research as P


def configs() -> dict[str, list[object]]:
    return {
        "es": [
            P.PConfig(
                "morning_regime", "es", entry_minute=15, mode="gap_fill",
                threshold=0.10, location=0.80, stop_mode="range", target_rr=1.0,
            ),
            P.PConfig(
                "morning_regime", "es", entry_minute=15, mode="gap_fill",
                threshold=0.0, location=0.80, stop_mode="range", target_rr=1.0,
            ),
            P.PConfig(
                "morning_regime", "es", entry_minute=15, mode="gap_fill",
                threshold=0.0, location=0.80, stop_mode="range", target_rr=0.75,
            ),
            P.PConfig(
                "morning_regime", "es", entry_minute=15, mode="gap_fill",
                threshold=0.10, location=0.60, stop_mode="range", target_rr=1.0,
            ),
        ],
        "nq": [
            P.PConfig(
                "morning_regime", "nq", entry_minute=15, mode="gap_fill",
                threshold=0.10, location=0.80, stop_mode="range", target_rr=1.0,
            ),
            P.PConfig(
                "morning_regime", "nq", entry_minute=15, mode="gap_fill",
                threshold=0.10, location=0.80, stop_mode="range", target_rr=0.50,
            ),
            P.PConfig(
                "morning_regime", "nq", entry_minute=15, mode="gap_fill",
                threshold=0.10, location=0.80, stop_mode="range", target_rr=0.75,
            ),
            G.GapConfig(
                "nq", "opening_gap", 30, 0.002,
                "reverse", "turn", "atr", "rr", 1.0,
            ),
            G.GapConfig(
                "nq", "opening_gap", 15, 0.002,
                "reverse", "turn", "atr", "rr", 1.0,
            ),
            G.GapConfig(
                "nq", "opening_gap", 15, 0.001,
                "reverse", "turn", "atr", "rr", 1.0,
            ),
        ],
    }


def make_trades(market: str, cfg: object, days: list[L.Day]) -> list[L.Trade]:
    if isinstance(cfg, G.GapConfig):
        raw = G.generate(days, cfg)
    else:
        raw = P.run_config(cfg, {market: days})
    exact = B.convert_all(days, raw, protective_stop=True)
    # The shared evaluator has two independent sizing sleeves keyed by the
    # word "morning".  Use that key for NQ and remove it for ES.
    if market == "nq":
        return [replace(t, strategy="morning_nq_" + t.strategy) for t in exact]
    return [
        replace(t, strategy=t.strategy.replace("morning", "session"))
        for t in exact
    ]


def calendar(
    all_days: dict[str, list[L.Day]],
    lo: date | None,
    hi: date | None,
) -> list[date]:
    return sorted({
        d.day
        for days in all_days.values()
        for d in days
        if (lo is None or d.day >= lo) and (hi is None or d.day <= hi)
    })


def eval_one(
    trades: list[L.Trade],
    dates: list[date],
    policy: S.Policy,
    horizon: int,
) -> dict:
    return S.evaluate(trades, dates, policy, horizon, S.RULES_25K)


def score30(
    trades: list[L.Trade],
    calendars: dict[str, list[date]],
    policy: S.Policy,
) -> tuple[float, dict]:
    result = {
        period: eval_one(trades, dates, policy, 30)
        for period, dates in calendars.items()
    }
    pass_floor = min(r["pass_rate"] for r in result.values())
    fail_ceiling = max(r["fail_rate"] for r in result.values())
    return 2.0 * pass_floor - 2.5 * fail_ceiling, result


def full_score(
    trades: list[L.Trade],
    calendars: dict[str, list[date]],
    policy: S.Policy,
) -> tuple[float, dict]:
    result = {}
    for period, dates in calendars.items():
        for horizon in (20, 30):
            result[f"{period}_{horizon}"] = eval_one(
                trades, dates, policy, horizon
            )
    pass30 = min(
        result["train_30"]["pass_rate"], result["valid_30"]["pass_rate"]
    )
    pass20 = min(
        result["train_20"]["pass_rate"], result["valid_20"]["pass_rate"]
    )
    fail = max(
        value["fail_rate"] for value in result.values()
    )
    return 2.0 * pass30 + pass20 - 2.5 * fail, result


def main() -> int:
    all_days = {market: L.load_days(market) for market in ("es", "nq")}
    calendars = {
        "train": calendar(all_days, None, L.TRAIN_END),
        "valid": calendar(all_days, date(2022, 1, 1), L.VALID_END),
    }
    test_calendar = calendar(all_days, date(2024, 1, 1), None)
    cfgs = configs()
    signals = {
        (market, cfg.label): make_trades(market, cfg, all_days[market])
        for market, choices in cfgs.items()
        for cfg in choices
    }

    base = []
    for es_cfg, nq_cfg in itertools.product(cfgs["es"], cfgs["nq"]):
        trades = sorted(
            signals[("es", es_cfg.label)] + signals[("nq", nq_cfg.label)],
            key=lambda t: (t.entry_ts, S.signal_priority(t), t.exit_ts),
        )
        for es_risk in (200.0, 300.0, 400.0, 500.0, 600.0):
            for nq_risk in (200.0, 300.0, 400.0, 500.0, 600.0):
                policy = S.Policy(nq_risk, es_risk)
                score, result = score30(trades, calendars, policy)
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
    for row in base[:12]:
        policy = row["policy"]
        policy_grid = [policy]
        for drawdown_cut in (-300.0, -500.0):
            for drawdown_scale in (0.25, 0.50):
                for profit_cut in (500.0, 800.0):
                    for profit_scale in (1.50, 2.0):
                        policy_grid.append(replace(
                            policy,
                            drawdown_cut=drawdown_cut,
                            drawdown_scale=drawdown_scale,
                            profit_cut=profit_cut,
                            profit_scale=profit_scale,
                        ))
        for candidate_policy in policy_grid:
            score, result = full_score(
                row["trades"], calendars, candidate_policy
            )
            adaptive.append({
                **row,
                "policy": candidate_policy,
                "score": score,
                "result": result,
            })
    adaptive.sort(key=lambda row: row["score"], reverse=True)

    print("\nDEVELOPMENT-LOCKED LEADERS")
    seen = set()
    printed = 0
    for row in adaptive:
        key = (row["es"].label, row["nq"].label, row["policy"].label)
        if key in seen:
            continue
        seen.add(key)
        result = row["result"]
        test20 = eval_one(row["trades"], test_calendar, row["policy"], 20)
        test30 = eval_one(row["trades"], test_calendar, row["policy"], 30)
        trade_test = E.stats(row["trades"], date(2024, 1, 1), None)
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
            f"TEST30 {test30['pass_rate']:.1%}/{test30['fail_rate']:.1%}; "
            f"PF{trade_test['pf']:.2f} avg{trade_test['avg']:+.1f}\n"
        )
        printed += 1
        if printed == 10:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
