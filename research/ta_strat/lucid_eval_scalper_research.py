"""
Evaluation-shaped, strictly causal strategy search.

This experiment deliberately optimizes for a smoother 25K LucidPro evaluation
rather than for a large reward/risk ratio on each trade.  Every candidate:

* forms its signal only from completed bars;
* enters at the next one-minute open with one adverse tick;
* keeps one resting protective stop, filled no better than the stop after gaps;
* confirms a profit target with a completed one-minute close and exits at the
  following open with one adverse tick;
* uses integer micro contracts, commission, the aggregate contract cap, and the
  prior-EOD trailing loss floor in the shared account simulator.

The 2024+ test period is never used for parameter or risk-policy selection.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_gap_research as G
import lucid_portfolio_policy as S
import lucid_predictive_research as P


def candidate_grid(market: str, families: set[str]) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if "morning" in families:
        for minute in (15, 30):
            for mode in ("drive", "gap_fill"):
                for threshold in (0.0, 0.10):
                    for location in (0.60, 0.80):
                        for stop_mode in ("open", "range"):
                            for rr in (0.25, 0.50, 0.75, 1.0):
                                out.append((
                                    "morning",
                                    P.PConfig(
                                        "morning_regime",
                                        market,
                                        entry_minute=minute,
                                        mode=mode,
                                        threshold=threshold,
                                        location=location,
                                        stop_mode=stop_mode,
                                        target_rr=rr,
                                    ),
                                ))
    if "orb" in families:
        for or_min in (5, 15, 30):
            for tf in (1, 5):
                for cutoff in (60, 120):
                    for stop_mode in ("mid", "opposite", "bar"):
                        for rr in (0.25, 0.50, 0.75, 1.0):
                            out.append((
                                "orb",
                                P.PConfig(
                                    "timely_orb",
                                    market,
                                    tf=tf,
                                    or_min=or_min,
                                    cutoff=cutoff,
                                    volume_ratio=0.0,
                                    stop_mode=stop_mode,
                                    target_rr=rr,
                                ),
                            ))
    if "gap" in families:
        for minute in (15, 30):
            for threshold in (0.001, 0.002):
                for confirmation in ("any", "turn"):
                    for stop_mode in ("extreme", "atr"):
                        for rr in (0.25, 0.50, 0.75, 1.0):
                            out.append((
                                "gap",
                                G.GapConfig(
                                    market,
                                    "opening_gap",
                                    minute,
                                    threshold,
                                    "reverse",
                                    confirmation,
                                    stop_mode,
                                    "rr",
                                    rr,
                                ),
                            ))
    return out


def generate(
    family: str,
    cfg: object,
    days: list[L.Day],
) -> list[L.Trade]:
    if family == "gap":
        raw = G.generate(days, cfg)  # type: ignore[arg-type]
    else:
        raw = P.run_config(cfg, {cfg.market: days})  # type: ignore[arg-type,union-attr]
    # Profit exits are not resting orders.  A completed close must first prove
    # the target traded, so high/low order within the minute cannot help us.
    return B.convert_all(days, raw, protective_stop=True)


def stats(
    trades: list[L.Trade],
    lo: date | None,
    hi: date | None,
    risk: float = 300.0,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def development_score(train: dict, valid: dict) -> float:
    if train["n"] < 100 or valid["n"] < 35:
        return -1e9
    min_pf = min(train["pf"], valid["pf"])
    min_avg = min(train["avg"], valid["avg"])
    min_win = min(train["win"], valid["win"])
    # PF and dollars/trade prevent a superficially high win rate with a loss
    # expectancy from winning the screen.
    return 4.0 * math.log(max(min_pf, 1e-9)) + min_avg / 50.0 + min_win


def period_days(days: list[L.Day], lo: date | None, hi: date | None) -> list[date]:
    return [
        day.day
        for day in days
        if (lo is None or day.day >= lo) and (hi is None or day.day <= hi)
    ]


def evaluation_result(
    trades: list[L.Trade],
    days: list[date],
    risk: float,
    horizon: int,
) -> dict:
    policy = S.Policy(risk, risk)
    return S.evaluate(trades, days, policy, horizon, S.RULES_25K)


def evaluation_score(
    trades: list[L.Trade],
    days: list[L.Day],
    risk: float,
) -> tuple[float, dict]:
    train_days = period_days(days, None, L.TRAIN_END)
    valid_days = period_days(days, date(2022, 1, 1), L.VALID_END)
    results = {}
    for period, selected_days in (("train", train_days), ("valid", valid_days)):
        for horizon in (20, 30):
            results[f"{period}_{horizon}"] = evaluation_result(
                trades, selected_days, risk, horizon
            )
    # The worst development period matters most. Failures are much worse than
    # timeouts because a failed evaluation incurs a reset/repurchase.
    pass_floor = min(
        results["train_30"]["pass_rate"],
        results["valid_30"]["pass_rate"],
    )
    pass_20_floor = min(
        results["train_20"]["pass_rate"],
        results["valid_20"]["pass_rate"],
    )
    fail_ceiling = max(
        results["train_30"]["fail_rate"],
        results["valid_30"]["fail_rate"],
    )
    score = 2.0 * pass_floor + pass_20_floor - 2.5 * fail_ceiling
    return score, results


def compact(result: dict) -> dict:
    return {
        key: result[key]
        for key in (
            "starts",
            "passes",
            "fails",
            "undecided",
            "pass_rate",
            "fail_rate",
            "median_days",
            "mean_pass_days",
            "restricted_mean_days",
        )
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=("es", "nq", "cl"), required=True)
    ap.add_argument(
        "--families",
        nargs="+",
        choices=("morning", "orb", "gap"),
        default=("morning", "orb", "gap"),
    )
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()

    days = L.load_days(args.market)
    screened = []
    for family, cfg in candidate_grid(args.market, set(args.families)):
        trades = generate(family, cfg, days)
        train = stats(trades, None, L.TRAIN_END)
        valid = stats(trades, date(2022, 1, 1), L.VALID_END)
        screened.append({
            "family": family,
            "cfg": cfg,
            "trades": trades,
            "train": train,
            "valid": valid,
            "score": development_score(train, valid),
        })
    screened.sort(key=lambda row: row["score"], reverse=True)
    finalists = [row for row in screened if row["score"] > -1e8][:args.top]

    print(f"{args.market.upper()} screened={len(screened)} finalists={len(finalists)}")
    for rank, row in enumerate(finalists, 1):
        print(
            f"{rank:2}. {row['cfg'].label} score={row['score']:.3f} "
            f"train n{row['train']['n']} PF{row['train']['pf']:.2f} "
            f"W{row['train']['win']:.1%} avg{row['train']['avg']:+.1f}; "
            f"valid n{row['valid']['n']} PF{row['valid']['pf']:.2f} "
            f"W{row['valid']['win']:.1%} avg{row['valid']['avg']:+.1f}"
        )

    policies = []
    for rank, row in enumerate(finalists, 1):
        for risk in (150.0, 200.0, 250.0, 300.0, 400.0, 500.0, 600.0):
            score, results = evaluation_score(row["trades"], days, risk)
            policies.append({
                "rank": rank,
                "row": row,
                "risk": risk,
                "score": score,
                "development": results,
            })
    policies.sort(key=lambda item: item["score"], reverse=True)

    records = []
    test_days = period_days(days, date(2024, 1, 1), None)
    print("\nDEVELOPMENT-LOCKED EVALUATION POLICIES")
    for item in policies[:10]:
        d = item["development"]
        test20 = evaluation_result(
            item["row"]["trades"], test_days, item["risk"], 20
        )
        test30 = evaluation_result(
            item["row"]["trades"], test_days, item["risk"], 30
        )
        test_trade_stats = stats(
            item["row"]["trades"], date(2024, 1, 1), None
        )
        print(
            f"{item['row']['cfg'].label} risk={item['risk']:.0f} "
            f"devscore={item['score']:.3f} | "
            f"tr20 {d['train_20']['pass_rate']:.1%}/"
            f"{d['train_20']['fail_rate']:.1%} "
            f"va20 {d['valid_20']['pass_rate']:.1%}/"
            f"{d['valid_20']['fail_rate']:.1%} "
            f"tr30 {d['train_30']['pass_rate']:.1%}/"
            f"{d['train_30']['fail_rate']:.1%} "
            f"va30 {d['valid_30']['pass_rate']:.1%}/"
            f"{d['valid_30']['fail_rate']:.1%} | "
            f"TEST20 {test20['pass_rate']:.1%}/{test20['fail_rate']:.1%} "
            f"TEST30 {test30['pass_rate']:.1%}/{test30['fail_rate']:.1%} "
            f"testPF {test_trade_stats['pf']:.2f}"
        )
        records.append({
            "market": args.market,
            "family": item["row"]["family"],
            "config": asdict(item["row"]["cfg"]),
            "label": item["row"]["cfg"].label,
            "risk": item["risk"],
            "development_score": item["score"],
            "train_trade_stats": item["row"]["train"],
            "valid_trade_stats": item["row"]["valid"],
            "development": {
                key: compact(value) for key, value in d.items()
            },
            "test_trade_stats": test_trade_stats,
            "test_20": compact(test20),
            "test_30": compact(test30),
        })
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(records, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
