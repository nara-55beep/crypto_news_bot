"""
Small predeclared frequency search for exact-replay opening-gap reversals.

This varies only the opening window, gap threshold, stop definition, and reward
multiple around the already documented gap-reversal mechanism.  Signals are created
by ``lucid_gap_research`` and every exit is converted to completed-close/next-open
execution by ``lucid_barclose_execution`` before any statistics are calculated.
"""
from __future__ import annotations

import math
from datetime import date

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
import lucid_gap_research as G


def configs(market: str) -> list[G.GapConfig]:
    return [
        G.GapConfig(
            market,
            "opening_gap",
            minute,
            threshold,
            "reverse",
            confirmation,
            stop_mode,
            "rr",
            rr,
        )
        for minute in (15, 30, 60)
        for threshold in (0.001, 0.002, 0.003)
        for confirmation in ("any", "turn")
        for stop_mode in ("extreme", "atr")
        for rr in (1.5, 2.0)
    ]


def stats(
    trades: list[L.Trade],
    lo: date | None,
    hi: date | None,
    risk: float = 300.0,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 50:
        return -1e9
    return min(train["pf"], valid["pf"]) * math.log1p(
        min(train["n"], valid["n"])
    ) + min(train["avg"], valid["avg"]) / 100.0


def main() -> int:
    rows = []
    for market in ("nq", "es"):
        days = L.load_days(market)
        grid = configs(market)
        print(f"{market}: {len(grid)} exact-replay configs", flush=True)
        for cfg in grid:
            trades = B.convert_all(
                days, G.generate(days, cfg), protective_stop=True
            )
            train = stats(trades, None, L.TRAIN_END)
            valid = stats(trades, date(2022, 1, 1), L.VALID_END)
            rows.append((score(train, valid), cfg, trades, train, valid))
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for development_score, cfg, trades, train, valid in rows[:20]:
        test = stats(trades, date(2024, 1, 1), None)
        print(f"\n{cfg.label} score {development_score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:4} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
