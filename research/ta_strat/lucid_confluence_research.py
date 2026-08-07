"""
Confluence test using already development-selected causal signals.

No parameters are re-optimized.  The question is whether agreement between distinct
opening mechanisms raises expectancy enough to justify one larger-risk 25K trade:

  * NQ and ES 10:00 gap reversals agree;
  * NQ 09:45 opening drive agrees with its 10:00 gap reversal;
  * ES 09:45 gap-fill classifier agrees with its 10:00 gap reversal.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_gap_research as G
import lucid_portfolio_policy as S


def _map(trades: list[L.Trade]) -> dict[date, L.Trade]:
    return {trade.day: trade for trade in trades}


def candidates(days: dict[str, list[L.Day]]) -> dict[str, list[L.Trade]]:
    base = S.selected_signals(days)
    nq_morning = _map([
        t for t in base if t.market == "nq" and "morning" in t.strategy
    ])
    es_morning = _map([
        t for t in base if t.market == "es" and "morning" in t.strategy
    ])
    nq_gap = _map(G.generate(
        days["nq"],
        G.GapConfig(
            "nq", "opening_gap", 30, 0.002,
            "reverse", "turn", "atr", "rr", 2.0,
        ),
    ))
    es_gap = _map(G.generate(
        days["es"],
        G.GapConfig(
            "es", "opening_gap", 30, 0.002,
            "reverse", "turn", "extreme", "rr", 2.0,
        ),
    ))

    def agreed(left: dict[date, L.Trade], right: dict[date, L.Trade]) -> list[L.Trade]:
        return [
            replace(left[day], strategy="morning_confluence_" + left[day].strategy)
            for day in sorted(set(left) & set(right))
            if left[day].side == right[day].side
        ]

    cross = agreed(nq_gap, es_gap)
    nq_internal = agreed(nq_gap, nq_morning)
    es_internal = agreed(es_gap, es_morning)
    union = {
        (trade.market, trade.day, trade.entry_ts): trade
        for trade in cross + nq_internal + es_internal
    }
    return {
        "nq_es_gap": cross,
        "nq_internal": nq_internal,
        "es_internal": es_internal,
        "all_confluence": sorted(
            union.values(), key=lambda t: (t.entry_ts, S.signal_priority(t))
        ),
    }


def _basic(trades: list[L.Trade], lo: date | None, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), 300.0))


def main() -> int:
    days = {market: L.load_days(market) for market in ("nq", "es")}
    all_days = sorted({d.day for rows in days.values() for d in rows})
    candidates_by_name = candidates(days)
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    print("CONFLUENCE BASIC STATS AT $300 PLANNED RISK")
    for name, trades in candidates_by_name.items():
        print(f"\n{name}")
        for period, (lo, hi) in periods.items():
            result = _basic(trades, lo, hi)
            print(
                f"  {period:<5} n{result['n']:4} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )

    # Policies are fixed before the chronological test printout.
    policies = [
        S.Policy(risk, 0.0, -250.0, dd_scale, 500.0, up_scale, prior_off)
        for risk in (500.0, 700.0, 900.0)
        for dd_scale, up_scale, prior_off in (
            (1.0, 1.0, False),
            (0.5, 1.0, True),
            (1.0, 1.5, False),
        )
    ]
    rows = []
    for name, trades in candidates_by_name.items():
        for policy in policies:
            dev = {}
            for period in ("train", "valid"):
                lo, hi = periods[period]
                raw = S._slice_trades(trades, lo, hi)
                sessions = S._slice_days(all_days, lo, hi)
                dev[period] = {
                    12: S.evaluate(raw, sessions, policy, 12, S.RULES_25K),
                    30: S.evaluate(raw, sessions, policy, 30, S.RULES_25K),
                }
            score = min(
                2 * dev[p][12]["pass_rate"] + dev[p][30]["pass_rate"]
                - dev[p][30]["fail_rate"]
                for p in ("train", "valid")
            )
            rows.append((score, name, trades, policy, dev))
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nCONFLUENCE POLICIES SELECTED ON TRAIN + VALIDATION ONLY")
    for score, name, trades, policy, dev in rows[:10]:
        lo, hi = periods["test"]
        raw = S._slice_trades(trades, lo, hi)
        sessions = S._slice_days(all_days, lo, hi)
        test = {
            12: S.evaluate(raw, sessions, policy, 12, S.RULES_25K),
            30: S.evaluate(raw, sessions, policy, 30, S.RULES_25K),
        }
        print(f"\n{name} {policy.label} score {score:+.3f}")
        for period, result in (("train", dev["train"]), ("valid", dev["valid"]), ("TEST", test)):
            e12, e30 = result[12], result[30]
            print(
                f"  {period:<5} 12d pass {e12['pass_rate']*100:5.1f}% "
                f"fail {e12['fail_rate']*100:5.1f}% | "
                f"30d pass {e30['pass_rate']*100:5.1f}% "
                f"fail {e30['fail_rate']*100:5.1f}% "
                f"median {e30['median_days']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
