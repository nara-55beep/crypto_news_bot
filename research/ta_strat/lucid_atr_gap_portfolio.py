"""
Exact 25K Lucid replay of causal gap-reversal and ATR-breakout sleeves.

The gap-reversal sleeve was selected in the earlier exact-replay research.  The
new sleeve takes NQ ATR breakouts only when the direction agrees with the
overnight gap and, in selected variants, a second pre-open state variable.
Because one sleeve fades opening gaps and the other follows sufficiently large
intraday breakouts, the combination has an economic diversification rationale.

All selection and risk-policy ranking uses 2016-2023.  Results from 2024 onward
are printed only for the development finalists.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_atr_regime_research as R
import lucid_causal_rebuild as L
import lucid_exact_portfolio as X
import lucid_fast_alpha_research as F
import lucid_portfolio_policy as S


ATR_VARIANTS = (
    (
        F.FastConfig("nq", 20, 0.35, "immediate", "immediate", 1, 0.0),
        R.Regime("gap_vol", ("gap_align", "vol_high")),
    ),
    (
        F.FastConfig("nq", 10, 0.35, "immediate", "immediate", 1, 0.0),
        R.Regime("gap_vol", ("gap_align", "vol_high")),
    ),
    (
        F.FastConfig("nq", 20, 0.35, "immediate", "immediate", 1, 0.0),
        R.Regime("gap_trend", ("gap_align", "trend_align")),
    ),
    (
        F.FastConfig("nq", 14, 0.50, "immediate", "immediate", 1, 0.0),
        R.Regime("none", ()),
    ),
)


def _score(results: dict[str, dict[int, dict]]) -> float:
    values = []
    for period in ("train", "valid"):
        r12 = results[period][12]
        r20 = results[period][20]
        r30 = results[period][30]
        values.append(
            r12["pass_rate"] + r20["pass_rate"] + 2.0 * r30["pass_rate"]
            - 2.0 * r20["fail_rate"] - 2.0 * r30["fail_rate"]
        )
    return min(values)


def main() -> int:
    days = {market: L.load_days(market) for market in ("nq", "es")}
    all_dates = sorted({day.day for rows in days.values() for day in rows})
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }

    gap_sets = X.candidates(days)
    features = R._features(days["nq"])
    atr_sets = {}
    for cfg, regime in ATR_VARIANTS:
        raw = R.filter_trades(F.generate(days["nq"], cfg), regime, features)
        atr_sets[(cfg.label, regime.name)] = [
            replace(trade, strategy="atr_follow_" + trade.strategy)
            for trade in raw
        ]

    rows = []
    for gap_name in ("gaps", "frequency_gaps"):
        for (atr_label, regime_name), atr_trades in atr_sets.items():
            trades = sorted(
                gap_sets[gap_name] + atr_trades,
                key=lambda trade: (
                    trade.entry_ts, S.signal_priority(trade), trade.exit_ts
                ),
            )
            for gap_risk in (500.0, 700.0, 900.0):
                for atr_risk in (300.0, 500.0, 700.0, 900.0):
                    bases = (
                        S.Policy(gap_risk, atr_risk),
                        S.Policy(
                            gap_risk, atr_risk, -250.0, 0.5, 500.0, 1.0, False
                        ),
                        S.Policy(
                            gap_risk, atr_risk, -250.0, 0.5, 500.0, 1.5, False
                        ),
                    )
                    for policy in bases:
                        dev: dict[str, dict[int, dict]] = {}
                        for period in ("train", "valid"):
                            lo, hi = periods[period]
                            selected_trades = S._slice_trades(trades, lo, hi)
                            selected_days = S._slice_days(all_dates, lo, hi)
                            dev[period] = {
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
                                _score(dev), gap_name, atr_label, regime_name,
                                trades, policy, dev,
                            )
                        )

    rows.sort(key=lambda row: row[0], reverse=True)
    print(f"development combinations={len(rows)}")
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for (
        score, gap_name, atr_label, regime_name, trades, policy, dev
    ) in rows[:15]:
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
            f"\nGAP={gap_name}\nATR={atr_label} + {regime_name}\n"
            f"policy={policy.label} score={score:.3f}"
        )
        for period, result in (
            ("train", dev["train"]), ("valid", dev["valid"]), ("TEST", test)
        ):
            fields = []
            for horizon in (12, 20, 30):
                item = result[horizon]
                fields.append(
                    f"{horizon}d {item['pass_rate']:.1%}/"
                    f"{item['fail_rate']:.1%}"
                )
            print(
                f"  {period:<5} {' | '.join(fields)} "
                f"rm30 {result[30]['restricted_mean_days']:.1f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
