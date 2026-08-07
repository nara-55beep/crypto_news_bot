"""
Causal shadow-performance gate for the price-only NQ regime strategy.

The unfiltered strategy is weak in 2016--2017 under conservative friction.
This test does not use a hindsight date cutoff.  Every raw hypothetical trade
continues to update a rolling normalized-R history, including while live
entries are disabled.  A trade is allowed only when the previous N shadow
outcomes have positive mean R.  Therefore the gate can turn off and later
reactivate using information that was genuinely available at each timestamp.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date

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


def shadow_gate(
    trades: list[L.Trade],
    *,
    lookback: int,
    by_sleeve: bool,
    threshold: float = 0.0,
) -> list[L.Trade]:
    histories: dict[str, deque[float]] = defaultdict(
        lambda: deque(maxlen=lookback)
    )
    accepted = []
    for trade in sorted(trades, key=lambda t: (t.entry_ts, t.exit_ts)):
        key = (
            "active" if "morning_regime_active_" in trade.strategy else "bear"
        ) if by_sleeve else "combined"
        history = histories[key]
        enabled = (
            len(history) == lookback
            and float(np.mean(history)) > threshold
        )
        if enabled:
            accepted.append(trade)
        # This is a shadow outcome: it is observed/calculated regardless of
        # whether the live gate accepted the trade.
        net = trade.gross_per_micro - L.COMMISSION_RT
        risk = trade.risk_per_micro + L.COMMISSION_RT
        history.append(net / max(risk, 1e-12))
    return accepted


def _eval(
    trades: list[L.Trade],
    dates: list[date],
) -> dict[int, dict]:
    return {
        horizon: S.evaluate(
            trades, dates, POLICY, horizon, S.RULES_25K
        )
        for horizon in (20, 30, 60)
    }


def _score(results: dict[str, dict[int, dict]]) -> float:
    return min(
        results[period][30]["pass_rate"]
        + 0.20 * results[period][60]["pass_rate"]
        - 3.0 * results[period][30]["fail_rate"]
        for period in ("train", "valid")
    )


def main() -> int:
    days, all_dates = L.load_days_and_sessions("nq")
    bars = G._bars(days)
    states = G.walkforward_states(bars, G._features(bars)[:, :2])
    active = G.generate(days, bars, R._signals(bars, states, 1), ACTIVE)
    bear = G.generate(days, bars, R._signals(bars, states, 0), BEAR)
    raw = R.merge_nonoverlap(
        active, bear, active_first=True, daily_cap=3
    )
    raw = B._extra_cost(raw, 2.5)

    rows = []
    for lookback in (25, 50, 100, 200, 400):
        for by_sleeve in (False, True):
            trades = shadow_gate(
                raw,
                lookback=lookback,
                by_sleeve=by_sleeve,
            )
            results = {}
            for period in ("train", "valid"):
                lo, hi = PERIODS[period]
                results[period] = _eval(
                    S._slice_trades(trades, lo, hi),
                    S._slice_days(all_dates, lo, hi),
                )
            rows.append((
                _score(results),
                lookback,
                by_sleeve,
                trades,
                results,
            ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, lookback, by_sleeve, trades, results in rows:
        post = _eval(
            S._slice_trades(trades, date(2024, 1, 1), None),
            S._slice_days(all_dates, date(2024, 1, 1), None),
        )
        print(
            f"\nlookback={lookback} by_sleeve={by_sleeve} "
            f"trades={len(trades)} score={score:.3f}"
        )
        for period, values in (
            ("train", results["train"]),
            ("valid", results["valid"]),
            ("post23", post),
        ):
            print(
                f"  {period:<6} "
                f"20d {values[20]['pass_rate']:.1%} "
                f"30d {values[30]['pass_rate']:.1%} "
                f"60d {values[60]['pass_rate']:.1%} "
                f"fail30 {values[30]['fail_rate']:.1%}"
            )

    _, lookback, by_sleeve, best, _ = rows[0]
    print(
        f"\nYEAR AUDIT best lookback={lookback} by_sleeve={by_sleeve}"
    )
    for year in sorted({session.year for session in all_dates}):
        stats = L.basic_stats(L.size_trades(
            [trade for trade in best if trade.day.year == year],
            500.0,
        ))
        print(
            f"  {year} n={stats['n']:4} PF={stats['pf']:.2f} "
            f"net={stats['net']:+9.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
