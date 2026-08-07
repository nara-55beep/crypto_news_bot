"""
Development-only Lucid risk search for the frozen price-only regime strategy.

The state/trade parameters are fixed.  Risk pairs are ranked on 2016--2023
only, under a conservative two-NQ-point all-in round-trip cost.  The 2024+
segment is printed only after ranking, but it is no longer a pristine holdout
because prior experiments in this research program have already exposed it.
"""
from __future__ import annotations

from datetime import date

import lucid_causal_rebuild as L
import lucid_gmm_confluence_research as G
import lucid_gmm_regime_portfolio as R
import lucid_gmm_robustness_research as B
import lucid_portfolio_policy as S


ACTIVE = G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 2)
BEAR = G.GConfig(0.15, 0.25, "short", 13, 30, 300, 13, 2)


def _result(
    trades: list[L.Trade],
    dates: list[date],
    policy: S.Policy,
) -> dict[int, dict]:
    return {
        horizon: S.evaluate(
            trades,
            dates,
            policy,
            horizon,
            S.RULES_25K,
        )
        for horizon in (20, 30)
    }


def _score(train: dict[int, dict], valid: dict[int, dict]) -> float:
    # Favor the weaker development segment and penalize actual breaches more
    # than unresolved 30-day windows.
    return min(
        values[30]["pass_rate"]
        + 0.25 * values[20]["pass_rate"]
        - 3.0 * values[30]["fail_rate"]
        for values in (train, valid)
    )


def main() -> int:
    days, all_dates = L.load_days_and_sessions("nq")
    bars = G._bars(days)
    features = G._features(bars)[:, :2]
    states = G.walkforward_states(
        bars,
        features,
        train_bars=5_000,
        random_state=20260731,
    )
    active = G.generate(days, bars, R._signals(bars, states, 1), ACTIVE)
    bear = G.generate(days, bars, R._signals(bars, states, 0), BEAR)
    raw = R.merge_nonoverlap(
        active,
        bear,
        active_first=True,
        daily_cap=3,
    )
    # Native execution deducts $1.50/MNQ micro (one exit tick plus $1 RT
    # commission).  Another $2.50 makes the conservative total $4 = 2 points.
    trades = B._extra_cost(raw, 2.5)

    train_dates = S._slice_days(all_dates, None, L.TRAIN_END)
    valid_dates = S._slice_days(
        all_dates, date(2022, 1, 1), L.VALID_END
    )
    test_dates = S._slice_days(all_dates, date(2024, 1, 1), None)
    train_trades = S._slice_trades(trades, None, L.TRAIN_END)
    valid_trades = S._slice_trades(
        trades, date(2022, 1, 1), L.VALID_END
    )
    test_trades = S._slice_trades(trades, date(2024, 1, 1), None)

    rows = []
    for active_risk in (100.0, 200.0, 300.0, 400.0, 500.0):
        for bear_risk in (100.0, 200.0, 300.0, 400.0, 500.0):
            policy = S.Policy(active_risk, bear_risk)
            train = _result(train_trades, train_dates, policy)
            valid = _result(valid_trades, valid_dates, policy)
            rows.append((
                _score(train, valid),
                active_risk,
                bear_risk,
                policy,
                train,
                valid,
            ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print(
        f"complete_sessions={len(days)} eval_sessions={len(all_dates)} "
        f"trades={len(trades)} policies={len(rows)}"
    )
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for (
        score,
        active_risk,
        bear_risk,
        policy,
        train,
        valid,
    ) in rows[:10]:
        test = _result(test_trades, test_dates, policy)
        print(
            f"\nactive=${active_risk:.0f} bear=${bear_risk:.0f} "
            f"development_score={score:.3f}"
        )
        for name, result in (
            ("train", train),
            ("valid", valid),
            ("post23", test),
        ):
            print(
                f"  {name:<5} "
                f"20d {result[20]['pass_rate']:.1%}/"
                f"{result[20]['fail_rate']:.1%} "
                f"30d {result[30]['pass_rate']:.1%}/"
                f"{result[30]['fail_rate']:.1%} "
                f"rm30 {result[30]['restricted_mean_days']:.1f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
