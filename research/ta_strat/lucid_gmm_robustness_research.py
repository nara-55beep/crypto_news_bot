"""
Specification and friction robustness for the frozen NQ regime portfolio.

The trading rules are not re-optimized here.  We perturb only modeling choices
that should not make a real signal disappear:

* prior-only GMM fit length;
* deterministic initialization seed;
* whether the Dukascopy proxy-volume feature is present.

We then replay the same active-long / bear-short configurations, one position
at a time, with one-tick limit penetration.  Cost stress is applied after the
causal fills as an extra round-turn charge per micro.  The 2024+ segment is a
chronological audit, not a pristine holdout: it has already been observed
during this iterative research program and must never be advertised otherwise.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_gmm_confluence_research as G
import lucid_gmm_regime_portfolio as R
import lucid_portfolio_policy as S


ACTIVE = G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 2)
BEAR = G.GConfig(0.15, 0.25, "short", 13, 30, 300, 13, 2)
PERIODS = {
    "train": (None, L.TRAIN_END),
    "valid": (date(2022, 1, 1), L.VALID_END),
    "post23": (date(2024, 1, 1), None),
}


def _extra_cost(trades: list[L.Trade], dollars_per_micro: float) -> list[L.Trade]:
    return [
        replace(
            trade,
            gross_per_micro=trade.gross_per_micro - dollars_per_micro,
            # The account simulator reserves stop risk before entry.  Higher
            # known friction must be inside that reservation as well as P&L.
            risk_per_micro=trade.risk_per_micro + dollars_per_micro,
        )
        for trade in trades
    ]


def _show(
    label: str,
    trades: list[L.Trade],
    all_dates: list[date],
    *,
    extra_cost: float = 0.0,
) -> None:
    if extra_cost:
        trades = _extra_cost(trades, extra_cost)
    print(f"\n{label} trades={len(trades)} extra_cost=${extra_cost:g}/micro")
    policy = S.Policy(300.0, 300.0)
    for period, (lo, hi) in PERIODS.items():
        selected = S._slice_trades(trades, lo, hi)
        dates = S._slice_days(all_dates, lo, hi)
        basic = L.basic_stats(L.size_trades(selected, 500.0))
        ev20 = S.evaluate(selected, dates, policy, 20, S.RULES_25K)
        ev30 = S.evaluate(selected, dates, policy, 30, S.RULES_25K)
        print(
            f"  {period:<5} n{basic['n']:4} PF{basic['pf']:.2f} "
            f"net{basic['net']:+9.0f} DD{basic['maxdd']:8.0f} | "
            f"20d {ev20['pass_rate']:.1%}/{ev20['fail_rate']:.1%} "
            f"30d {ev30['pass_rate']:.1%}/{ev30['fail_rate']:.1%} "
            f"rm30 {ev30['restricted_mean_days']:.1f}"
        )


def main() -> int:
    days, all_dates = L.load_days_and_sessions("nq")
    print(
        f"complete_model_sessions={len(days)} "
        f"evaluation_sessions={len(all_dates)} "
        f"zero_trade_data_guard_sessions={len(all_dates) - len(days)}",
        flush=True,
    )
    bars = G._bars(days)
    full_x = G._features(bars)
    variants = (
        ("base_5000_seed20260731_all3", 5_000, 20260731, full_x),
        ("fit2500_seed20260731_all3", 2_500, 20260731, full_x),
        ("fit10000_seed20260731_all3", 10_000, 20260731, full_x),
        ("fit5000_seed7_all3", 5_000, 7, full_x),
        ("fit5000_seed41_all3", 5_000, 41, full_x),
        (
            "fit5000_seed20260731_price_only",
            5_000,
            20260731,
            full_x[:, :2],
        ),
        ("fit5000_seed7_price_only", 5_000, 7, full_x[:, :2]),
        ("fit5000_seed41_price_only", 5_000, 41, full_x[:, :2]),
    )
    generated: dict[str, list[L.Trade]] = {}
    for label, train_bars, seed, x in variants:
        print(f"fitting {label}", flush=True)
        states = G.walkforward_states(
            bars,
            x,
            train_bars=train_bars,
            random_state=seed,
        )
        active = G.generate(days, bars, R._signals(bars, states, 1), ACTIVE)
        bear = G.generate(days, bars, R._signals(bars, states, 0), BEAR)
        trades = R.merge_nonoverlap(
            active,
            bear,
            active_first=True,
            daily_cap=3,
        )
        generated[label] = trades
        _show(label, trades, all_dates)

    base = generated["base_5000_seed20260731_all3"]
    # Base execution already includes one tick at the exit ($0.50/MNQ micro)
    # and $1 commission.  Extra $2.50 brings the all-in deduction to the
    # paper's conservative two NQ points ($4.00/micro); $4.00 goes beyond it.
    for extra in (1.0, 2.0, 2.5, 4.0):
        _show("base_friction_stress", base, all_dates, extra_cost=extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
