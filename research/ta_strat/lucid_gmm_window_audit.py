"""
Dependence-aware audit of the frozen NQ regime portfolio.

Rolling daily starts are useful operationally but heavily overlap.  This audit
also reports:

* calendar-month starts;
* non-overlapping 30-session windows across every possible phase;
* calendar-year profit factors;
* a Newey-West t-statistic on fixed-risk daily P&L including zero-trade days.
"""
from __future__ import annotations

from datetime import date
import math

import numpy as np

import lucid_causal_rebuild as L
import lucid_gmm_confluence_research as G
import lucid_gmm_regime_portfolio as R
import lucid_gmm_robustness_research as B
import lucid_portfolio_policy as S


ACTIVE = G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 2)
BEAR = G.GConfig(0.15, 0.25, "short", 13, 30, 300, 13, 2)
POLICY = S.Policy(300.0, 300.0)
PERIODS = {
    "train": (None, L.TRAIN_END),
    "valid": (date(2022, 1, 1), L.VALID_END),
    "post23": (date(2024, 1, 1), None),
}


def _portfolio(
    days: list[L.Day],
    bars: list[G.BarRef],
    states: np.ndarray,
) -> list[L.Trade]:
    active = G.generate(days, bars, R._signals(bars, states, 1), ACTIVE)
    bear = G.generate(days, bars, R._signals(bars, states, 0), BEAR)
    return R.merge_nonoverlap(
        active,
        bear,
        active_first=True,
        daily_cap=3,
    )


def _by_day(trades: list[L.Trade], dates: list[date]) -> dict[date, list[L.Trade]]:
    out = {session: [] for session in dates}
    for trade in trades:
        if trade.day in out:
            out[trade.day].append(trade)
    return out


def _outcomes(
    trades: list[L.Trade],
    dates: list[date],
    starts: list[int],
    horizon: int = 30,
) -> dict:
    by_day = _by_day(trades, dates)
    outcomes = [
        S.simulate_window(
            by_day,
            dates[start:start + horizon],
            POLICY,
            S.RULES_25K,
        )[0]
        for start in starts
        if start + horizon <= len(dates)
    ]
    n = len(outcomes)
    return {
        "n": n,
        "pass": outcomes.count("pass") / n if n else math.nan,
        "fail": outcomes.count("fail") / n if n else math.nan,
    }


def _newey_west_t(values: np.ndarray, lag: int = 5) -> float:
    if len(values) < 2:
        return math.nan
    residual = values - float(np.mean(values))
    n = len(values)
    long_run = float(np.dot(residual, residual) / n)
    for k in range(1, min(lag, n - 1) + 1):
        covariance = float(np.dot(residual[k:], residual[:-k]) / n)
        long_run += 2.0 * (1.0 - k / (lag + 1.0)) * covariance
    if long_run <= 0:
        return math.nan
    return float(np.mean(values) / math.sqrt(long_run / n))


def main() -> int:
    days, all_dates = L.load_days_and_sessions("nq")
    bars = G._bars(days)
    # Audit the portable price-only specification and the same conservative
    # two-point all-in MNQ cost used by the policy search.
    states = G.walkforward_states(bars, G._features(bars)[:, :2])
    trades = B._extra_cost(_portfolio(days, bars, states), 2.5)
    print(
        f"complete_model_sessions={len(days)} "
        f"evaluation_sessions={len(all_dates)} trades={len(trades)}"
    )

    for period, (lo, hi) in PERIODS.items():
        selected_dates = S._slice_days(all_dates, lo, hi)
        selected_trades = S._slice_trades(trades, lo, hi)
        print(f"\n{period}")
        for horizon in (20, 30, 40, 60):
            ev = S.evaluate(
                selected_trades,
                selected_dates,
                POLICY,
                horizon,
                S.RULES_25K,
            )
            print(
                f"  rolling {horizon:2}d pass={ev['pass_rate']:.1%} "
                f"fail={ev['fail_rate']:.1%} "
                f"restricted_mean={ev['restricted_mean_days']:.1f}"
            )

        month_starts = [
            i for i, session in enumerate(selected_dates)
            if i == 0
            or (
                session.year,
                session.month,
            ) != (
                selected_dates[i - 1].year,
                selected_dates[i - 1].month,
            )
        ]
        monthly = _outcomes(
            selected_trades,
            selected_dates,
            month_starts,
        )
        print(
            f"  month-start 30d n={monthly['n']} "
            f"pass={monthly['pass']:.1%} fail={monthly['fail']:.1%}"
        )

        phase_results = []
        for offset in range(30):
            result = _outcomes(
                selected_trades,
                selected_dates,
                list(range(offset, len(selected_dates), 30)),
            )
            if result["n"]:
                phase_results.append(result)
        pass_rates = np.asarray([row["pass"] for row in phase_results])
        fail_rates = np.asarray([row["fail"] for row in phase_results])
        print(
            f"  nonoverlap phases 30d pass "
            f"min/median/max={np.min(pass_rates):.1%}/"
            f"{np.median(pass_rates):.1%}/{np.max(pass_rates):.1%}; "
            f"fail max={np.max(fail_rates):.1%}"
        )

        sized = L.size_trades(selected_trades, 500.0)
        pnl_by_day = {session: 0.0 for session in selected_dates}
        for trade in sized:
            pnl_by_day[trade.trade.day] += trade.pnl
        daily = np.asarray([pnl_by_day[session] for session in selected_dates])
        print(
            f"  fixed-risk daily mean=${np.mean(daily):+.2f} "
            f"NW(5) t={_newey_west_t(daily, 5):.2f}"
        )

    print("\ncalendar years at fixed $500 signal risk")
    for year in sorted({session.year for session in all_dates}):
        selected = [
            trade for trade in trades if trade.day.year == year
        ]
        stats = L.basic_stats(L.size_trades(selected, 500.0))
        print(
            f"  {year} n={stats['n']:4} PF={stats['pf']:.2f} "
            f"net={stats['net']:+9.0f} DD={stats['maxdd']:8.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
