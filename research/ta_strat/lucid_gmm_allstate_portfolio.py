"""Single-position NQ portfolio using all three canonical GMM states."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_gmm_confluence_research as G
import lucid_gmm_regime_portfolio as R
import lucid_portfolio_policy as S


ACTIVE = G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 2)
BEAR = G.GConfig(0.15, 0.25, "short", 13, 30, 300, 13, 2)
BULLS = (
    G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 1),
    G.GConfig(0.10, 0.25, "long", 13, 60, 300, 13, 1),
)


def merge(
    sleeves: list[tuple[int, str, list[L.Trade]]],
    daily_cap: int,
) -> list[L.Trade]:
    tagged = []
    for priority, name, trades in sleeves:
        tagged.extend(
            (
                priority,
                replace(
                    trade,
                    strategy=f"morning_allstate_{name}_{trade.strategy}",
                ),
            )
            for trade in trades
        )
    tagged.sort(
        key=lambda item: (
            item[1].entry_ts, item[0], item[1].exit_ts
        )
    )
    accepted = []
    last_exit = {}
    counts = {}
    for _, trade in tagged:
        if counts.get(trade.day, 0) >= daily_cap:
            continue
        if trade.entry_ts < last_exit.get(trade.day, trade.entry_ts):
            continue
        accepted.append(trade)
        last_exit[trade.day] = trade.exit_ts
        counts[trade.day] = counts.get(trade.day, 0) + 1
    return accepted


def score(result: dict[str, dict[int, dict]]) -> float:
    return min(
        result[period][20]["pass_rate"]
        + 2.0 * result[period][30]["pass_rate"]
        - 3.0 * result[period][30]["fail_rate"]
        for period in ("train", "valid")
    )


def main() -> int:
    days = L.load_days("nq")
    bars = G._bars(days)
    x = G._features(bars)
    states = G.walkforward_states(bars, x)
    signals = {
        state: R._signals(bars, states, state) for state in (0, 1, 2)
    }
    active = G.generate(days, bars, signals[1], ACTIVE)
    bear = G.generate(days, bars, signals[0], BEAR)
    bull_sets = {
        cfg.label: G.generate(days, bars, signals[2], cfg)
        for cfg in BULLS
    }
    all_dates = [day.day for day in days]
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    rows = []
    for bull_name, bull in bull_sets.items():
        for cap in (3, 4):
            trades = merge(
                [
                    (0, "active", active),
                    (1, "bear", bear),
                    (2, "bull", bull),
                ],
                cap,
            )
            for risk in (200.0, 300.0, 400.0, 500.0):
                policy = S.Policy(risk, risk)
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
                        for horizon in (20, 30)
                    }
                rows.append((
                    score(result), bull_name, cap, risk, trades, result
                ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print(f"development combinations={len(rows)}")
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for value, bull_name, cap, risk, trades, result in rows:
        lo, hi = periods["test"]
        selected_trades = S._slice_trades(trades, lo, hi)
        selected_days = S._slice_days(all_dates, lo, hi)
        policy = S.Policy(risk, risk)
        test = {
            horizon: S.evaluate(
                selected_trades, selected_days, policy, horizon, S.RULES_25K
            )
            for horizon in (20, 30)
        }
        print(
            f"\nBULL={bull_name} cap={cap} risk={risk:.0f} "
            f"score={value:.3f}"
        )
        for period, values in (
            ("train", result["train"]),
            ("valid", result["valid"]),
            ("TEST", test),
        ):
            print(
                f"  {period:<5} 20d {values[20]['pass_rate']:.1%}/"
                f"{values[20]['fail_rate']:.1%} | "
                f"30d {values[30]['pass_rate']:.1%}/"
                f"{values[30]['fail_rate']:.1%} "
                f"rm30 {values[30]['restricted_mean_days']:.1f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
