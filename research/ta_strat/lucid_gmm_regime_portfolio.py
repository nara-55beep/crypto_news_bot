"""
Single-position NQ regime portfolio: active-state long + bear-state short.

Each sleeve is generated causally, then a deterministic scheduler rejects any
entry that overlaps an already accepted position.  This avoids the physically
incorrect assumption that a futures account can hold separate long and short
MNQ positions at the same time.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_gmm_confluence_research as G
import lucid_portfolio_policy as S


ACTIVE_CONFIGS = (
    G.GConfig(0.10, 0.25, "long", 13, 30, 300, 13, 2),
    G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 2),
)
BEAR_CONFIGS = (
    G.GConfig(0.10, 0.25, "short", 13, 30, 300, 13, 1),
    G.GConfig(0.15, 0.25, "short", 13, 60, 300, 13, 2),
    G.GConfig(0.15, 0.25, "short", 13, 30, 300, 13, 2),
)


def _signals(
    bars: list[G.BarRef],
    states,
    wanted: int,
) -> list[G.Signal]:
    return [
        G.Signal(
            i, bar.day_i, bar.end_i,
            int(bar.ret > 0) - int(bar.ret < 0),
            float(wanted),
        )
        for i, bar in enumerate(bars)
        if states[i] == wanted
    ]


def merge_nonoverlap(
    active: list[L.Trade],
    bear: list[L.Trade],
    *,
    active_first: bool,
    daily_cap: int,
) -> list[L.Trade]:
    tagged = [
        (0 if active_first else 1, replace(
            trade, strategy="morning_regime_active_" + trade.strategy
        ))
        for trade in active
    ] + [
        (1 if active_first else 0, replace(
            trade, strategy="regime_bear_" + trade.strategy
        ))
        for trade in bear
    ]
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


def _score(result: dict[str, dict[int, dict]]) -> float:
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
    active_signals = _signals(bars, states, 1)
    bear_signals = _signals(bars, states, 0)
    active_sets = {
        cfg.label: G.generate(days, bars, active_signals, cfg)
        for cfg in ACTIVE_CONFIGS
    }
    bear_sets = {
        cfg.label: G.generate(days, bars, bear_signals, cfg)
        for cfg in BEAR_CONFIGS
    }
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    all_dates = [day.day for day in days]
    rows = []
    for active_name, active in active_sets.items():
        for bear_name, bear in bear_sets.items():
            for active_first in (True, False):
                for daily_cap in (2, 3):
                    trades = merge_nonoverlap(
                        active, bear,
                        active_first=active_first,
                        daily_cap=daily_cap,
                    )
                    for active_risk in (200.0, 300.0, 400.0, 500.0):
                        for bear_risk in (200.0, 300.0, 400.0, 500.0):
                            policy = S.Policy(active_risk, bear_risk)
                            result = {}
                            for period in ("train", "valid"):
                                lo, hi = periods[period]
                                selected_trades = S._slice_trades(
                                    trades, lo, hi
                                )
                                selected_days = S._slice_days(
                                    all_dates, lo, hi
                                )
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
                                _score(result), active_name, bear_name,
                                active_first, daily_cap, active_risk,
                                bear_risk, trades, result,
                            ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print(f"development combinations={len(rows)}")
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for (
        score, active_name, bear_name, active_first, daily_cap,
        active_risk, bear_risk, trades, result,
    ) in rows[:20]:
        lo, hi = periods["test"]
        test_trades = S._slice_trades(trades, lo, hi)
        test_days = S._slice_days(all_dates, lo, hi)
        policy = S.Policy(active_risk, bear_risk)
        test = {
            horizon: S.evaluate(
                test_trades, test_days, policy, horizon, S.RULES_25K
            )
            for horizon in (20, 30)
        }
        print(
            f"\nACTIVE={active_name}\nBEAR={bear_name}\n"
            f"active_first={active_first} cap={daily_cap} "
            f"active_risk={active_risk:.0f} bear_risk={bear_risk:.0f} "
            f"score={score:.3f}"
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
