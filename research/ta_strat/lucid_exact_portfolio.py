"""
Exact-replay 25K portfolio evaluation.

Candidate signals:
  * development-selected NQ and ES opening-gap reversals;
  * a higher-frequency non-stacked gap pair chosen by the exact-replay search;
  * development-selected one-day NQ reversal, included as a high-frequency sleeve.

Every position is converted to completed-close/next-open exit logic before sizing.
Portfolio selection and risk-policy ranking use 2016-2023 only; 2024+ is printed
after selection.  The current LucidPro 25K target, EOD MLL, and 20-micro cap come
from ``lucid_portfolio_policy.RULES_25K``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_daily_research as D
import lucid_gap_research as G
import lucid_portfolio_policy as S


def candidates(
    days: dict[str, list[L.Day]],
) -> dict[str, list[L.Trade]]:
    nq_gap_cfg = G.GapConfig(
        "nq", "opening_gap", 30, 0.002,
        "reverse", "turn", "atr", "rr", 2.0,
    )
    es_gap_cfg = G.GapConfig(
        "es", "opening_gap", 30, 0.002,
        "reverse", "turn", "extreme", "rr", 2.0,
    )
    nq_frequency_cfg = G.GapConfig(
        "nq", "opening_gap", 15, 0.002,
        "reverse", "turn", "atr", "rr", 2.0,
    )
    es_frequency_cfg = G.GapConfig(
        "es", "opening_gap", 30, 0.001,
        "reverse", "turn", "extreme", "rr", 2.0,
    )
    daily_cfg = D.DailyConfig(
        "nq", 1, "reverse", "none", 0.0, 0.50, None
    )
    nq_gap = B.convert_all(
        days["nq"], G.generate(days["nq"], nq_gap_cfg), protective_stop=True
    )
    es_gap = B.convert_all(
        days["es"], G.generate(days["es"], es_gap_cfg), protective_stop=True
    )
    daily = B.convert_all(
        days["nq"], D.generate(days["nq"], daily_cfg), protective_stop=True
    )
    nq_frequency = B.convert_all(
        days["nq"],
        G.generate(days["nq"], nq_frequency_cfg),
        protective_stop=True,
    )
    es_frequency = B.convert_all(
        days["es"],
        G.generate(days["es"], es_frequency_cfg),
        protective_stop=True,
    )

    def mark(rows: list[L.Trade], sleeve: str) -> list[L.Trade]:
        return [
            replace(trade, strategy=f"morning_exact_{sleeve}_{trade.strategy}")
            for trade in rows
        ]

    gaps = mark(nq_gap, "gap") + mark(es_gap, "gap")
    frequency_gaps = (
        mark(nq_frequency, "frequency_gap")
        + mark(es_frequency, "frequency_gap")
    )
    daily = mark(daily, "daily")
    return {
        "gaps": sorted(gaps, key=lambda t: (t.entry_ts, S.signal_priority(t))),
        "frequency_gaps": sorted(
            frequency_gaps,
            key=lambda t: (t.entry_ts, S.signal_priority(t)),
        ),
        "daily": sorted(daily, key=lambda t: (t.entry_ts, S.signal_priority(t))),
        "gaps_daily": sorted(
            gaps + daily, key=lambda t: (t.entry_ts, S.signal_priority(t))
        ),
    }


def policies() -> list[S.Policy]:
    return [
        S.Policy(risk, 0.0, -250.0, down, 500.0, up, False)
        for risk in (300.0, 400.0, 500.0, 700.0, 900.0)
        for down, up in (
            (1.0, 1.0),
            (0.5, 1.0),
            (1.0, 1.5),
            (0.5, 1.5),
        )
    ]


def _score(e12: dict, e30: dict) -> float:
    return (
        2.0 * e12["pass_rate"]
        + e30["pass_rate"]
        - 2.0 * e12["fail_rate"]
        - 1.5 * e30["fail_rate"]
    )


def _basic(
    trades: list[L.Trade],
    lo: date | None,
    hi: date | None,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), 500.0))


def main() -> int:
    days = {market: L.load_days(market) for market in ("nq", "es")}
    portfolios = candidates(days)
    all_days = sorted({d.day for rows in days.values() for d in rows})
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    print("EXACT-REPLAY BASIC STATS AT $500 PLANNED RISK")
    for portfolio_name, trades in portfolios.items():
        print(f"\n{portfolio_name}")
        for period, (lo, hi) in periods.items():
            result = _basic(trades, lo, hi)
            print(
                f"  {period:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )

    rows = []
    for portfolio_name, trades in portfolios.items():
        for policy in policies():
            development = {}
            for period in ("train", "valid"):
                lo, hi = periods[period]
                period_trades = S._slice_trades(trades, lo, hi)
                period_days = S._slice_days(all_days, lo, hi)
                development[period] = {
                    12: S.evaluate(
                        period_trades, period_days, policy, 12, S.RULES_25K
                    ),
                    30: S.evaluate(
                        period_trades, period_days, policy, 30, S.RULES_25K
                    ),
                }
            score = min(
                _score(development[p][12], development[p][30])
                for p in ("train", "valid")
            )
            rows.append((score, portfolio_name, trades, policy, development))
        print(f"completed {portfolio_name}", flush=True)

    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, portfolio_name, trades, policy, development in rows[:15]:
        lo, hi = periods["test"]
        test_trades = S._slice_trades(trades, lo, hi)
        test_days = S._slice_days(all_days, lo, hi)
        test = {
            12: S.evaluate(
                test_trades, test_days, policy, 12, S.RULES_25K
            ),
            30: S.evaluate(
                test_trades, test_days, policy, 30, S.RULES_25K
            ),
        }
        print(f"\n{portfolio_name} {policy.label} score {score:+.3f}")
        for name, result in (
            ("train", development["train"]),
            ("valid", development["valid"]),
            ("TEST", test),
        ):
            e12, e30 = result[12], result[30]
            print(
                f"  {name:<5} 12d pass {e12['pass_rate']*100:5.1f}% "
                f"fail {e12['fail_rate']*100:5.1f}% | "
                f"30d pass {e30['pass_rate']*100:5.1f}% "
                f"fail {e30['fail_rate']*100:5.1f}% "
                f"median {e30['median_days']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
