"""Direction tests for each canonical walk-forward NQ GMM state."""
from __future__ import annotations

from datetime import date

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_gmm_confluence_research as G


def _signals(
    bars: list[G.BarRef],
    states,
    wanted: int,
) -> list[G.Signal]:
    return [
        G.Signal(
            bar_i=i,
            day_i=bar.day_i,
            end_i=bar.end_i,
            side_hint=int(bar.ret > 0) - int(bar.ret < 0),
            transition=float(wanted),
        )
        for i, bar in enumerate(bars)
        if states[i] == wanted
    ]


def main() -> int:
    days = L.load_days("nq")
    bars = G._bars(days)
    x = G._features(bars)
    states = G.walkforward_states(bars, x)
    by_state = {
        state: _signals(bars, states, state) for state in (0, 1, 2)
    }
    rows = []
    for state in (0, 1, 2):
        for direction in ("long", "short"):
            for pullback in (0.10, 0.15):
                for start in (30, 60):
                    for maximum in (1, 2):
                        cfg = G.GConfig(
                            pullback, 0.25, direction, 13,
                            start, 300, 13, maximum,
                        )
                        trades = G.generate(
                            days, bars, by_state[state], cfg
                        )
                        train = L.basic_stats(L.size_trades(
                            L._slice(trades, None, L.TRAIN_END), 500.0
                        ))
                        valid = L.basic_stats(L.size_trades(
                            L._slice(
                                trades, date(2022, 1, 1), L.VALID_END
                            ),
                            500.0,
                        ))
                        rows.append((
                            E.development_score(train, valid),
                            state, cfg, trades, train, valid,
                        ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, state, cfg, trades, train, valid in rows[:25]:
        test = L.basic_stats(L.size_trades(
            L._slice(trades, date(2024, 1, 1), None), 500.0
        ))
        print(f"\nstate{state} {cfg.label} score {score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
