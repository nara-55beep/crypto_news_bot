"""
Development-selected 25K portfolio: walk-forward GMM state + gap reversal.

The GMM sleeve is the independently specified active-state model, not a claimed
reproduction of Mesfin's undisclosed model.  Risk budgets are static.  The exact
account replay enforces integer micros, the aggregate 20-micro cap, commissions,
concurrent stop-risk reservation, and Lucid's prior-EOD trailing MLL.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_exact_portfolio as X
import lucid_gmm_confluence_research as G
import lucid_portfolio_policy as S


GMM_CONFIGS = (
    G.GConfig(0.10, 0.25, "long", 13),
    G.GConfig(0.15, 0.25, "long", 13),
)


def _score(result: dict[str, dict[int, dict]]) -> float:
    return min(
        result[period][12]["pass_rate"]
        + result[period][20]["pass_rate"]
        + 2.0 * result[period][30]["pass_rate"]
        - 3.0 * result[period][30]["fail_rate"]
        for period in ("train", "valid")
    )


def main() -> int:
    days = {market: L.load_days(market) for market in ("nq", "es")}
    all_dates = sorted({day.day for rows in days.values() for day in rows})
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    gaps = X.candidates(days)
    bars = G._bars(days["nq"])
    x = G._features(bars)
    signals = G.walkforward_signals(
        bars, x, transition_threshold=-1.0, volume_threshold=-999.0
    )
    gmm_sets = {
        cfg.label: [
            replace(trade, strategy="gmm_state_" + trade.strategy)
            for trade in G.generate(days["nq"], bars, signals, cfg)
        ]
        for cfg in GMM_CONFIGS
    }

    rows = []
    risks = (300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0)
    for gap_name in ("gaps", "frequency_gaps"):
        for gmm_name, gmm_trades in gmm_sets.items():
            trades = sorted(
                gaps[gap_name] + gmm_trades,
                key=lambda trade: (
                    trade.entry_ts, S.signal_priority(trade), trade.exit_ts
                ),
            )
            for gap_risk in risks:
                for gmm_risk in risks:
                    policy = S.Policy(gap_risk, gmm_risk)
                    result = {}
                    for period in ("train", "valid"):
                        lo, hi = periods[period]
                        selected_trades = S._slice_trades(trades, lo, hi)
                        selected_days = S._slice_days(all_dates, lo, hi)
                        result[period] = {
                            horizon: S.evaluate(
                                selected_trades,
                                selected_days,
                                policy,
                                horizon,
                                S.RULES_25K,
                            )
                            for horizon in (12, 20, 30)
                        }
                    rows.append(
                        (
                            _score(result), gap_name, gmm_name, trades,
                            policy, result,
                        )
                    )

    rows.sort(key=lambda row: row[0], reverse=True)
    print(f"development combinations={len(rows)}")
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, gap_name, gmm_name, trades, policy, result in rows[:15]:
        lo, hi = periods["test"]
        test_trades = S._slice_trades(trades, lo, hi)
        test_days = S._slice_days(all_dates, lo, hi)
        test = {
            horizon: S.evaluate(
                test_trades, test_days, policy, horizon, S.RULES_25K
            )
            for horizon in (12, 20, 30)
        }
        print(
            f"\nGAP={gap_name}\nGMM={gmm_name}\n"
            f"policy={policy.label} score={score:.3f}"
        )
        for period, values in (
            ("train", result["train"]),
            ("valid", result["valid"]),
            ("TEST", test),
        ):
            fields = [
                f"{h}d {values[h]['pass_rate']:.1%}/"
                f"{values[h]['fail_rate']:.1%}"
                for h in (12, 20, 30)
            ]
            print(
                f"  {period:<5} {' | '.join(fields)} "
                f"rm30 {values[30]['restricted_mean_days']:.1f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
