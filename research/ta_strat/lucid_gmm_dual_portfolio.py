"""Exact 25K replay of frozen NQ and cross-market ES GMM-state sleeves."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_gmm_confluence_research as G
import lucid_portfolio_policy as S


def _make(
    days: list[L.Day],
    cfg: G.GConfig,
    market: str,
    prefix: str,
) -> list[L.Trade]:
    bars = G._bars(days)
    x = G._features(bars)
    signals = G.walkforward_signals(
        bars, x, transition_threshold=-1.0, volume_threshold=-999.0
    )
    return [
        replace(trade, strategy=prefix + trade.strategy)
        for trade in G.generate(days, bars, signals, cfg, market=market)
    ]


def _score(result: dict[str, dict[int, dict]]) -> float:
    return min(
        result[period][20]["pass_rate"]
        + 2.0 * result[period][30]["pass_rate"]
        - 3.0 * result[period][30]["fail_rate"]
        for period in ("train", "valid")
    )


def main() -> int:
    days = {market: L.load_days(market) for market in ("nq", "es")}
    nq_cfgs = (
        G.GConfig(0.10, 0.25, "long", 13, 30, 300, 13, 2),
        G.GConfig(0.15, 0.25, "long", 13, 60, 300, 13, 2),
    )
    es_cfg = G.GConfig(0.10, 0.25, "long", 13, 60, 300, 13, 2)
    nq_sets = {
        cfg.label: _make(days["nq"], cfg, "nq", "morning_gmm_nq_")
        for cfg in nq_cfgs
    }
    es = _make(days["es"], es_cfg, "es", "gmm_es_")
    all_dates = sorted({day.day for rows in days.values() for day in rows})
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }

    rows = []
    for nq_name, nq in nq_sets.items():
        trades = sorted(
            nq + es,
            key=lambda trade: (
                trade.entry_ts, S.signal_priority(trade), trade.exit_ts
            ),
        )
        for nq_risk in (200.0, 300.0, 400.0, 500.0, 600.0):
            for es_risk in (100.0, 200.0, 300.0, 400.0, 500.0):
                policy = S.Policy(nq_risk, es_risk)
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
                    _score(result), nq_name, trades, policy, result
                ))

    rows.sort(key=lambda row: row[0], reverse=True)
    print(f"development combinations={len(rows)}")
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, nq_name, trades, policy, result in rows[:15]:
        lo, hi = periods["test"]
        selected_trades = S._slice_trades(trades, lo, hi)
        selected_days = S._slice_days(all_dates, lo, hi)
        test = {
            horizon: S.evaluate(
                selected_trades,
                selected_days,
                policy,
                horizon,
                S.RULES_25K,
            )
            for horizon in (20, 30)
        }
        print(
            f"\nNQ={nq_name}\nES={es_cfg.label}\n"
            f"policy={policy.label} score={score:.3f}"
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
