"""
Frozen strongest development-selected causal LucidPro 25K research candidate.

This is intentionally not called a deployable or guaranteed strategy.  Its purpose
is to reproduce the best honest result from the completed search with one command.

Sleeves:
  * NQ: observe the completed 09:30 minute, follow its direction from 09:31
        through the 15:59 minute open; protective stop = 0.50 prior RTH range.
  * ES: observe the completed first five minutes, follow their direction from
        09:35 through the 13:30 minute open; same protective-stop formula.

The shared 25K policy requests $500 NQ risk and $200 ES risk, subject to integer
micros, the aggregate 20-micro cap, commissions, reserved MLL room, and prior-EOD
trailing-floor accounting.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_opening_horizon_search as H
import lucid_portfolio_policy as S


NQ_CONFIG = H.HConfig("nq", 1, 389, "opening", False, 0.50)
ES_CONFIG = H.HConfig("es", 5, 240, "opening", False, 0.50)
POLICY = S.Policy(500.0, 200.0)


def signals(days: dict[str, list[L.Day]]) -> list[L.Trade]:
    nq = [
        replace(t, strategy="morning_nq_" + t.strategy)
        for t in H.generate(days["nq"], NQ_CONFIG)
    ]
    es = [
        replace(t, strategy="es_" + t.strategy)
        for t in H.generate(days["es"], ES_CONFIG)
    ]
    return sorted(
        nq + es,
        key=lambda t: (t.entry_ts, S.signal_priority(t), t.exit_ts),
    )


def main() -> int:
    days = {market: L.load_days(market) for market in ("es", "nq")}
    trades = signals(days)
    all_dates = sorted({day.day for rows in days.values() for day in rows})
    periods = (
        ("train", None, L.TRAIN_END),
        ("valid", date(2022, 1, 1), L.VALID_END),
        ("TEST", date(2024, 1, 1), None),
    )
    print(
        f"NQ={NQ_CONFIG.label}\nES={ES_CONFIG.label}\n"
        f"policy={POLICY.label}\n"
    )
    for name, lo, hi in periods:
        dates = [
            session for session in all_dates
            if (lo is None or session >= lo) and (hi is None or session <= hi)
        ]
        selected = [
            trade for trade in trades
            if (lo is None or trade.day >= lo) and (hi is None or trade.day <= hi)
        ]
        for horizon in (20, 30):
            result = S.evaluate(
                selected, dates, POLICY, horizon, S.RULES_25K
            )
            print(
                f"{name:<5} {horizon:2}d starts={result['starts']:4} "
                f"pass={result['pass_rate']:.1%} "
                f"fail={result['fail_rate']:.1%} "
                f"timeout={result['undecided']/result['starts']:.1%} "
                f"conditional_median={result['median_days']} "
                f"all_start_restricted_mean="
                f"{result['restricted_mean_days']:.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
